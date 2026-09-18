"""Data models for asm."""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def encode_path(path: str) -> str:
    """Encode a filesystem path to Claude's directory name format.

    Note: This encoding is lossy -- different paths may produce the same encoded
    name (e.g., /home/user/my-project and /home/user/my_project both become
    -home-user-my-project). This matches Claude Code's own encoding scheme.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def decode_path_hint(encoded: str) -> str:
    """Best-effort decode of an encoded path (lossy, for display only).

    On Windows, detects drive-letter prefixes (e.g. ``C-Users-...`` → ``C:/Users/...``).
    On Unix, always prefixes with ``/``.
    """
    parts = [p for p in encoded.strip("-").split("-") if p]
    if not parts:
        return "/"
    # Windows: first part is a single letter → treat as drive letter
    if IS_WINDOWS and len(parts[0]) == 1 and parts[0].isalpha():
        drive = parts[0].upper()
        rest = "/".join(parts[1:])
        return f"{drive}:/{rest}" if rest else f"{drive}:/"
    return "/" + "/".join(parts)


CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSION_ENV_DIR = CLAUDE_DIR / "session-env"
FILE_HISTORY_DIR = CLAUDE_DIR / "file-history"
DEBUG_DIR = CLAUDE_DIR / "debug"
TODOS_DIR = CLAUDE_DIR / "todos"
# Claude Code >= 2.1 stores per-session task/todo lists under tasks/<sessionId>/<n>.json
# (the old flat todos/<sessionId>-agent-<id>.json layout was removed).
TASKS_DIR = CLAUDE_DIR / "tasks"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
SKILLS_DIR = CLAUDE_DIR / "skills"
APP_DATA_DIR = Path.home() / ".asm"
BACKUP_BASE_DIR = APP_DATA_DIR / "backups"
RECOVERY_BASE_DIR = APP_DATA_DIR / "recovery"
# Older data dirs kept for one-time migration on startup (newest first).
LEGACY_APP_DATA_DIRS = [Path.home() / ".agentkeep", Path.home() / ".cc-tui"]

# --- Codex (OpenAI Codex CLI) data layout ---
# Sessions are global rollout-*.jsonl files partitioned by date, not per-project.
# Codex App, CLI and companion agents share one data home. CODEX_HOME may
# point one process at a legacy/archive home, but asm never auto-discovers or
# aggregates sibling ~/.codex-* directories.
DEFAULT_CODEX_DIR = Path.home() / ".codex"
CODEX_DIR = Path(os.environ["CODEX_HOME"]).expanduser() if os.environ.get("CODEX_HOME") else DEFAULT_CODEX_DIR
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"



def migrate_legacy_data_dir() -> bool:
    """One-time migration of older data dirs (~/.agentkeep, ~/.cc-tui) into ~/.asm.

    Merges each legacy item (backups/, recovery/, trash-log.jsonl) into the new
    dir without clobbering anything already there, so a stray file in the new dir
    doesn't strand the old backups. Returns True if anything was migrated. Safe to
    call on every startup (no-op once the legacy dirs are gone).
    """
    moved = False
    failures: list[str] = []
    for legacy in LEGACY_APP_DATA_DIRS:
        if not legacy.exists():
            continue
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        for item in legacy.iterdir():
            dest = APP_DATA_DIR / item.name
            if dest.exists():
                continue
            try:
                shutil.move(str(item), str(dest))
                moved = True
            except OSError as exc:
                failures.append(f"{item}: {exc}")
        try:
            if not any(legacy.iterdir()):
                legacy.rmdir()
        except OSError as exc:
            failures.append(f"{legacy}: {exc}")
    if failures:
        raise OSError("Legacy app-data migration failed: " + "; ".join(failures))
    return moved


@dataclass
class ProjectInfo:
    """A project registered in .claude.json."""

    path: str
    exists: bool = False
    last_cost: float | None = None
    last_duration: float | None = None
    session_env_dirs: list[str] = field(default_factory=list)


@dataclass
class SessionInfo:
    """A session data directory under ~/.claude/projects/."""

    dir_name: str
    actual_path: str
    size_bytes: int = 0
    file_count: int = 0
    session_env_dirs: list[str] = field(default_factory=list)
    is_orphaned: bool = False


@dataclass
class SessionDetail:
    """Detailed session info from SDK or JSONL parsing."""

    session_id: str
    summary: str = ""
    last_modified: float = 0
    file_size: int = 0
    first_prompt: str = ""
    git_branch: str = ""
    cwd: str = ""
    project_dir: str = ""


@dataclass
class FileHistoryEntry:
    """A file history entry."""

    dir_name: str
    path: str
    size_bytes: int = 0
    is_orphaned: bool = False


@dataclass
class DebugEntry:
    """A debug file entry."""

    name: str
    path: str
    size_bytes: int = 0
    is_orphaned: bool = False


@dataclass
class TodoEntry:
    """A todo file entry."""

    name: str
    path: str
    size_bytes: int = 0
    is_orphaned: bool = False


@dataclass
class BackupInfo:
    """A backup entry."""

    name: str
    path: str
    created: float = 0
    size_bytes: int = 0
    backup_type: str = ""  # config, full, settings, plugins, sessions


@dataclass
class RecoveryInfo:
    """A recoverable snapshot created before trashing data."""

    id: str
    name: str
    category: str
    original_path: str
    snapshot_path: str
    created: float = 0
    size_bytes: int = 0
    original_exists: bool = False


@dataclass
class Stats:
    """Overall statistics."""

    total_projects: int = 0
    total_sessions: int = 0
    total_file_history: int = 0
    total_debug: int = 0
    total_todos: int = 0
    orphaned_sessions: int = 0
    orphaned_file_history: int = 0
    orphaned_debug: int = 0
    orphaned_todos: int = 0
    claude_dir_size: int = 0
    projects_dir_size: int = 0
