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
from asm.utils import RECENT_DAYS_LIMIT, SUMMARY_MAX_CHARS, TOP_PROJECT_LIMIT

logger = logging.getLogger(__name__)

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
    except (PermissionError, OSError):
        return []
    files.sort(key=lambda f: _safe_mtime(f), reverse=True)
    return files[:limit] if limit else files


def _safe_mtime(f: Path) -> float:
    try:
        return f.stat().st_mtime
    except OSError:
        return 0.0


def total_session_count() -> int:
    if not CODEX_SESSIONS_DIR.exists():
        return 0
    try:
        return sum(1 for _ in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))
    except (PermissionError, OSError):
        return 0


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
        if Path(record["path"]).resolve().is_relative_to(root)
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
                except json.JSONDecodeError:
                    continue
                typ = obj.get("type")
                payload = obj.get("payload", {})
                if typ == "session_meta":
                    meta = payload
                    model = payload.get("model", "") or model
                elif typ == "turn_context":
                    # The authoritative model id lives here (session_meta only
                    # carries model_provider). Last turn wins on mid-session switch.
                    model = payload.get("model", "") or model
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    usage = info.get("total_token_usage")
                    if usage:
                        last_usage = usage
                    if not model:
                        model = info.get("model", "") or model
                elif not first_prompt and typ == "response_item" and payload.get("type") == "message" \
                        and payload.get("role") == "user":
                    text = _extract_input_text(payload.get("content", []))
                    if text and not text.startswith("#") and not text.startswith("<"):
                        first_prompt = text[:SUMMARY_MAX_CHARS]
    except OSError:
        return None
    if meta is None:
        return None
    git = meta.get("git") or {}
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
    except OSError:
        return 0


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
    for p in paths:
        f = Path(p)
        if not f.exists():
            continue
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
    path = Path(project_dir) if project_dir else _find_rollout(session_id)
    if not path or not path.exists():
        return []
    messages: list[dict] = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload", {})
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _extract_message_text(payload.get("content", []))
                if text:
                    messages.append({"type": role, "content": text})
    except OSError:
        return []
    return messages[-limit:] if len(messages) > limit else messages


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
    for f in _rollout_files():
        if session_id in f.name:
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


def _retarget_cwd(obj, new_cwd: str) -> bool:
    """Recursively set every ``cwd`` string field in a JSON object to new_cwd."""
    changed = False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "cwd" and isinstance(v, str):
                obj[k] = new_cwd
                changed = True
            elif isinstance(v, (dict, list)):
                changed = _retarget_cwd(v, new_cwd) or changed
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                changed = _retarget_cwd(item, new_cwd) or changed
    return changed


def move_session(rollout_path: str, new_cwd: str) -> bool:
    """Re-assign a Codex session to a different working directory.

    Codex sessions are global, date-partitioned rollouts whose owning project is
    recorded as ``cwd`` in the session_meta (Codex resumes by cwd, e.g.
    ``codex resume --cd <dir>``). "Moving" rewrites that cwd; the file stays in
    its date folder. A recovery snapshot is taken first. Returns True if changed.
    """
    from asm.services.recovery import create_recovery_snapshot

    p = Path(rollout_path)
    if not p.exists():
        return False
    # Defense-in-depth: only operate on real files under the Codex sessions dir
    # (parity with the sibling trash function), even though callers pass scanned
    # rollouts. Uses the module-level dir so it follows test/patched roots.
    try:
        root = CODEX_SESSIONS_DIR.resolve()
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            logger.warning("Refusing to move session outside Codex sessions dir: %s", rollout_path)
            return False
    except OSError:
        return False
    try:
        create_recovery_snapshot(p, "codex-session")
        out, changed = [], False
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            if _retarget_cwd(obj, new_cwd):
                changed = True
            out.append(json.dumps(obj, ensure_ascii=False))
        if changed:
            _atomic_write_text(p, "\n".join(out) + "\n")
            refresh()
        return changed
    except OSError:
        return False


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a same-dir temp file + os.replace so a crash can't truncate."""
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
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
