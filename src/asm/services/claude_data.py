"""Service for reading Claude Code data from filesystem and SDK."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

_SDK_FALLBACK_EXCEPTIONS = (
    ImportError,
    ModuleNotFoundError,
    OSError,
    ValueError,
    TypeError,
    AttributeError,
)


from asm.utils import RECENT_DAYS_LIMIT, SUMMARY_MAX_CHARS, TOP_PROJECT_LIMIT
from asm.utils import dir_size as _dir_size

from asm.models import (
    CLAUDE_DIR,
    CLAUDE_JSON,
    DEBUG_DIR,
    FILE_HISTORY_DIR,
    PROJECTS_DIR,
    SESSION_ENV_DIR,
    TASKS_DIR,
    TODOS_DIR,
    DebugEntry,
    FileHistoryEntry,
    ProjectInfo,
    SessionDetail,
    SessionInfo,
    Stats,
    TodoEntry,
    decode_path_hint,
    encode_path,
)
from asm.utils import validate_identifier


class ClaudeDataError(RuntimeError):
    pass


def _file_count(path: Path) -> int:
    """Count files and directories in a path."""
    try:
        return sum(1 for _ in path.iterdir())
    except (PermissionError, OSError) as exc:
        raise ClaudeDataError(f"cannot read {path}: {exc}") from exc


def _get_session_envs(project_path: str) -> list[str]:
    """Find session-env directories associated with a project path."""
    encoded = encode_path(project_path)
    envs = []
    if SESSION_ENV_DIR.exists():
        try:
            for d in SESSION_ENV_DIR.iterdir():
                if d.is_dir() and encoded in d.name:
                    envs.append(d.name)
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {SESSION_ENV_DIR}: {exc}") from exc
    return envs


def load_claude_json() -> dict:
    """Load and parse .claude.json."""
    if not CLAUDE_JSON.exists():
        return {}
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise ClaudeDataError(f"cannot read {CLAUDE_JSON}: {exc}") from exc
    if not isinstance(data, dict):
        raise ClaudeDataError(f"unexpected shape in {CLAUDE_JSON}: expected object")
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        raise ClaudeDataError(f"unexpected projects shape in {CLAUDE_JSON}")
    if any(not isinstance(config, dict) for config in projects.values()):
        raise ClaudeDataError(f"unexpected project entry in {CLAUDE_JSON}")
    return data


def get_projects() -> list[ProjectInfo]:
    """Projects from .claude.json plus session dirs whose project isn't registered.

    Claude only registers a path in .claude.json once it runs there, so sessions
    moved into a new project (or kept after a config cleanup) would otherwise be
    invisible to listing and search.
    """
    data = load_claude_json()
    projects_data = data.get("projects", {})
    result = []
    for path_str, config in projects_data.items():
        exists = Path(path_str).exists()
        result.append(
            ProjectInfo(
                path=path_str,
                exists=exists,
                last_cost=config.get("lastCost"),
                last_duration=config.get("lastDuration"),
                session_env_dirs=_get_session_envs(path_str),
            )
        )
    registered = _build_encoded_to_paths_map()
    if PROJECTS_DIR.exists():
        try:
            dirs = sorted(d for d in PROJECTS_DIR.iterdir() if d.is_dir() and d.name not in registered)
        except OSError as exc:
            raise ClaudeDataError(f"cannot read {PROJECTS_DIR}: {exc}") from exc
        for d in dirs:
            cwd = _dir_project_path(d)
            if cwd:
                result.append(ProjectInfo(path=cwd, exists=Path(cwd).exists()))
    return result


def _dir_project_path(project_dir: Path, max_lines: int = 200) -> str | None:
    """The project path a session dir belongs to, from a recorded ``cwd`` whose
    encoding matches the dir name (sub-dir cwds don't match and are skipped)."""
    try:
        sessions = sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        for jsonl in sessions[:5]:
            with open(jsonl, "rb") as f:
                for i, line in enumerate(f):
                    if i >= max_lines:
                        break
                    if b'"cwd"' not in line:
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except (json.JSONDecodeError, UnicodeError, AttributeError):
                        continue
                    if isinstance(cwd, str) and cwd and encode_path(cwd) == project_dir.name:
                        return cwd
    except OSError as exc:
        raise ClaudeDataError(f"cannot read {project_dir}: {exc}") from exc
    return None


def get_project_paths() -> set[str]:
    """Get set of all project paths from .claude.json."""
    data = load_claude_json()
    return set(data.get("projects", {}).keys())


_encoded_map_cache: tuple[tuple, dict[str, list[str]]] | None = None


def _build_encoded_to_paths_map() -> dict[str, list[str]]:
    """Build encoded project-dir name -> project paths mapping.

    Memoized on the .claude.json stat: the UI asks for it once per tree node,
    and re-parsing the (often 500KB+) file each time froze the filter box.
    """
    global _encoded_map_cache
    try:
        st = CLAUDE_JSON.stat()
        key = (str(CLAUDE_JSON), st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        key = (str(CLAUDE_JSON), None, None)
    if _encoded_map_cache is not None and _encoded_map_cache[0] == key:
        return {k: list(v) for k, v in _encoded_map_cache[1].items()}
    encoded_to_paths: dict[str, list[str]] = defaultdict(list)
    for path_str in sorted(get_project_paths()):
        encoded_to_paths[encode_path(path_str)].append(path_str)
    _encoded_map_cache = (key, dict(encoded_to_paths))
    return {k: list(v) for k, v in encoded_to_paths.items()}


def project_paths_for_encoded(dir_name: str) -> list[str]:
    return _build_encoded_to_paths_map().get(dir_name, [])


def _resolve_project_dir(project_ref: str | None) -> Path | None:
    """Resolve a project path or already-encoded directory name to a projects dir.

    ``project_ref`` may be either an absolute project path (from .claude.json) or
    an already-encoded directory name (e.g. ``-home-bch-foo``). Note that
    ``PROJECTS_DIR / "/abs/path"`` collapses to ``/abs/path`` in pathlib (an
    absolute right-hand operand discards the left), so we must encode an absolute
    path *before* joining — otherwise it resolves to the real project source dir,
    which has no session files.
    """
    if not project_ref:
        return None
    # Already-encoded directory name (relative, no leading slash).
    if not project_ref.startswith("/"):
        validate_identifier(project_ref, "encoded project directory")
        direct = PROJECTS_DIR / project_ref
        if direct.exists() and direct.parent == PROJECTS_DIR:
            return direct
    encoded = encode_path(project_ref)
    matches = project_paths_for_encoded(encoded)
    if len(matches) > 1:
        raise ClaudeDataError(f"ambiguous encoded project directory: {encoded}")
    candidate = PROJECTS_DIR / encoded
    if candidate.exists():
        return candidate
    return None


def _project_label_for_dir(dir_name: str, encoded_to_paths: dict[str, list[str]]) -> str:
    """Return a readable project label for a Claude projects dir."""
    matches = encoded_to_paths.get(dir_name, [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return f"[ambiguous] {' | '.join(matches)}"
    return _dir_project_path(PROJECTS_DIR / dir_name) or decode_path_hint(dir_name)


def get_sessions() -> list[SessionInfo]:
    """Get all session data directories."""
    if not PROJECTS_DIR.exists():
        return []
    result = []
    encoded_to_paths = _build_encoded_to_paths_map()
    try:
        for d in sorted(PROJECTS_DIR.iterdir()):
            if not d.is_dir():
                continue
            # Unregistered but its project still exists on disk → live, not orphaned.
            is_orphaned = d.name not in encoded_to_paths and not (
                (cwd := _dir_project_path(d)) and Path(cwd).exists()
            )

            result.append(
                SessionInfo(
                    dir_name=d.name,
                    actual_path=str(d),
                    size_bytes=0,  # Calculated lazily in UI
                    file_count=_file_count(d),
                    session_env_dirs=_find_session_envs_for_dir(d.name),
                    is_orphaned=is_orphaned,
                )
            )
    except (PermissionError, OSError) as exc:
        raise ClaudeDataError(f"cannot read {PROJECTS_DIR}: {exc}") from exc
    return result


def _find_session_envs_for_dir(dir_name: str) -> list[str]:
    """Find session-env dirs matching a project dir name."""
    envs = []
    if SESSION_ENV_DIR.exists():
        try:
            for d in SESSION_ENV_DIR.iterdir():
                if d.is_dir() and (d.name == dir_name or d.name.startswith(dir_name + "-")):
                    envs.append(d.name)
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {SESSION_ENV_DIR}: {exc}") from exc
    return envs


def get_session_details(project_dir: str | None = None) -> list[SessionDetail]:
    """Get detailed session info using SDK if available, fallback to JSONL parsing."""
    try:
        return _get_sessions_via_sdk(project_dir)
    except _SDK_FALLBACK_EXCEPTIONS:
        logger.debug("SDK path failed for get_session_details, falling back to JSONL", exc_info=True)
        return _get_sessions_via_jsonl(project_dir)


_SDK_SESSION_LIMIT = 100


def _get_sessions_via_sdk(project_dir: str | None) -> list[SessionDetail]:
    """Use claude-agent-sdk to list sessions."""
    from claude_agent_sdk import list_sessions

    sessions = list_sessions(directory=project_dir, limit=_SDK_SESSION_LIMIT)
    result = []
    for s in sessions:
        result.append(
            SessionDetail(
                session_id=s.session_id,
                summary=s.summary or "",
                last_modified=s.last_modified or 0,
                file_size=s.file_size or 0,
                first_prompt=s.first_prompt or "",
                git_branch=s.git_branch or "",
                cwd=s.cwd or "",
                project_dir=project_dir or "",
            )
        )
    return result


def _get_sessions_via_jsonl(project_dir: str | None) -> list[SessionDetail]:
    """Fallback: parse JSONL session files directly."""
    search_dirs = []
    if project_dir:
        p = _resolve_project_dir(project_dir)
        if p and p.exists():
            search_dirs.append(p)
    else:
        if PROJECTS_DIR.exists():
            search_dirs = [d for d in PROJECTS_DIR.iterdir() if d.is_dir()]

    result = []
    for d in search_dirs:
        for jsonl in d.glob("*.jsonl"):
            try:
                stat = jsonl.stat()
                first_prompt = ""
                session_id = jsonl.stem
                # Read first user message for summary
                with open(jsonl) as f:
                    for line in f:
                        try:
                            msg = json.loads(line)
                            if not isinstance(msg, dict):
                                raise ClaudeDataError(f"unexpected transcript record: {jsonl}")
                            if msg.get("type") == "user":
                                message = msg.get("message", {})
                                if not isinstance(message, dict):
                                    raise ClaudeDataError(f"unexpected message shape: {jsonl}")
                                content = message.get("content", "")
                                if isinstance(content, str):
                                    first_prompt = content[:SUMMARY_MAX_CHARS]
                                elif isinstance(content, list):
                                    for block in content:
                                        if isinstance(block, dict) and block.get("type") == "text":
                                            first_prompt = block.get("text", "")[:SUMMARY_MAX_CHARS]
                                            break
                                break
                        except (json.JSONDecodeError, UnicodeError) as exc:
                            raise ClaudeDataError(f"malformed transcript: {jsonl}") from exc
                result.append(
                    SessionDetail(
                        session_id=session_id,
                        summary=first_prompt,
                        last_modified=stat.st_mtime,
                        file_size=stat.st_size,
                        first_prompt=first_prompt,
                        cwd="",
                        project_dir=d.name,
                    )
                )
            except (OSError, UnicodeError) as exc:
                raise ClaudeDataError(f"cannot read {jsonl}: {exc}") from exc
    result.sort(key=lambda s: s.last_modified, reverse=True)
    return result


def get_session_messages(session_id: str, project_dir: str | None = None, limit: int = 50) -> list[dict]:
    """Get messages from a session. Uses SDK if available, fallback to JSONL."""
    validate_identifier(session_id, "session id")
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if project_dir and _resolve_project_dir(project_dir):
        return _get_messages_via_jsonl(session_id, project_dir, limit)
    try:
        return _get_messages_via_sdk(session_id, project_dir, limit)
    except _SDK_FALLBACK_EXCEPTIONS:
        logger.debug("SDK path failed for get_session_messages, falling back to JSONL", exc_info=True)
        return _get_messages_via_jsonl(session_id, project_dir, limit)


def _get_messages_via_sdk(session_id: str, project_dir: str | None, limit: int) -> list[dict]:
    """Use SDK to get session messages."""
    from claude_agent_sdk import get_session_messages as sdk_get_messages

    messages = sdk_get_messages(session_id=session_id, directory=project_dir, limit=limit)
    return [
        {
            "type": m.type,
            "content": _extract_text_content(m.message) if hasattr(m, "message") else str(m),
        }
        for m in messages
    ]


def _get_messages_via_jsonl(session_id: str, project_dir: str | None, limit: int) -> list[dict]:
    """Fallback: parse JSONL directly."""
    # Find the JSONL file
    jsonl_path = None
    if project_dir:
        project_root = _resolve_project_dir(project_dir)
        candidate = project_root / f"{session_id}.jsonl" if project_root else None
        if candidate and candidate.exists():
            jsonl_path = candidate
    else:
        if PROJECTS_DIR.exists():
            for d in PROJECTS_DIR.iterdir():
                candidate = d / f"{session_id}.jsonl"
                if candidate.exists():
                    jsonl_path = candidate
                    break

    if not jsonl_path:
        return []

    messages = []
    ai_title = ""
    meta_lines: list[str] = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise ClaudeDataError(f"malformed transcript: {jsonl_path}") from exc
                if not isinstance(msg, dict):
                    raise ClaudeDataError(f"unexpected transcript record: {jsonl_path}")
                msg_type = msg.get("type")
                if msg_type in ("user", "assistant"):
                    if not isinstance(msg.get("message"), (dict, str)):
                        raise ClaudeDataError(f"unexpected message shape: {jsonl_path}")
                    content = _extract_text_content(msg.get("message", {}))
                    if content:
                        messages.append({"type": msg_type, "content": content})
                elif msg_type == "ai-title":
                    ai_title = msg.get("aiTitle", "") or ai_title
                elif msg_type == "pr-link":
                    url = msg.get("url") or msg.get("prLink") or ""
                    if url:
                        meta_lines.append(f"PR: {url}")
    except (OSError, UnicodeError) as exc:
        raise ClaudeDataError(f"cannot read {jsonl_path}: {exc}") from exc

    if messages:
        return messages[-limit:] if limit and len(messages) > limit else messages

    # No conversation turns — surface whatever metadata exists so the preview is
    # informative instead of blank (these are title/PR/automation-only sessions).
    if ai_title or meta_lines:
        info = "[no conversation — metadata only]"
        if ai_title:
            info += f"\n\nTitle: {ai_title}"
        if meta_lines:
            info += "\n" + "\n".join(meta_lines)
        return [{"type": "meta", "content": info}]
    return []


def _extract_text_content(message: dict | str) -> str:
    """Extract text content from a message object."""
    if isinstance(message, str):
        return message
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    texts.append(f"[Tool: {block.get('name', '?')}]")
                elif block.get("type") == "tool_result":
                    texts.append("[Tool Result]")
        return "\n".join(texts)
    return str(content)


def get_file_history() -> list[FileHistoryEntry]:
    """Get file history entries."""
    if not FILE_HISTORY_DIR.exists():
        return []
    active_session_ids = _get_active_session_ids()
    result = []
    try:
        for d in sorted(FILE_HISTORY_DIR.iterdir()):
            if not d.is_dir():
                continue
            is_orphaned = d.name not in active_session_ids
            result.append(
                FileHistoryEntry(
                    dir_name=d.name,
                    path=str(d),
                    size_bytes=0,
                    is_orphaned=is_orphaned,
                )
            )
    except (PermissionError, OSError) as exc:
        raise ClaudeDataError(f"cannot read {FILE_HISTORY_DIR}: {exc}") from exc
    return result


def get_debug_files() -> list[DebugEntry]:
    """Get debug file entries."""
    if not DEBUG_DIR.exists():
        return []
    # Build set of active session IDs for orphan detection
    active_session_ids = _get_active_session_ids()
    result = []
    try:
        for f in sorted(DEBUG_DIR.iterdir()):
            size = f.stat().st_size if f.is_file() else _dir_size(f)
            is_orphaned = f.stem not in active_session_ids
            result.append(
                DebugEntry(name=f.name, path=str(f), size_bytes=size, is_orphaned=is_orphaned)
            )
    except (PermissionError, OSError) as exc:
        raise ClaudeDataError(f"cannot read {DEBUG_DIR}: {exc}") from exc
    return result


def get_todos() -> list[TodoEntry]:
    """Get per-session todo/task entries.

    Claude Code >= 2.1 stores task lists under ``tasks/<sessionId>/<n>.json``
    (one directory per session). Older versions used flat ``todos/*.json`` files.
    Each session directory (or legacy file) becomes one entry.
    """
    active_session_ids = _get_active_session_ids()
    result = []
    if TASKS_DIR.exists():
        try:
            for d in sorted(TASKS_DIR.iterdir()):
                if not d.is_dir():
                    continue
                session_id = d.name
                is_orphaned = session_id not in active_session_ids
                result.append(
                    TodoEntry(name=session_id, path=str(d), size_bytes=_dir_size(d), is_orphaned=is_orphaned)
                )
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {TASKS_DIR}: {exc}") from exc
        if result:
            return result
    # Legacy fallback: flat todos/<sessionId>-agent-<id>.json files.
    if not TODOS_DIR.exists():
        return []
    try:
        for f in sorted(TODOS_DIR.iterdir()):
            size = f.stat().st_size if f.is_file() else _dir_size(f)
            session_id = f.stem.split("-agent-")[0] if "-agent-" in f.stem else f.stem
            is_orphaned = session_id not in active_session_ids
            result.append(
                TodoEntry(name=f.name, path=str(f), size_bytes=size, is_orphaned=is_orphaned)
            )
    except (PermissionError, OSError) as exc:
        raise ClaudeDataError(f"cannot read {TODOS_DIR}: {exc}") from exc
    return result


_active_ids_cache: set[str] | None = None


def _get_active_session_ids() -> set[str]:
    """Get set of all active session IDs from project directories.

    Uses a recursive walk so nested (e.g. subagent) session files are also
    counted — otherwise their task/file-history entries look orphaned. Cached
    because file-history/debug/todos getters each call this on every refresh;
    cleared by refresh_usage_cache().
    """
    global _active_ids_cache
    if _active_ids_cache is not None:
        return _active_ids_cache
    ids: set[str] = set()
    if PROJECTS_DIR.exists():
        try:
            for d in PROJECTS_DIR.iterdir():
                if d.is_dir():
                    for jsonl in d.rglob("*.jsonl"):
                        ids.add(jsonl.stem)
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {PROJECTS_DIR}: {exc}") from exc
    _active_ids_cache = ids
    return ids


def get_session_to_project_map() -> dict[str, str]:
    """Build session_id → project_path mapping."""
    mapping: dict[str, str] = {}
    encoded_to_paths = _build_encoded_to_paths_map()
    if PROJECTS_DIR.exists():
        try:
            for d in PROJECTS_DIR.iterdir():
                if d.is_dir():
                    project_path = _project_label_for_dir(d.name, encoded_to_paths)
                    for jsonl in d.glob("*.jsonl"):
                        mapping[jsonl.stem] = project_path
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {PROJECTS_DIR}: {exc}") from exc
    return mapping


def get_stats() -> Stats:
    """Calculate overall statistics.

    Computes shared data once to avoid redundant file parsing.
    """
    projects = get_projects()
    sessions = get_sessions()

    # Compute active session IDs once for orphan detection
    active_ids = _get_active_session_ids()

    # Count file history
    fh_total, fh_orphaned = 0, 0
    if FILE_HISTORY_DIR.exists():
        try:
            for d in FILE_HISTORY_DIR.iterdir():
                if d.is_dir():
                    fh_total += 1
                    if d.name not in active_ids:
                        fh_orphaned += 1
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {FILE_HISTORY_DIR}: {exc}") from exc

    # Count debug files
    db_total, db_orphaned = 0, 0
    if DEBUG_DIR.exists():
        try:
            for f in DEBUG_DIR.iterdir():
                db_total += 1
                if f.stem not in active_ids:
                    db_orphaned += 1
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {DEBUG_DIR}: {exc}") from exc

    # Count todo/task entries (tasks/<sessionId>/ dirs, or legacy todos/ files)
    td_total, td_orphaned = 0, 0
    task_dirs = [d for d in TASKS_DIR.iterdir() if d.is_dir()] if TASKS_DIR.exists() else []
    if task_dirs:
        try:
            for d in task_dirs:
                td_total += 1
                if d.name not in active_ids:
                    td_orphaned += 1
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {TASKS_DIR}: {exc}") from exc
    elif TODOS_DIR.exists():
        try:
            for f in TODOS_DIR.iterdir():
                td_total += 1
                session_id = f.stem.split("-agent-")[0] if "-agent-" in f.stem else f.stem
                if session_id not in active_ids:
                    td_orphaned += 1
        except (PermissionError, OSError) as exc:
            raise ClaudeDataError(f"cannot read {TODOS_DIR}: {exc}") from exc

    return Stats(
        total_projects=len(projects),
        total_sessions=len(sessions),
        total_file_history=fh_total,
        total_debug=db_total,
        total_todos=td_total,
        orphaned_sessions=sum(1 for s in sessions if s.is_orphaned),
        orphaned_file_history=fh_orphaned,
        orphaned_debug=db_orphaned,
        orphaned_todos=td_orphaned,
        claude_dir_size=_dir_size(CLAUDE_DIR) if CLAUDE_DIR.exists() else 0,
        projects_dir_size=_dir_size(PROJECTS_DIR) if PROJECTS_DIR.exists() else 0,
    )


# Cached result of the expensive scan that parses token usage out of every
# session JSONL. Reused across daily/weekly/monthly so one screen refresh scans
# the tree once instead of three times. Cleared by refresh_usage_cache().
_usage_scan_cache: dict[str, dict] | None = None


def refresh_usage_cache() -> None:
    """Drop cached scans (token-usage + active-session-ids); call on refresh."""
    global _usage_scan_cache, _active_ids_cache
    _usage_scan_cache = None
    _active_ids_cache = None


def _collect_msg_usage() -> dict[str, dict]:
    """Usage records from the persistent ledger (incrementally updated).

    Returns {message_id: {usage, model, ts_str, project_dir, cost}} — ``cost``
    is the value frozen when the message was first scanned, so old sessions
    keep the rates that applied back then. Records survive file deletion.
    """
    global _usage_scan_cache
    if _usage_scan_cache is not None:
        return _usage_scan_cache
    from asm.services import ledger

    ledger.update_claude()
    _usage_scan_cache = ledger.claude_records()
    return _usage_scan_cache


def _period_key(dt, period: str) -> str:
    from datetime import timedelta
    # Bucket by LOCAL date (timestamps are UTC). Otherwise KST etc. mis-attribute
    # early-morning / late-night activity to the wrong day, and disagree with the
    # history.jsonl day counts (which use local time).
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    if period == "monthly":
        return dt.strftime("%Y-%m")
    if period == "weekly":
        start = dt - timedelta(days=dt.weekday())  # ISO week start (Monday)
        return start.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def _aggregate_period(msg_last: dict[str, dict], period: str) -> list[dict]:
    from collections import defaultdict
    from datetime import datetime

    from asm.services.pricing import display_model as _display_model

    agg: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(lambda: {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_create_tokens": 0,
        "cost": 0.0, "messages": 0,
    }))
    for info in msg_last.values():
        try:
            dt = datetime.fromisoformat(info["ts_str"].replace("Z", "+00:00"))
        except ValueError:
            continue
        usage = info["usage"]
        model = info["model"]
        pk = _period_key(dt, period)
        short_model = _display_model(model).removeprefix("claude-")
        entry = agg[pk][short_model]
        entry["input_tokens"] += usage.get("input_tokens", 0)
        entry["output_tokens"] += usage.get("output_tokens", 0)
        entry["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
        entry["cache_create_tokens"] += usage.get("cache_creation_input_tokens", 0)
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


def get_period_usage(period: str = "daily") -> list[dict]:
    """Token usage grouped by period ('daily' | 'weekly' | 'monthly')."""
    return _aggregate_period(_collect_msg_usage(), period)


def get_all_period_usage() -> dict[str, list[dict]]:
    """All three period groupings from a single scan (for the dashboard)."""
    msg_last = _collect_msg_usage()
    return {p: _aggregate_period(msg_last, p) for p in ("daily", "weekly", "monthly")}


def get_usage_data() -> dict:
    """Get usage/cost data.

    Cost / model / per-project totals are derived from the same full session
    scan that feeds the period tables (so the dashboard headline and the tables
    agree). Session-per-day counts come from history.jsonl.
    """
    from collections import defaultdict
    from datetime import datetime

    from asm.services.pricing import display_model

    data = load_claude_json()
    encoded_to_paths = _build_encoded_to_paths_map()

    # Aggregate the deduplicated usage scan by project and by model.
    proj_agg: dict[str, float] = defaultdict(float)
    model_totals: dict[str, dict] = {}
    total_cost = 0.0
    for info in _collect_msg_usage().values():
        usage = info["usage"]
        model = info["model"]
        cost = info["cost"]
        total_cost += cost
        proj_agg[info.get("project_dir", "")] += cost
        mt = model_totals.setdefault(
            display_model(model),
            {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "costUSD": 0},
        )
        mt["inputTokens"] += usage.get("input_tokens", 0)
        mt["outputTokens"] += usage.get("output_tokens", 0)
        mt["cacheReadInputTokens"] += usage.get("cache_read_input_tokens", 0)
        mt["costUSD"] += cost

    project_costs = []
    for dir_name, cost in proj_agg.items():
        if cost <= 0:
            continue
        label = _project_label_for_dir(dir_name, encoded_to_paths)
        project_costs.append({
            "path": label,
            "name": Path(label).name or label,
            "cost": cost,
            "duration": 0,
        })
    project_costs.sort(key=lambda x: x["cost"], reverse=True)

    # Daily sessions from history.jsonl
    sessions_by_day: dict[str, int] = defaultdict(int)
    history_path = CLAUDE_DIR / "history.jsonl"
    if history_path.exists():
        try:
            with open(history_path) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        ts = d.get("timestamp", 0)
                        if ts:
                            dt = datetime.fromtimestamp(ts / 1000)
                            sessions_by_day[dt.strftime("%Y-%m-%d")] += 1
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            pass

    return {
        "total_cost": total_cost,
        "project_costs": project_costs[:TOP_PROJECT_LIMIT],
        "model_totals": model_totals,
        "sessions_by_day": dict(sorted(sessions_by_day.items(), reverse=True)[:RECENT_DAYS_LIMIT]),
        "total_sessions_ever": sum(sessions_by_day.values()),
        "first_use": data.get("firstStartTime", ""),
        "num_startups": data.get("numStartups", 0),
    }


def get_project_cost_map() -> dict[str, float]:
    """Cumulative cost per project path over the whole ledger."""
    encoded_to_paths = _build_encoded_to_paths_map()
    agg: dict[str, float] = defaultdict(float)
    for info in _collect_msg_usage().values():
        agg[info.get("project_dir", "")] += info["cost"]
    return {
        _project_label_for_dir(d, encoded_to_paths): c for d, c in agg.items() if c > 0
    }


def get_project_sessions(project_path: str) -> list[SessionDetail]:
    """Get sessions for one project.

    Claude Code >= 2.1 maintains a ``sessions-index.json`` per project dir with
    rich metadata (summary, firstPrompt, git branch, mtime). We prefer it and
    fall back to parsing the JSONL files directly for older layouts.
    """
    project_dir = _resolve_project_dir(project_path)
    if not project_dir:
        return []
    encoded = project_dir.name

    # The actual .jsonl files are the source of truth (those are viewable);
    # sessions-index.json only enriches them with summary/branch metadata.
    index = _load_session_index(project_dir)

    result = []
    fresh: list[tuple[str, int, int, str, str]] = []
    for jsonl in project_dir.glob("*.jsonl"):
        try:
            stat = jsonl.stat()
        except OSError as exc:
            raise ClaudeDataError(f"cannot stat {jsonl}: {exc}") from exc
        session_id = jsonl.stem
        meta = index.get(session_id)
        if meta:
            summary = meta.get("summary") or meta.get("firstPrompt") or f"(session {session_id[:8]})"
            first_prompt = (meta.get("firstPrompt") or "")[:SUMMARY_MAX_CHARS]
            git_branch = meta.get("gitBranch", "") or ""
        else:
            first_prompt, ai_title = _cached_summary(jsonl, stat, fresh)
            summary = ai_title or first_prompt or f"(session {session_id[:8]})"
            git_branch = ""
        result.append(
            SessionDetail(
                session_id=session_id,
                summary=summary[:SUMMARY_MAX_CHARS],
                last_modified=stat.st_mtime,
                file_size=stat.st_size,
                first_prompt=first_prompt,
                git_branch=git_branch,
                project_dir=encoded,
            )
        )
    if fresh:
        from asm.services import ledger

        ledger.store_claude_titles(fresh)
    result.sort(key=lambda s: s.last_modified, reverse=True)
    return result


_title_cache: dict[str, tuple[int, int, str, str]] | None = None
_title_lock = threading.Lock()


def _cached_summary(jsonl: Path, stat: os.stat_result, fresh: list) -> tuple[str, str]:
    """(first_prompt, ai_title), re-scanning the file only when its mtime/size changed."""
    global _title_cache
    from asm.services import ledger

    with _title_lock:
        if _title_cache is None:
            _title_cache = ledger.claude_titles()
        hit = _title_cache.get(str(jsonl))
    if hit and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
        return hit[2], hit[3]
    first_prompt, ai_title = _scan_jsonl_summary(jsonl)
    row = (stat.st_mtime_ns, stat.st_size, first_prompt, ai_title)
    with _title_lock:
        _title_cache[str(jsonl)] = row
    fresh.append((str(jsonl), *row))
    return first_prompt, ai_title


def _load_session_index(project_dir: Path) -> dict[str, dict]:
    """Load sessions-index.json into a {sessionId: entry} map (empty if absent)."""
    index_path = project_dir / "sessions-index.json"
    if not index_path.exists():
        return {}
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise ClaudeDataError(f"cannot read {index_path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
        raise ClaudeDataError(f"unexpected session index shape: {index_path}")
    entries = data.get("entries", [])
    if any(not isinstance(entry, dict) for entry in entries):
        raise ClaudeDataError(f"unexpected session index entry: {index_path}")
    return {
        e["sessionId"]: e
        for e in entries
        if isinstance(e, dict) and e.get("sessionId")
    }


def _scan_jsonl_summary(jsonl: Path) -> tuple[str, str]:
    """Return (first_prompt, ai_title) by scanning a session JSONL file."""
    first_prompt = ""
    ai_title = ""
    try:
        with open(jsonl, "rb") as f:
            for line in f:
                # Byte prefilter: sessions without an ai-title are read to the end,
                # so only lines that can matter are decoded and parsed.
                if not (b"ai-title" in line and not ai_title) and not (b'"user"' in line and not first_prompt):
                    continue
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise ClaudeDataError(f"malformed transcript: {jsonl}") from exc
                if not isinstance(msg, dict):
                    raise ClaudeDataError(f"unexpected transcript record: {jsonl}")
                mtype = msg.get("type")
                if mtype == "ai-title" and not ai_title:
                    ai_title = (msg.get("aiTitle") or "")[:SUMMARY_MAX_CHARS]
                elif mtype == "user" and not msg.get("isMeta") and not first_prompt:
                    content = msg.get("message", {}).get("content", "")
                    if isinstance(content, str):
                        if content and not content.startswith("<"):
                            first_prompt = content[:SUMMARY_MAX_CHARS]
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if text and not text.startswith("<"):
                                    first_prompt = text[:SUMMARY_MAX_CHARS]
                                    break
                if ai_title and first_prompt:
                    break
    except (OSError, UnicodeError) as exc:
        raise ClaudeDataError(f"cannot read {jsonl}: {exc}") from exc
    return first_prompt, ai_title


def find_duplicate_sessions() -> dict[str, list[str]]:
    """Find session ids that appear in more than one project directory.

    Returns ``{session_id: [project_dir_name, ...]}`` for ids present in 2+ dirs
    (e.g. left over from session migration/copy). Only the top level of each
    project dir is scanned.
    """
    from collections import defaultdict

    id_to_dirs: dict[str, list[str]] = defaultdict(list)
    if not PROJECTS_DIR.exists():
        return {}
    try:
        for d in PROJECTS_DIR.iterdir():
            if not d.is_dir():
                continue
            for jsonl in d.glob("*.jsonl"):
                id_to_dirs[jsonl.stem].append(d.name)
    except (PermissionError, OSError):
        pass
    return {sid: dirs for sid, dirs in id_to_dirs.items() if len(dirs) > 1}


def _session_has_conversation(jsonl: Path) -> bool:
    """True if a session file has any user/assistant message with content."""
    has_conversation, _corrupt = _session_state(jsonl)
    return has_conversation


def _session_state(jsonl: Path) -> tuple[bool, bool]:
    has_conversation = False
    try:
        with open(jsonl, encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    return has_conversation, True
                if not isinstance(msg, dict):
                    return has_conversation, True
                if msg.get("type") in ("user", "assistant"):
                    message = msg.get("message")
                    if not isinstance(message, (dict, str)):
                        return has_conversation, True
                    if _extract_text_content(message):
                        has_conversation = True
    except OSError:
        return False, True
    return has_conversation, False


# A session with a real conversation is far larger than this; only files under
# the cap are parsed to confirm emptiness, which bounds the scan.
EMPTY_SESSION_SIZE_CAP = 16 * 1024


def find_empty_sessions(size_cap: int = EMPTY_SESSION_SIZE_CAP) -> list[dict]:
    """Find 'stub' session files that have no actual conversation.

    These are sessions with only an ai-title / pr-link / metadata and no
    user/assistant turns (they can't be resumed). Only files up to ``size_cap``
    bytes are inspected — a real conversation is far larger, so this bounds the
    scan; emptiness itself is confirmed by parsing.
    Returns ``[{session_id, project_dir, path, title}]``.
    """
    result: list[dict] = []
    if not PROJECTS_DIR.exists():
        return result
    try:
        for d in PROJECTS_DIR.iterdir():
            if not d.is_dir():
                continue
            for jsonl in d.glob("*.jsonl"):
                try:
                    if jsonl.stat().st_size > size_cap:
                        continue
                except OSError:
                    continue
                has_conversation, corrupt = _session_state(jsonl)
                if corrupt or has_conversation:
                    continue
                _, title = _scan_jsonl_summary(jsonl)
                result.append({
                    "session_id": jsonl.stem,
                    "project_dir": d.name,
                    "path": str(jsonl),
                    "title": title,
                })
    except (PermissionError, OSError):
        pass
    return result


def remove_project_from_json(project_path: str) -> bool:
    """Remove a project entry from .claude.json (atomic write)."""
    import tempfile
    data = load_claude_json()
    projects = data.get("projects", {})
    if project_path in projects:
        del projects[project_path]
        data["projects"] = projects
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(CLAUDE_JSON.parent), suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, str(CLAUDE_JSON))
            return True
        except OSError:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False
    return False
