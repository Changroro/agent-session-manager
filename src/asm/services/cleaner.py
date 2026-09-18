"""Service for cleaning (trashing) Claude Code data."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from send2trash import send2trash

from asm.models import CLAUDE_DIR, DEBUG_DIR, FILE_HISTORY_DIR, PROJECTS_DIR, SESSION_ENV_DIR, TASKS_DIR, TODOS_DIR
from asm.services.recovery import create_recovery_snapshot
from asm.utils import strict_child, validate_identifier

logger = logging.getLogger(__name__)

# --- Path traversal prevention ---

_ALLOWED_ROOTS = (CLAUDE_DIR,)


def _validate_path(path: Path, root: Path = CLAUDE_DIR) -> Path:
    """Validate path and return resolved version. Raises ValueError if invalid."""
    return strict_child(path, root)


# --- Trash logging (recovery mechanism) ---

_TRASH_LOG = Path.home() / ".asm" / "trash-log.jsonl"
_DEFAULT_TRASH_LOG = _TRASH_LOG
_log_lock = threading.Lock()


def _log_trash(path: Path, category: str) -> None:
    """Append a record to the trash log (thread-safe)."""
    log_path = _TRASH_LOG
    if log_path == _DEFAULT_TRASH_LOG:
        from asm import models

        log_path = models.APP_DATA_DIR / "trash-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now().isoformat(),
        "path": str(path),
        "category": category,
    }
    with _log_lock:
        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")


def record_trash(path: Path, category: str) -> None:
    _log_trash(path, category)


def _snapshot_or_raise(path: Path, category: str) -> str:
    snapshot_id = create_recovery_snapshot(path, category)
    if not snapshot_id:
        raise OSError(f"Recovery snapshot failed: {path}")
    return snapshot_id


# --- Trash functions ---


def trash_session(dir_name: str) -> bool:
    """Move a session directory to trash, including related session-env dirs."""
    try:
        validate_identifier(dir_name, "project directory")
    except ValueError:
        return False
    target = PROJECTS_DIR / dir_name
    if not target.exists():
        return False
    try:
        resolved = _validate_path(target, PROJECTS_DIR)
        related = _related_session_envs(dir_name)
        _snapshot_or_raise(resolved, "session")
        for related_path in related:
            _snapshot_or_raise(related_path, "session-env")
        _trash_related_session_envs(related)
        _log_trash(resolved, "session")
        send2trash(str(resolved))
        return True
    except (ValueError, PermissionError, OSError) as e:
        logger.warning("Failed to trash session %s: %s", dir_name, e)
        return False


def trash_sessions(dir_names: list[str]) -> tuple[int, int]:
    """Trash multiple sessions. Returns (success_count, fail_count)."""
    ok, fail = 0, 0
    for name in dir_names:
        if trash_session(name):
            ok += 1
        else:
            fail += 1
    return ok, fail


def _related_session_envs(dir_name: str) -> list[Path]:
    if not SESSION_ENV_DIR.exists():
        return []
    try:
        return [
            _validate_path(d, SESSION_ENV_DIR)
            for d in SESSION_ENV_DIR.iterdir()
            if d.is_dir() and (d.name == dir_name or d.name.startswith(dir_name + "-"))
        ]
    except (PermissionError, OSError, ValueError) as exc:
        raise OSError(f"cannot inspect {SESSION_ENV_DIR}: {exc}") from exc


def _trash_related_session_envs(paths: list[Path]) -> None:
    """Trash session-env directories related to a project dir."""
    for path in paths:
        _log_trash(path, "session-env")
        send2trash(str(path))


def trash_single_session_file(project_encoded: str, session_id: str) -> bool:
    """Move a single .jsonl session file to trash."""
    try:
        validate_identifier(project_encoded, "project directory")
        validate_identifier(session_id, "session id")
    except ValueError:
        return False
    target = PROJECTS_DIR / project_encoded / f"{session_id}.jsonl"
    if not target.exists():
        return False
    try:
        project_root = _validate_path(PROJECTS_DIR / project_encoded, PROJECTS_DIR)
        resolved = _validate_path(target, project_root)
        _snapshot_or_raise(resolved, "session")
        _log_trash(resolved, "session")
        send2trash(str(resolved))
        return True
    except (ValueError, PermissionError, OSError) as e:
        logger.warning("Failed to trash session file %s: %s", session_id, e)
        return False


def trash_file_history(dir_name: str) -> bool:
    """Move a file history directory to trash."""
    try:
        validate_identifier(dir_name, "file history id")
    except ValueError:
        return False
    target = FILE_HISTORY_DIR / dir_name
    if not target.exists():
        return False
    try:
        resolved = _validate_path(target, FILE_HISTORY_DIR)
        _snapshot_or_raise(resolved, "file_history")
        _log_trash(resolved, "file_history")
        send2trash(str(resolved))
        return True
    except (ValueError, PermissionError, OSError) as e:
        logger.warning("Failed to trash file history %s: %s", dir_name, e)
        return False


def trash_file_histories(dir_names: list[str]) -> tuple[int, int]:
    """Trash multiple file history entries."""
    ok, fail = 0, 0
    for name in dir_names:
        if trash_file_history(name):
            ok += 1
        else:
            fail += 1
    return ok, fail


def trash_debug_file(name: str) -> bool:
    """Move a debug file to trash."""
    try:
        validate_identifier(name, "debug file")
    except ValueError:
        return False
    target = DEBUG_DIR / name
    if not target.exists():
        return False
    try:
        resolved = _validate_path(target, DEBUG_DIR)
        _snapshot_or_raise(resolved, "debug")
        _log_trash(resolved, "debug")
        send2trash(str(resolved))
        return True
    except (ValueError, PermissionError, OSError) as e:
        logger.warning("Failed to trash debug file %s: %s", name, e)
        return False


def trash_debug_files(names: list[str]) -> tuple[int, int]:
    """Trash multiple debug files."""
    ok, fail = 0, 0
    for name in names:
        if trash_debug_file(name):
            ok += 1
        else:
            fail += 1
    return ok, fail


def trash_todo_file(name: str) -> bool:
    """Move a todo/task entry to trash.

    ``name`` is a session id (a ``tasks/<sessionId>/`` directory) on Claude Code
    >= 2.1, or a legacy ``todos/*.json`` filename on older versions.
    """
    try:
        validate_identifier(name, "todo entry")
    except ValueError:
        return False
    target = TASKS_DIR / name
    if not target.exists():
        target = TODOS_DIR / name
    if not target.exists():
        return False
    try:
        root = TASKS_DIR if target.parent == TASKS_DIR else TODOS_DIR
        resolved = _validate_path(target, root)
        _snapshot_or_raise(resolved, "todo")
        _log_trash(resolved, "todo")
        send2trash(str(resolved))
        return True
    except (ValueError, PermissionError, OSError) as e:
        logger.warning("Failed to trash todo entry %s: %s", name, e)
        return False


def trash_todo_files(names: list[str]) -> tuple[int, int]:
    """Trash multiple todo files."""
    ok, fail = 0, 0
    for name in names:
        if trash_todo_file(name):
            ok += 1
        else:
            fail += 1
    return ok, fail


def prune_empty_debug_files() -> tuple[int, int]:
    """Find and trash empty debug files ([], {}, or empty content)."""
    return _prune_empty_in_dir(DEBUG_DIR, "debug")


def prune_empty_todo_files() -> tuple[int, int]:
    """Find and trash empty todo/task entries.

    On Claude Code >= 2.1 this trashes ``tasks/<sessionId>/`` directories that
    hold no task json files (only lock/highwatermark housekeeping files). On
    older versions it trashes empty ``todos/*.json`` files.
    """
    task_dirs = _task_dirs()
    if task_dirs:
        return _prune_empty_task_dirs()
    return _prune_empty_in_dir(TODOS_DIR, "todo") if TODOS_DIR.exists() else (0, 0)


def _task_dirs() -> list[Path]:
    if not TASKS_DIR.exists():
        return []
    try:
        return [d for d in TASKS_DIR.iterdir() if d.is_dir()]
    except (PermissionError, OSError) as exc:
        raise OSError(f"cannot read {TASKS_DIR}: {exc}") from exc


def _task_dir_is_empty(d: Path) -> bool:
    """A task dir is empty if it has no task json files (locks don't count)."""
    try:
        return not any(f.suffix == ".json" for f in d.iterdir() if f.is_file())
    except (PermissionError, OSError) as exc:
        raise OSError(f"cannot read task entry {d}: {exc}") from exc


def _prune_empty_task_dirs() -> tuple[int, int]:
    """Trash tasks/<sessionId>/ directories that contain no task json files."""
    if not TASKS_DIR.exists():
        return 0, 0
    ok, fail = 0, 0
    for d in TASKS_DIR.iterdir():
        if not d.is_dir() or not _task_dir_is_empty(d):
            continue
        try:
            resolved = _validate_path(d, TASKS_DIR)
            _snapshot_or_raise(resolved, "todo")
            _log_trash(resolved, "todo")
            send2trash(str(resolved))
            ok += 1
        except (ValueError, PermissionError, OSError) as e:
            logger.warning("Failed to prune empty task dir %s: %s", d.name, e)
            fail += 1
    return ok, fail


def count_empty_todos() -> int:
    """Count empty todo/task entries (task dirs with no json, or empty legacy files)."""
    task_dirs = _task_dirs()
    if task_dirs:
        return sum(1 for d in task_dirs if _task_dir_is_empty(d))
    return count_empty_files(TODOS_DIR) if TODOS_DIR.exists() else 0


def _prune_empty_in_dir(directory: Path, category: str = "generic") -> tuple[int, int]:
    """Trash files whose content is [], {}, or empty."""
    if not directory.exists():
        return 0, 0
    ok, fail = 0, 0
    for f in directory.iterdir():
        if not f.is_file():
            continue
        try:
            content = f.read_text(errors="replace").strip()
            if content in ("[]", "{}", ""):
                resolved_f = _validate_path(f, directory)
                _snapshot_or_raise(resolved_f, category)
                _log_trash(resolved_f, category)
                send2trash(str(resolved_f))
                ok += 1
        except (ValueError, PermissionError, OSError) as e:
            logger.warning("Failed to prune %s: %s", f.name, e)
            fail += 1
    return ok, fail


def count_empty_files(directory: Path) -> int:
    """Count files with empty content ([], {}, or empty)."""
    return len(_list_empty_files(directory))


def _list_empty_files(directory: Path) -> list[str]:
    """Names of files whose content is [], {}, or empty."""
    if not directory.exists():
        return []
    names: list[str] = []
    try:
        for f in sorted(directory.iterdir()):
            if not f.is_file():
                continue
            try:
                content = f.read_text(errors="replace").strip()
                if content in ("[]", "{}", ""):
                    names.append(f.name)
            except OSError as exc:
                raise OSError(f"cannot read empty-file candidate {f}: {exc}") from exc
    except (PermissionError, OSError) as exc:
        raise OSError(f"cannot read empty-file directory {directory}: {exc}") from exc
    return names


def list_empty_debug_files() -> list[str]:
    """Names of empty debug files (what prune_empty_debug_files would trash)."""
    return _list_empty_files(DEBUG_DIR)


def list_empty_todo_entries() -> list[str]:
    """Names of empty todo/task entries (what prune_empty_todo_files would trash)."""
    task_dirs = _task_dirs()
    if task_dirs:
        return sorted(d.name for d in task_dirs if _task_dir_is_empty(d))
    return _list_empty_files(TODOS_DIR) if TODOS_DIR.exists() else []


def trash_codex_session(path: str | Path) -> bool:
    """Move a Codex rollout session file to trash (validated under CODEX_HOME)."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        if p.is_symlink():
            raise ValueError(f"Refusing to operate on symlink: {p}")
        resolved = p.resolve()
        from asm.services import codex_data

        if resolved == codex_data.CODEX_SESSIONS_DIR.resolve() or not resolved.is_relative_to(codex_data.CODEX_SESSIONS_DIR.resolve()):
            raise ValueError(f"Path outside Codex dir: {p}")
        _snapshot_or_raise(resolved, "codex-session")
        _log_trash(resolved, "codex-session")
        send2trash(str(resolved))
        return True
    except (ValueError, PermissionError, OSError) as e:
        logger.warning("Failed to trash codex session %s: %s", path, e)
        return False
