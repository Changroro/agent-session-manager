"""Persistent usage ledger (SQLite at ~/.asm/usage.db).

Solves three problems the in-memory scan cannot:

- **Incremental**: files are re-parsed only when (mtime, size) changed since
  the last scan, so a refresh stats the tree instead of re-reading it.
- **Prices frozen at scan time**: cost is computed with the rates in effect
  when a session is first parsed and stored; later rate changes never
  re-value old sessions. (The initial backfill necessarily uses current
  rates — historical rate data does not exist anywhere.)
- **Survives deletion**: rows are kept when the source file disappears
  (Claude Code prunes transcripts via cleanupPeriodDays), so cost history
  outlives the session files. Delete ~/.asm/usage.db to rebuild from disk.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

ProgressCB = Callable[[str, int, int], None]  # (source, done, total_new)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scanned_files(
    path TEXT PRIMARY KEY, mtime REAL, size INTEGER, fingerprint TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS claude_messages(
    msg_id TEXT PRIMARY KEY, ts TEXT, model TEXT,
    input INTEGER, output INTEGER, cache_read INTEGER, cache_create INTEGER,
    cost REAL, project_dir TEXT, file TEXT);
CREATE INDEX IF NOT EXISTS idx_claude_file ON claude_messages(file);
CREATE TABLE IF NOT EXISTS codex_sessions(
    path TEXT PRIMARY KEY, session_id TEXT, cwd TEXT, model TEXT,
    started TEXT, mtime REAL, size INTEGER,
    input INTEGER, cached_input INTEGER, output INTEGER, cost REAL,
    first_prompt TEXT, git_branch TEXT);
CREATE TABLE IF NOT EXISTS claude_titles(
    path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, first_prompt TEXT, ai_title TEXT);
"""

_PROGRESS_EVERY = 200


def _db_path() -> Path:
    from asm import models

    return models.APP_DATA_DIR / "usage.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if os.name != "nt" and path.parent.stat().st_mode & 0o777 != 0o700:
        raise OSError(f"Could not restrict ledger directory: {path.parent}")
    # Generous timeout: a one-time backfill in another asm process can hold the write lock.
    conn = sqlite3.connect(str(path), timeout=60)
    conn.executescript(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scanned_files)")}
    if "fingerprint" not in columns:
        conn.execute("ALTER TABLE scanned_files ADD COLUMN fingerprint TEXT")
    path.chmod(0o600)
    if os.name != "nt" and path.stat().st_mode & 0o777 != 0o600:
        raise OSError(f"Could not restrict usage ledger: {path}")
    return conn


def _known_files(conn: sqlite3.Connection, source: str) -> dict[str, tuple[float, int, str | None]]:
    return {
        row[0]: (row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT path, mtime, size, fingerprint FROM scanned_files WHERE source = ?", (source,)
        )
    }


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _changed(known: dict, path: Path) -> tuple[float, int, str] | None:
    """Return the current file identity when it is new or changed.

    Unchanged (mtime, size) means unchanged: hashing every file on every refresh
    re-read the whole ~/.claude + ~/.codex tree (many GB) each time asm opened.
    """
    try:
        st = path.stat()
        prev = known.get(str(path))
        if prev is not None and prev[0] == st.st_mtime and prev[1] == st.st_size:
            return None
        fingerprint = _fingerprint(path)
    except OSError as exc:
        raise OSError(f"cannot fingerprint {path}: {exc}") from exc
    stamp = (st.st_mtime, st.st_size, fingerprint)
    return None if prev == stamp else stamp


def _load_rates_if_needed(new_count: int) -> None:
    if new_count:
        from asm.services import pricing

        pricing.load_live_rates()


# ── Claude ───────────────────────────────────────────────────────────────


# Tabs load in parallel worker threads; one updater at a time avoids duplicate
# scans and "database is locked" (the next caller then finds nothing new).
_update_lock = threading.Lock()


def update_claude(progress: ProgressCB | None = None) -> int:
    """Parse new/changed Claude session files into the ledger. Returns count."""
    with _update_lock:
        return _update_claude(progress)


def _update_claude(progress: ProgressCB | None) -> int:
    from asm.services import claude_data

    root = claude_data.PROJECTS_DIR
    if not root.exists():
        return 0
    conn = _connect()
    try:
        known = _known_files(conn, "claude")
        todo: list[tuple[Path, str, tuple[float, int]]] = []
        for d in root.iterdir():
            if not d.is_dir():
                continue
            for jsonl in d.rglob("*.jsonl"):
                stamp = _changed(known, jsonl)
                if stamp is not None:
                    todo.append((jsonl, d.name, stamp))
        _load_rates_if_needed(len(todo))
        for i, (jsonl, project_dir, stamp) in enumerate(todo, 1):
            _ingest_claude_file(conn, jsonl, project_dir, stamp)
            if progress and (i % _PROGRESS_EVERY == 0 or i == len(todo)):
                progress("claude", i, len(todo))
        conn.commit()
        return len(todo)
    finally:
        conn.close()


def _ingest_claude_file(
    conn: sqlite3.Connection, jsonl: Path, project_dir: str, stamp: tuple[float, int, str]
) -> None:
    from asm.services.pricing import PricingDataError, calc_cost, is_billable

    rows = []
    with open(jsonl, encoding="utf-8") as f:
        for line in f:
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError(f"malformed Claude transcript: {jsonl}") from exc
            if not isinstance(msg, dict):
                raise ValueError(f"unexpected Claude transcript record: {jsonl}")
            if msg.get("type") != "assistant":
                continue
            m = msg.get("message")
            if not isinstance(m, dict):
                raise ValueError(f"unexpected Claude message shape: {jsonl}")
            usage = m.get("usage")
            model = m.get("model", "")
            ts_str = msg.get("timestamp", "")
            msg_id = m.get("id", "")
            if usage is None:
                continue
            if not isinstance(usage, dict) or not isinstance(model, str) or not isinstance(ts_str, str) or not isinstance(msg_id, str):
                raise ValueError(f"unexpected Claude usage shape: {jsonl}")
            if not usage or not ts_str or not msg_id or not is_billable(model):
                continue
            token_values = [usage.get(key, 0) for key in (
                "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"
            )]
            if any(not isinstance(value, (int, float)) or value < 0 for value in token_values):
                raise ValueError(f"unexpected Claude token usage: {jsonl}")
            if not any(token_values):
                continue
            try:
                cost = calc_cost(usage, model)
            except PricingDataError:
                cost = 0.0  # non-Anthropic model behind a proxy (e.g. GLM): tokens kept, no Claude price
            rows.append((
                msg_id, ts_str, model,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
                cost,
                project_dir, str(jsonl),
            ))
    frozen = {
        row[0]: row[1]
        for row in conn.execute("SELECT msg_id, cost FROM claude_messages WHERE file = ?", (str(jsonl),))
    }
    rows = [row[:7] + (frozen.get(row[0], row[7]),) + row[8:] for row in rows]
    # A rewrite (e.g. migrate) can drop lines — replace this file's rows wholesale.
    conn.execute("DELETE FROM claude_messages WHERE file = ?", (str(jsonl),))
    conn.executemany(
        "INSERT OR REPLACE INTO claude_messages VALUES (?,?,?,?,?,?,?,?,?,?)", rows
    )
    conn.execute(
        "INSERT OR REPLACE INTO scanned_files(path,mtime,size,fingerprint,source) VALUES (?,?,?,?,?)",
        (str(jsonl), stamp[0], stamp[1], stamp[2], "claude"),
    )


def claude_records() -> dict[str, dict]:
    """{msg_id: {usage, model, ts_str, project_dir, cost}} over the whole ledger."""
    conn = _connect()
    try:
        out: dict[str, dict] = {}
        for row in conn.execute(
            "SELECT msg_id, ts, model, input, output, cache_read, cache_create,"
            " cost, project_dir FROM claude_messages"
        ):
            out[row[0]] = {
                "usage": {
                    "input_tokens": row[3],
                    "output_tokens": row[4],
                    "cache_read_input_tokens": row[5],
                    "cache_creation_input_tokens": row[6],
                },
                "model": row[2],
                "ts_str": row[1],
                "project_dir": row[8],
                "cost": row[7],
            }
        return out
    finally:
        conn.close()


def claude_titles() -> dict[str, tuple[int, int, str, str]]:
    """{jsonl path: (mtime_ns, size, first_prompt, ai_title)} cached session titles."""
    conn = _connect()
    try:
        return {
            row[0]: (row[1], row[2], row[3], row[4])
            for row in conn.execute("SELECT path, mtime_ns, size, first_prompt, ai_title FROM claude_titles")
        }
    finally:
        conn.close()


def store_claude_titles(rows: list[tuple[str, int, int, str, str]]) -> None:
    conn = _connect()
    try:
        with conn:
            conn.executemany("INSERT OR REPLACE INTO claude_titles VALUES (?,?,?,?,?)", rows)
    finally:
        conn.close()


# ── Codex ────────────────────────────────────────────────────────────────


def update_codex(progress: ProgressCB | None = None) -> int:
    """Parse new/changed Codex rollouts into the ledger. Returns count."""
    with _update_lock:
        return _update_codex(progress)


def _update_codex(progress: ProgressCB | None) -> int:
    from asm.services import codex_data

    if not codex_data.CODEX_SESSIONS_DIR.exists():
        return 0
    conn = _connect()
    try:
        known = _known_files(conn, "codex")
        todo: list[tuple[Path, tuple[float, int]]] = []
        for f in codex_data._rollout_files():
            stamp = _changed(known, f)
            if stamp is not None:
                todo.append((f, stamp))
        _load_rates_if_needed(len(todo))
        for i, (f, stamp) in enumerate(todo, 1):
            _ingest_codex_file(conn, f, stamp)
            if progress and (i % _PROGRESS_EVERY == 0 or i == len(todo)):
                progress("codex", i, len(todo))
            if i % 1000 == 0:
                conn.commit()  # keep the one-time backfill restartable
        conn.commit()
        return len(todo)
    finally:
        conn.close()


def _ingest_codex_file(conn: sqlite3.Connection, f: Path, stamp: tuple[float, int, str]) -> None:
    from asm.services import codex_data, pricing

    info = codex_data._scan_session(f)
    if info is None:
        raise ValueError(f"rollout has no session metadata: {f}")
    usage = info["usage"]
    model = info["model"] or codex_data.UNKNOWN_MODEL
    cost = pricing.calc_openai_cost(usage, model) if usage else 0.0
    previous = conn.execute("SELECT cost FROM codex_sessions WHERE path = ?", (str(f),)).fetchone()
    if previous is not None:
        cost = previous[0]
    conn.execute(
        "INSERT OR REPLACE INTO codex_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(f), info["id"], info["cwd"], info["model"],
            info["started"], info["mtime"], info["size"],
            usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0),
            usage.get("output_tokens", 0), cost,
            info["first_prompt"], info["git_branch"],
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO scanned_files(path,mtime,size,fingerprint,source) VALUES (?,?,?,?,?)",
        (str(f), stamp[0], stamp[1], stamp[2], "codex"),
    )


def codex_records() -> list[dict]:
    """All Codex sessions in the ledger (deleted rollouts included), as the
    same info dicts the scanner produces, plus the frozen ``cost``."""
    conn = _connect()
    try:
        out = []
        for row in conn.execute(
            "SELECT path, session_id, cwd, model, started, mtime, size,"
            " input, cached_input, output, cost, first_prompt, git_branch"
            " FROM codex_sessions"
        ):
            usage = (
                {"input_tokens": row[7], "cached_input_tokens": row[8], "output_tokens": row[9]}
                if (row[7] or row[8] or row[9])
                else {}
            )
            out.append({
                "id": row[1], "cwd": row[2], "model": row[3],
                "first_prompt": row[11], "git_branch": row[12],
                "started": row[4], "usage": usage,
                "path": row[0], "size": row[6], "mtime": row[5],
                "cost": row[10],
            })
        return out
    finally:
        conn.close()
