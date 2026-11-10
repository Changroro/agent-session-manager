"""Service for reading OpenAI Codex CLI data (~/.codex).

Codex stores every session as a global ``sessions/YYYY/MM/DD/rollout-*.jsonl``
file (not per-project like Claude). Each rollout starts with a ``session_meta``
line carrying ``cwd``/``model``/``git`` and interleaves ``response_item`` and
``event_msg`` lines; ``event_msg`` ``token_count`` events carry cumulative token
usage. Aggregations read the persistent usage ledger (see services.ledger),
which parses each rollout once and keeps records even after files are deleted.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from asm.models import (
    CODEX_SESSIONS_DIR,
    ProjectInfo,
    SessionDetail,
    Stats,
)
from asm.utils import RECENT_DAYS_LIMIT, SUMMARY_MAX_CHARS, TOP_PROJECT_LIMIT, strict_child, validate_identifier

logger = logging.getLogger(__name__)


class CodexDataError(RuntimeError):
    pass

# Label for sessions whose rollout carries no model id anywhere; kept visible in
# model tables instead of silently assuming a model. Priced at the default GPT tier.
UNKNOWN_MODEL = "(unknown)"


def is_available() -> bool:
    return CODEX_SESSIONS_DIR.exists()


def _rollout_files(limit: int | None = None) -> list[Path]:
    """Return rollout files from the active CODEX_HOME, newest first."""
    if not CODEX_SESSIONS_DIR.exists():
        return []
    try:
        files = list(CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))
    except (PermissionError, OSError) as exc:
        raise CodexDataError(f"cannot read {CODEX_SESSIONS_DIR}: {exc}") from exc
    root = CODEX_SESSIONS_DIR.resolve()
    unsafe = [f for f in files if f.is_symlink() or not f.resolve().is_relative_to(root)]
    if unsafe:
        raise CodexDataError(f"refusing rollout outside active Codex home: {unsafe[0]}")
    files.sort(key=lambda f: _safe_mtime(f), reverse=True)
    return files[:limit] if limit else files


def _safe_mtime(f: Path) -> float:
    try:
        return f.stat().st_mtime
    except OSError as exc:
        raise CodexDataError(f"cannot stat {f}: {exc}") from exc


def total_session_count() -> int:
    if not CODEX_SESSIONS_DIR.exists():
        return 0
    try:
        return sum(1 for _ in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))
    except (PermissionError, OSError) as exc:
        raise CodexDataError(f"cannot read {CODEX_SESSIONS_DIR}: {exc}") from exc


# Memo of the ledger read so one refresh serves every aggregation once.
_scan_cache: dict[str, list[dict]] = {}


def refresh() -> None:
    """Drop the memoized ledger read (call when the user refreshes)."""
    _scan_cache.clear()


def _scanned_sessions() -> list[dict]:
    """All Codex sessions from the usage ledger (incremental scan first)."""
    cached = _scan_cache.get("all")
    if cached is not None:
        return cached
    from asm.services import ledger
    ledger.update_codex()
    root = CODEX_SESSIONS_DIR.resolve()
    sessions = [
        record
        for record in ledger.codex_records()
        if not Path(record["path"]).is_symlink()
        and Path(record["path"]).resolve().is_relative_to(root)
    ]
    _scan_cache["all"] = sessions
    return sessions


def _scan_session(f: Path) -> dict | None:
    """Parse one rollout file into a summary dict.

    Returns ``{id, cwd, model, first_prompt, git_branch, started, usage}`` or
    None if the file is unreadable / has no session_meta.
    """
    meta = None
    first_prompt = ""
    last_usage: dict | None = None
    model = ""
    try:
        with open(f) as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise CodexDataError(f"malformed rollout: {f}") from exc
                if not isinstance(obj, dict):
                    raise CodexDataError(f"unexpected rollout record: {f}")
                typ = obj.get("type")
                payload = obj.get("payload", {})
                if not isinstance(payload, dict):
                    raise CodexDataError(f"unexpected rollout payload: {f}")
                if typ == "session_meta":
                    if not isinstance(payload.get("cwd", ""), str):
                        raise CodexDataError(f"unexpected session cwd: {f}")
                    # Subagent/forked rollouts repeat the parent's session_meta
                    # after their own; the first one is this thread.
                    if meta is None:
                        meta = payload
                        model = payload.get("model", "") or model
                elif typ == "turn_context":
                    if not isinstance(payload.get("model", ""), str):
                        raise CodexDataError(f"unexpected turn model: {f}")
                    # The authoritative model id lives here (session_meta only
                    # carries model_provider). Last turn wins on mid-session switch.
                    model = payload.get("model", "") or model
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    if not isinstance(info, dict):
                        raise CodexDataError(f"unexpected token info: {f}")
                    usage = info.get("total_token_usage")
                    if usage:
                        if not isinstance(usage, dict):
                            raise CodexDataError(f"unexpected token usage: {f}")
                        last_usage = usage
                    if not model:
                        model = info.get("model", "") or model
                elif not first_prompt and typ == "response_item" and payload.get("type") == "message" \
                        and payload.get("role") == "user":
                    text = _extract_input_text(payload.get("content", []))
                    if text and not text.startswith("#") and not text.startswith("<"):
                        first_prompt = text[:SUMMARY_MAX_CHARS]
    except (OSError, UnicodeError) as exc:
        raise CodexDataError(f"cannot read {f}: {exc}") from exc
    if meta is None:
        return None
    git = meta.get("git") or {}
    if not isinstance(git, dict):
        raise CodexDataError(f"unexpected git metadata: {f}")
    if last_usage is not None and any(
        not isinstance(last_usage.get(key, 0), (int, float)) or last_usage.get(key, 0) < 0
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
    ):
        raise CodexDataError(f"unexpected token usage: {f}")
    if not isinstance(meta.get("id", f.stem), str) or not isinstance(meta.get("timestamp", ""), str):
        raise CodexDataError(f"unexpected session metadata: {f}")
    return {
        "id": meta.get("id", f.stem),
        "cwd": meta.get("cwd", "") or "",
        "model": model or meta.get("model", "") or "",
        "first_prompt": first_prompt,
        "git_branch": git.get("branch", "") if isinstance(git, dict) else "",
        "started": meta.get("timestamp", ""),
        "usage": last_usage or {},
        "path": str(f),
        "size": _safe_size(f),
        "mtime": _safe_mtime(f),
    }


def _safe_size(f: Path) -> int:
    try:
        return f.stat().st_size
    except OSError as exc:
        raise CodexDataError(f"cannot stat {f}: {exc}") from exc


def _extract_input_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                return block.get("text", "")
    return ""


def get_projects() -> list[ProjectInfo]:
    """Group Codex sessions by working directory into project entries."""
    cwds = {info["cwd"] or "(unknown)" for info in _scanned_sessions()}
    result = [
        ProjectInfo(path=cwd, exists=Path(cwd).exists() if cwd != "(unknown)" else False)
        for cwd in cwds
    ]
    result.sort(key=lambda p: p.path.casefold())
    return result


def _detail_from_info(info: dict) -> SessionDetail:
    summary = info["first_prompt"] or f"(session {info['id'][:8]})"
    return SessionDetail(
        session_id=info["id"],
        summary=summary,
        last_modified=info["mtime"],
        file_size=info["size"],
        first_prompt=info["first_prompt"],
        git_branch=info["git_branch"],
        cwd=info["cwd"],
        project_dir=info["path"],  # full rollout path, used for messages/trash
    )


def get_project_sessions(cwd: str) -> list[SessionDetail]:
    """Codex sessions for ``cwd`` whose rollout file still exists on disk."""
    result = [
        _detail_from_info(info)
        for info in _scanned_sessions()
        if info["cwd"] == cwd and Path(info["path"]).exists()
    ]
    result.sort(key=lambda s: s.last_modified, reverse=True)
    return result


def get_sessions_by_paths(paths) -> list[SessionDetail]:
    """Load Codex sessions directly from rollout file paths.

    Full-text search identifies matches by absolute path via ripgrep; this
    loads those straight from disk. Unreadable/missing paths are skipped;
    results are newest-first.
    """
    result = []
    root = CODEX_SESSIONS_DIR.resolve()
    for p in paths:
        f = Path(p)
        if not f.exists() and not f.is_symlink():
            continue
        if f.is_symlink():
            raise CodexDataError(f"Refusing to read rollout symlink: {f}")
        strict_child(f, root)
        info = _scan_session(f)
        if info is not None:
            result.append(_detail_from_info(info))
    result.sort(key=lambda s: s.last_modified, reverse=True)
    return result


def find_session(session_id: str) -> SessionDetail | None:
    """Locate one Codex session by id (full filename scan).

    Returns its SessionDetail (carrying ``cwd`` and the rollout ``project_dir``)
    or None if no rollout matches.
    """
    f = _find_rollout(session_id)
    if f is None:
        return None
    info = _scan_session(f)
    return _detail_from_info(info) if info is not None else None


def get_session_messages(session_id: str, project_dir: str | None = None, limit: int = 50) -> list[dict]:
    """Read user/assistant messages from a Codex rollout file.

    ``project_dir`` carries the rollout file path (set by get_project_sessions).
    """
    validate_identifier(session_id, "session id")
    if limit < 0:
        raise ValueError("limit must be non-negative")
    path = Path(project_dir) if project_dir else _find_rollout(session_id)
    if not path or not path.exists():
        return []
    if path.is_symlink():
        raise CodexDataError(f"Refusing to read rollout symlink: {path}")
    strict_child(path, CODEX_SESSIONS_DIR)
    messages: list[dict] = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise CodexDataError(f"malformed rollout: {path}") from exc
                if not isinstance(obj, dict):
                    raise CodexDataError(f"unexpected rollout record: {path}")
                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload", {})
                if not isinstance(payload, dict):
                    raise CodexDataError(f"unexpected rollout payload: {path}")
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _extract_message_text(payload.get("content", []))
                if text:
                    messages.append({"type": role, "content": text})
    except (OSError, UnicodeError) as exc:
        raise CodexDataError(f"cannot read {path}: {exc}") from exc
    return messages[-limit:] if limit and len(messages) > limit else messages


def _extract_message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") in ("input_text", "output_text", "text"):
                parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p)


def _find_rollout(session_id: str) -> Path | None:
    validate_identifier(session_id, "session id")
    for f in _rollout_files():
        if f.name.endswith(f"-{session_id}.jsonl"):
            return f
    return None


def get_period_usage(period: str = "daily") -> list[dict]:
    """Aggregate token usage/cost by period over the whole ledger.

    Each session's final cumulative token usage is attributed to its start
    date, valued at the rates recorded when it was first scanned.
    """
    from datetime import timedelta

    def _period_key(dt: datetime) -> str:
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # bucket by local date, not UTC
        if period == "monthly":
            return dt.strftime("%Y-%m")
        if period == "weekly":
            start = dt - timedelta(days=dt.weekday())
            return start.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")

    agg: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "cost": 0.0, "messages": 0,
    }))

    for info in _scanned_sessions():
        if not info["usage"]:
            continue
        ts = info["started"]
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        except ValueError:
            dt = None
        if dt is None:
            dt = datetime.fromtimestamp(info["mtime"])
        pk = _period_key(dt)
        model = info["model"] or UNKNOWN_MODEL
        short = model
        usage = info["usage"]
        entry = agg[pk][short]
        entry["input_tokens"] += usage.get("input_tokens", 0)
        entry["output_tokens"] += usage.get("output_tokens", 0)
        entry["cache_read_tokens"] += usage.get("cached_input_tokens", 0)
        entry["cost"] += info["cost"]
        entry["messages"] += 1

    result = []
    for pk in sorted(agg.keys(), reverse=True):
        models = agg[pk]
        result.append({
            "period": pk,
            "total_cost": sum(m["cost"] for m in models.values()),
            "total_input": sum(m["input_tokens"] for m in models.values()),
            "total_output": sum(m["output_tokens"] for m in models.values()),
            "total_cache": sum(m["cache_read_tokens"] for m in models.values()),
            "total_messages": sum(m["messages"] for m in models.values()),
            "models": dict(models),
        })
    return result


def get_all_period_usage() -> dict[str, list[dict]]:
    """All three period groupings (the underlying ledger read is memoized)."""
    return {p: get_period_usage(p) for p in ("daily", "weekly", "monthly")}


def _retarget_cwd(obj, old_cwd: str, new_cwd: str) -> bool:
    """Recursively move every ``cwd`` at or under ``old_cwd`` to ``new_cwd``."""
    from asm.services.migrate import _path_replacement

    changed = False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "cwd" and isinstance(v, str):
                moved = _path_replacement(v, old_cwd, new_cwd)
                if moved != v:
                    obj[k] = moved
                    changed = True
            elif isinstance(v, (dict, list)):
                changed = _retarget_cwd(v, old_cwd, new_cwd) or changed
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                changed = _retarget_cwd(item, old_cwd, new_cwd) or changed
    return changed


def _state_dbs() -> list[Path]:
    """Codex's thread state databases (``state_<n>.sqlite``) beside the sessions dir."""
    return sorted(CODEX_SESSIONS_DIR.parent.glob("state_*.sqlite"))


def move_session(rollout_path: str, new_cwd: str) -> bool:
    """Re-assign a Codex session to a different working directory (same id).

    Codex sessions are global, date-partitioned rollouts whose owning project is
    recorded as ``cwd`` in the session_meta and mirrored in the ``threads`` table
    of ``state_<n>.sqlite`` (what ``codex resume`` lists from). Both are rewritten
    together: DB rows are updated inside an open transaction that commits only
    after the rollout write succeeds. Sub-directory cwds keep their suffix. A
    recovery snapshot is taken first. Returns True if changed.
    """
    return _relocate(rollout_path, new_cwd, copy=False) is not None


def copy_session(rollout_path: str, new_cwd: str) -> str | None:
    """Duplicate a Codex session under ``new_cwd`` with a fresh thread id.

    The original is untouched. The copy is a new rollout file beside it plus a
    cloned ``threads`` row; its token counters are zeroed so asm never prices the
    same conversation twice. Returns the new id, or None on failure.
    """
    return _relocate(rollout_path, new_cwd, copy=True)


def _zero_usage(record: dict) -> None:
    payload = record.get("payload")
    if record.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
        return
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    for key in ("total_token_usage", "last_token_usage"):
        usage = info.get(key)
        if isinstance(usage, dict):
            for field in usage:
                if field != "total_tokens" and isinstance(usage[field], (int, float)):
                    usage[field] = 0


def _replace_value(obj, old: str, new: str) -> None:
    """Replace every string value equal to ``old`` (the thread id) with ``new``."""
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    for k, v in list(items):
        if v == old:
            obj[k] = new
        elif isinstance(v, (dict, list)):
            _replace_value(v, old, new)


def _relocate(rollout_path: str, new_cwd: str, copy: bool) -> str | None:
    """Shared move/copy: returns the session id in the new location, None on failure/no-op."""
    import os
    import sqlite3
    import uuid

    from asm.services.migrate import _path_replacement
    from asm.services.recovery import create_recovery_snapshot

    p = Path(rollout_path)
    new_cwd = os.path.normpath(new_cwd)
    if not p.exists():
        return None
    # Defense-in-depth: only operate on real files under the Codex sessions dir
    # (parity with the sibling trash function), even though callers pass scanned
    # rollouts. Uses the module-level dir so it follows test/patched roots.
    try:
        root = CODEX_SESSIONS_DIR.resolve()
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            logger.warning("Refusing to touch session outside Codex sessions dir: %s", rollout_path)
            return None
    except OSError:
        return None
    conns: list[sqlite3.Connection] = []
    target = p
    created = False
    try:
        records = []
        # split("\n"), not splitlines(): U+2028 etc. may appear inside JSON strings.
        for line in p.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise CodexDataError(f"malformed rollout: {p}") from exc
            if not isinstance(obj, dict):
                raise CodexDataError(f"unexpected rollout record: {p}")
            records.append(obj)
        meta = next((r.get("payload") for r in records if r.get("type") == "session_meta"), None)
        old_cwd = meta.get("cwd") if isinstance(meta, dict) else None
        old_id = (meta.get("id") or "") if isinstance(meta, dict) else ""
        if not old_cwd or (copy and not old_id):
            raise CodexDataError(f"rollout has no session_meta cwd/id: {p}")
        if not copy:  # a copy never touches the original
            from asm.services.live import ensure_codex_not_live

            ensure_codex_not_live([old_id], [p])
        changed = False
        for obj in records:
            changed = _retarget_cwd(obj, old_cwd, new_cwd) or changed
        new_id = old_id
        if copy:
            new_id = str(uuid.uuid4())
            for obj in records:
                _replace_value(obj, old_id, new_id)
                _zero_usage(obj)
            if not p.name.endswith(f"-{old_id}.jsonl"):
                raise CodexDataError(f"rollout name does not end with its id: {p}")
            target = p.with_name(p.name[: -len(f"{old_id}.jsonl")] + f"{new_id}.jsonl")
        elif not changed:
            return None
        for db in _state_dbs():
            conn = sqlite3.connect(db, timeout=5, isolation_level=None)
            conns.append(conn)
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'").fetchone():
                continue
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, cwd FROM threads WHERE id = ? OR rollout_path = ?", (old_id, str(p))
            ).fetchall()
            for row_id, row_cwd in rows:
                moved_cwd = _path_replacement(row_cwd, old_cwd, new_cwd)
                if not copy:
                    conn.execute("UPDATE threads SET cwd = ? WHERE id = ?", (moved_cwd, row_id))
                    continue
                cols = [r[1] for r in conn.execute("PRAGMA table_info(threads)")]
                overrides = {"id": new_id, "rollout_path": str(target), "cwd": moved_cwd}
                if "tokens_used" in cols:
                    overrides["tokens_used"] = 0
                select = ", ".join("?" if c in overrides else f'"{c}"' for c in cols)
                params = [overrides[c] for c in cols if c in overrides]
                quoted = ", ".join(f'"{c}"' for c in cols)
                conn.execute(
                    f"INSERT INTO threads ({quoted}) SELECT {select} FROM threads WHERE id = ?",
                    (*params, row_id),
                )
        text = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
        if copy:
            if target.exists():
                raise CodexDataError(f"copy target already exists: {target}")
            created = True
            _atomic_write_text(target, text)
        else:
            if not create_recovery_snapshot(p, "codex-session"):
                raise CodexDataError(f"recovery snapshot failed: {p}")
            _atomic_write_text(p, text)
        for conn in conns:
            if conn.in_transaction:
                conn.execute("COMMIT")
        refresh()
        return new_id
    except (OSError, CodexDataError, sqlite3.Error) as exc:
        from asm.services.live import LiveSessionError

        logger.warning("Codex session %s failed for %s: %s", "copy" if copy else "move", rollout_path, exc)
        for conn in conns:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        if created and target.exists():
            target.unlink()
        if isinstance(exc, LiveSessionError):
            raise  # the caller must tell the user why, not just "failed"
        return None
    finally:
        for conn in conns:
            conn.close()


# Columns that point at a thread; rollout_migration_state.last_checked_thread_id
# is Codex's own scan bookmark and is deliberately not included.
_THREAD_REF_COLUMNS = {"thread_id", "parent_thread_id", "child_thread_id"}
THREAD_ROWS_FILE = "codex_threads.json"


def _thread_refs(conn) -> list[tuple[str, str]]:
    """``(table, column)`` pairs referencing a thread id, ``threads`` first."""
    refs = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for col in (r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')):
            if (table == "threads" and col == "id") or col in _THREAD_REF_COLUMNS:
                refs.append((table, col))
    refs.sort(key=lambda r: r[0] != "threads")
    return refs


def _jsonable(value):
    import base64

    return {"b64": base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def _from_jsonable(value):
    import base64

    return base64.b64decode(value["b64"]) if isinstance(value, dict) and set(value) == {"b64"} else value


def begin_thread_removal(session_id: str, rollout_path: Path):
    """Delete a session's rows from every Codex state DB inside open transactions.

    Returns ``(connections, saved_rows)``; nothing is final until
    :func:`end_thread_removal` commits, so the caller can roll back if trashing
    the rollout file fails. ``saved_rows`` is JSON-ready for :func:`restore_thread_rows`.
    """
    import sqlite3

    conns: list = []
    saved: dict[str, dict[str, list[dict]]] = {}
    try:
        for db in _state_dbs():
            conn = sqlite3.connect(db, timeout=5, isolation_level=None)
            conns.append(conn)
            refs = _thread_refs(conn)
            if not any(table == "threads" for table, _col in refs):
                continue
            conn.execute("BEGIN IMMEDIATE")
            ids = {session_id} | {
                r[0] for r in conn.execute("SELECT id FROM threads WHERE rollout_path = ?", (str(rollout_path),))
            }
            marks = ",".join("?" * len(ids))
            rows: dict[str, list[dict]] = {}
            for table, col in refs:
                cur = conn.execute(f'SELECT * FROM "{table}" WHERE "{col}" IN ({marks})', tuple(ids))
                names = [d[0] for d in cur.description]
                rows.setdefault(table, []).extend(
                    {n: _jsonable(v) for n, v in zip(names, row)} for row in cur.fetchall()
                )
            for table, col in reversed(refs):  # children before the thread itself
                conn.execute(f'DELETE FROM "{table}" WHERE "{col}" IN ({marks})', tuple(ids))
            rows = {t: r for t, r in rows.items() if r}
            if rows:
                saved[db.name] = rows
    except Exception:
        end_thread_removal(conns, commit=False)
        raise
    return conns, saved


def end_thread_removal(conns, commit: bool) -> None:
    try:
        for conn in conns:
            if conn.in_transaction:
                conn.execute("COMMIT" if commit else "ROLLBACK")
    finally:
        for conn in conns:
            conn.close()


def restore_thread_rows(saved: dict) -> None:
    """Re-insert rows saved by :func:`begin_thread_removal` (existing rows are kept)."""
    import sqlite3

    for db_name, tables in saved.items():
        if "/" in db_name or not (db_name.startswith("state_") and db_name.endswith(".sqlite")):
            raise CodexDataError(f"unexpected Codex state DB name: {db_name}")
        db = CODEX_SESSIONS_DIR.parent / db_name
        if not db.exists():
            raise CodexDataError(f"Codex state DB {db_name} no longer exists; Codex will re-index the rollout")
        conn = sqlite3.connect(db, timeout=5, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in sorted(tables, key=lambda t: t != "threads"):
                cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                for row in tables[table]:
                    keys = [k for k in row if k in cols]
                    conn.execute(
                        f'INSERT OR IGNORE INTO "{table}" ({", ".join(f"{chr(34)}{k}{chr(34)}" for k in keys)}) '
                        f'VALUES ({", ".join("?" * len(keys))})',
                        [_from_jsonable(row[k]) for k in keys],
                    )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a same-dir temp file + os.replace so a crash can't truncate."""
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_project_cost_map() -> dict[str, float]:
    """Cumulative cost per working directory over the whole ledger."""
    agg: dict[str, float] = defaultdict(float)
    for info in _scanned_sessions():
        if info["usage"]:
            agg[info["cwd"] or "(unknown)"] += info["cost"]
    return dict(agg)


def get_usage_data() -> dict:
    """Per-project cost + model totals + daily session counts over the ledger."""
    project_costs: dict[str, dict] = {}
    model_totals: dict[str, dict] = {}
    sessions_by_day: dict[str, int] = defaultdict(int)
    total_cost = 0.0

    records = _scanned_sessions()
    for info in records:
        ts = info["started"]
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.fromtimestamp(info["mtime"])
        except ValueError:
            dt = datetime.fromtimestamp(info["mtime"])
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # local date, same bucketing as the period tables
        sessions_by_day[dt.strftime("%Y-%m-%d")] += 1

        usage = info["usage"]
        if not usage:
            continue
        model = info["model"] or UNKNOWN_MODEL
        cost = info["cost"]
        total_cost += cost
        cwd = info["cwd"] or "(unknown)"
        pc = project_costs.setdefault(cwd, {"path": cwd, "name": Path(cwd).name or cwd, "cost": 0.0, "duration": 0})
        pc["cost"] += cost
        mt = model_totals.setdefault(model, {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "costUSD": 0})
        mt["inputTokens"] += usage.get("input_tokens", 0)
        mt["outputTokens"] += usage.get("output_tokens", 0)
        mt["cacheReadInputTokens"] += usage.get("cached_input_tokens", 0)
        mt["costUSD"] += cost

    costs = sorted(project_costs.values(), key=lambda x: x["cost"], reverse=True)
    return {
        "total_cost": total_cost,
        "project_costs": costs[:TOP_PROJECT_LIMIT],
        "model_totals": model_totals,
        "sessions_by_day": dict(sorted(sessions_by_day.items(), reverse=True)[:RECENT_DAYS_LIMIT]),
        "total_sessions_ever": len(records),
        "first_use": "",
        "num_startups": 0,
    }


def get_stats() -> Stats:
    projects = get_projects()
    return Stats(
        total_projects=len(projects),
        total_sessions=total_session_count(),
    )
