"""Service for backing up and restoring Claude Code data."""

from __future__ import annotations

import logging
import json
import os
import posixpath
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from send2trash import send2trash

from asm.models import (
    BACKUP_BASE_DIR,
    APP_DATA_DIR,
    CLAUDE_DIR,
    CLAUDE_JSON,
    CODEX_DIR,
    CODEX_SESSIONS_DIR,
    PLUGINS_DIR,
    PROJECTS_DIR,
    SKILLS_DIR,
    BackupInfo,
)
from asm.utils import strict_child

logger = logging.getLogger(__name__)

# Settings files to back up
SETTINGS_FILES = [
    CLAUDE_DIR / "settings.json",
    CLAUDE_DIR / "settings.local.json",
    CLAUDE_DIR / "keybindings.json",
]

_SYMLINKS_ON = sys.platform != "win32"
_DEFAULT_APP_DATA_DIR = APP_DATA_DIR


def _app_data_dir() -> Path:
    if APP_DATA_DIR == _DEFAULT_APP_DATA_DIR:
        from asm import models

        return models.APP_DATA_DIR
    return APP_DATA_DIR


def _validate_backup_path(path: Path) -> None:
    """Ensure path is within the backup directory."""
    strict_child(path, BACKUP_BASE_DIR)


def _ensure_backup_dir() -> Path:
    """Ensure backup directory exists (private — backups may hold OAuth tokens)."""
    BACKUP_BASE_DIR.mkdir(parents=True, exist_ok=True)
    for d in (BACKUP_BASE_DIR.parent, BACKUP_BASE_DIR):
        d.chmod(0o700)
        if os.name != "nt" and d.stat().st_mode & 0o777 != 0o700:
            raise OSError(f"Could not restrict backup directory: {d}")
    return BACKUP_BASE_DIR


def _restrict(path: Path) -> None:
    """Set owner-only permissions on a sensitive backup file."""
    path.chmod(0o600)
    if os.name != "nt" and path.stat().st_mode & 0o777 != 0o600:
        raise OSError(f"Could not restrict backup file: {path}")


def _dir_size(path: Path) -> int:
    """Calculate total size of all files in a directory tree."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _copytree(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise OSError(f"Backup source cannot be a symlink: {source}")
    broken = detect_broken_symlinks(source)
    if broken:
        raise OSError(f"Broken symlink in backup source {source}: {broken[0]}")
    shutil.copytree(
        str(source),
        str(destination),
        symlinks=_SYMLINKS_ON,
    )


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _source_exists(paths: list[Path]) -> bool:
    for path in paths:
        if path.is_symlink() or path.is_file():
            return True
        if path.is_dir():
            try:
                if any(path.iterdir()):
                    return True
            except OSError as exc:
                raise OSError(f"cannot inspect backup source: {path}: {exc}") from exc
    return False


def _is_safe_tar_member(member: tarfile.TarInfo) -> bool:
    """Return True when an archive member is safe to extract."""
    member_path = Path(member.name)
    if member_path.is_absolute() or ".." in member_path.parts:
        return False
    if member.islnk():
        return False
    if member.isdev():
        return False
    if member.issym():
        target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
        if member.linkname.startswith("/") or target == ".." or target.startswith("../"):
            return False
    return True


_MAX_ARCHIVE_MEMBERS = 10000
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_RATIO = 1000


def _validated_archive_members(tar: tarfile.TarFile, archive_size: int) -> tuple[list[tarfile.TarInfo], str]:
    members: list[tarfile.TarInfo] = []
    names: set[str] = set()
    top: str | None = None
    total = 0
    for member in tar:
        if len(members) >= _MAX_ARCHIVE_MEMBERS:
            raise ValueError("Archive contains too many members")
        if not member.name or "\x00" in member.name or not _is_safe_tar_member(member):
            raise ValueError(f"Unsafe path in archive: {member.name}")
        normalized = posixpath.normpath(member.name)
        if normalized != member.name or normalized in {"", "."}:
            raise ValueError(f"Unsafe path in archive: {member.name}")
        if member.name in names:
            raise ValueError(f"Duplicate path in archive: {member.name}")
        names.add(member.name)
        member_top = member.name.split("/", 1)[0]
        if top is None:
            top = member_top
        elif member_top != top:
            raise ValueError("Archive must contain one top-level backup")
        total += max(member.size, 0)
        if total > _MAX_ARCHIVE_BYTES:
            raise ValueError("Archive expands beyond the supported size")
        members.append(member)
    if not members or top is None:
        raise ValueError("Archive is empty")
    if archive_size and total > archive_size * _MAX_ARCHIVE_RATIO:
        raise ValueError("Archive compression ratio is too high")
    return members, top


def _extract_archive_safely(
    tar: tarfile.TarFile, members: list[tarfile.TarInfo], destination: Path
) -> None:
    created: set[Path] = set()
    for member in members:
        target = destination / Path(*member.name.split("/"))
        if member.isdir():
            strict_child(target, destination)
            if target.exists() or target.is_symlink():
                if not target.is_dir() or target.is_symlink():
                    raise ValueError(f"Archive path collision: {member.name}")
                continue
            target.mkdir(parents=True)
            created.add(target)
            continue
        strict_child(target, destination)
        if target.exists() or target.is_symlink():
            raise ValueError(f"Archive path collision: {member.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if member.issym():
            target.symlink_to(member.linkname)
        elif member.isfile():
            source = tar.extractfile(member)
            if source is None:
                raise ValueError(f"Archive member has no data: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
        else:
            raise ValueError(f"Unsupported archive member: {member.name}")


# ── Create backups ───────────────────────────────────────────────


def create_config_backup() -> str | None:
    """Create a backup of .claude.json. Returns backup path or None."""
    if not CLAUDE_JSON.exists():
        return None
    backup_dir = None
    dest = None
    try:
        backup_dir = _ensure_backup_dir() / "config"
        backup_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        dest = backup_dir / f".claude-{timestamp}.json"
        shutil.copy2(str(CLAUDE_JSON), str(dest))
        _restrict(dest)
        return str(dest)
    except (OSError, shutil.Error) as e:
        logger.warning("Failed to create config backup: %s", e)
        if dest is not None and dest.exists():
            dest.unlink()
        return None


def create_full_backup() -> str | None:
    """Create a full backup of .claude directory and .claude.json."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    backup_dir = None
    try:
        backup_dir = _ensure_backup_dir() / f"full-{timestamp}"
        if not _source_exists([CLAUDE_DIR, CLAUDE_JSON]):
            return None
        backup_dir.mkdir(parents=True, exist_ok=True)
        if CLAUDE_DIR.exists():
            _copytree(CLAUDE_DIR, backup_dir / ".claude")
        if CLAUDE_JSON.exists():
            json_dest = backup_dir / ".claude.json"
            shutil.copy2(str(CLAUDE_JSON), str(json_dest))
            _restrict(json_dest)
        return str(backup_dir)
    except (OSError, shutil.Error) as e:
        logger.warning("Failed to create full backup: %s", e)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        return None


def create_settings_backup() -> str | None:
    """Backup settings.json, settings.local.json, keybindings.json."""
    existing = [f for f in SETTINGS_FILES if f.exists()]
    if not existing:
        return None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    backup_dir = None
    try:
        backup_dir = _ensure_backup_dir() / f"settings-{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for f in existing:
            shutil.copy2(str(f), str(backup_dir / f.name))
            _restrict(backup_dir / f.name)
        return str(backup_dir)
    except (OSError, shutil.Error) as e:
        logger.warning("Failed to create settings backup: %s", e)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(str(backup_dir), ignore_errors=True)
        return None


def create_plugins_backup() -> str | None:
    """Backup plugins/ and skills/ directories (including symlinks)."""
    if not _source_exists([PLUGINS_DIR, SKILLS_DIR]):
        return None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    backup_dir = None
    try:
        backup_dir = _ensure_backup_dir() / f"plugins-{timestamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        if PLUGINS_DIR.exists():
            _copytree(PLUGINS_DIR, backup_dir / "plugins")
        if SKILLS_DIR.exists():
            _copytree(SKILLS_DIR, backup_dir / "skills")
        return str(backup_dir)
    except (OSError, shutil.Error) as e:
        logger.warning("Failed to create plugins backup: %s", e)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(str(backup_dir), ignore_errors=True)
        return None


def create_sessions_backup() -> str | None:
    """Backup projects/ directory (session data)."""
    if not _source_exists([PROJECTS_DIR]):
        return None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    backup_dir = None
    try:
        backup_dir = _ensure_backup_dir() / f"sessions-{timestamp}"
        _copytree(PROJECTS_DIR, backup_dir / "projects")
        import_record = _app_data_dir() / "codex_session_imports.json"
        if import_record.exists():
            shutil.copy2(str(import_record), str(backup_dir / import_record.name))
            _restrict(backup_dir / import_record.name)
        return str(backup_dir)
    except (OSError, shutil.Error) as e:
        logger.warning("Failed to create sessions backup: %s", e)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(str(backup_dir), ignore_errors=True)
        return None


def create_codex_backup() -> str | None:
    """Backup Codex session data (~/.codex/sessions + small index/config files).

    Excludes the large regenerable caches (sqlite logs, generated_images).
    """
    if not _source_exists([CODEX_SESSIONS_DIR]):
        return None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    backup_dir = None
    try:
        backup_dir = _ensure_backup_dir() / f"codex-{timestamp}"
        _copytree(CODEX_SESSIONS_DIR, backup_dir / "sessions")
        for name in ("session_index.jsonl", "history.jsonl", "config.toml"):
            f = CODEX_DIR / name
            if f.exists():
                shutil.copy2(str(f), str(backup_dir / name))
                _restrict(backup_dir / name)
        import_record = CODEX_DIR / "external_agent_session_imports.json"
        if import_record.exists():
            shutil.copy2(str(import_record), str(backup_dir / import_record.name))
            _restrict(backup_dir / import_record.name)
        return str(backup_dir)
    except (OSError, shutil.Error) as e:
        logger.warning("Failed to create codex backup: %s", e)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(str(backup_dir), ignore_errors=True)
        return None


# ── List backups ─────────────────────────────────────────────────


def list_backups() -> list[BackupInfo]:
    """List all available backups."""
    backup_dir = BACKUP_BASE_DIR
    if not backup_dir.exists():
        return []
    result = []

    # Config backups (single files in config/)
    config_dir = backup_dir / "config"
    if config_dir.exists():
        try:
            config_files = sorted(config_dir.iterdir(), reverse=True)
        except OSError as exc:
            logger.warning("Skipping inaccessible config backups: %s", exc)
            config_files = []
        for f in config_files:
            if f.suffix == ".json":
                try:
                    stat = f.stat()
                except OSError as exc:
                    logger.warning("Skipping backup with unavailable metadata %s: %s", f, exc)
                    continue
                result.append(
                    BackupInfo(
                        name=f"[config] {f.name}",
                        path=str(f),
                        created=stat.st_mtime,
                        size_bytes=stat.st_size,
                        backup_type="config",
                    )
                )

    # Directory-based backups: full, settings, plugins, sessions
    _TYPE_PREFIXES = {
        "full-": "full",
        "settings-": "settings",
        "plugins-": "plugins",
        "sessions-": "sessions",
        "codex-": "codex",
    }
    try:
        backup_entries = sorted(backup_dir.iterdir(), reverse=True)
    except OSError as exc:
        raise OSError(f"cannot list backups: {backup_dir}: {exc}") from exc
    for d in backup_entries:
        if not d.is_dir():
            continue
        for prefix, btype in _TYPE_PREFIXES.items():
            if d.name.startswith(prefix):
                try:
                    size = _dir_size(d)
                    stat = d.stat()
                except OSError as exc:
                    logger.warning("Skipping backup with unavailable metadata %s: %s", d, exc)
                    break
                result.append(
                    BackupInfo(
                        name=f"[{btype}] {d.name}",
                        path=str(d),
                        created=stat.st_mtime,
                        size_bytes=size,
                        backup_type=btype,
                    )
                )
                break

    result.sort(key=lambda b: b.created, reverse=True)
    return result


# ── Symlink detection ────────────────────────────────────────────


def detect_symlinks(path: Path) -> list[str]:
    """Find all symlinks in a directory tree. Returns list of relative path strings."""
    symlinks = []
    if not path.exists():
        return symlinks
    for item in path.rglob("*"):
        if item.is_symlink():
            target = str(item.resolve()) if item.exists() else "(broken)"
            rel = str(item.relative_to(path))
            symlinks.append(f"{rel} -> {target}")
    return symlinks


def detect_broken_symlinks(path: Path) -> list[str]:
    """Find symlinks whose targets don't exist. Returns list of relative path strings."""
    broken = []
    if not path.exists():
        return broken
    for item in path.rglob("*"):
        if item.is_symlink():
            target = item.resolve()
            if not target.exists():
                rel = str(item.relative_to(path))
                raw_target = str(Path(str(item)).readlink())
                broken.append(f"{rel} -> {raw_target}")
    return broken


# ── Restore backups ──────────────────────────────────────────────


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON backup: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON backup must contain an object: {path}")
    return data


def _validate_restore_source(src: Path, backup_type: str) -> None:
    _validate_backup_path(src)
    if backup_type == "config":
        if not src.is_file():
            raise ValueError(f"Config backup is not a file: {src}")
        _read_json_object(src)
        return
    if backup_type == "full":
        if not (src / ".claude").is_dir() and not (src / ".claude.json").is_file():
            raise ValueError(f"Full backup has no restorable data: {src}")
        if (src / ".claude.json").exists():
            _read_json_object(src / ".claude.json")
        return
    required = {
        "settings": ("settings.json", "settings.local.json", "keybindings.json"),
        "plugins": ("plugins", "skills"),
        "sessions": ("projects",),
        "codex": ("sessions",),
    }[backup_type]
    if not any((src / name).exists() for name in required):
        raise ValueError(f"Backup has no restorable {backup_type} data: {src}")
    if backup_type == "settings":
        for name in required:
            path = src / name
            if path.exists():
                _read_json_object(path)


def _require_safety_backup(create, sources: list[Path]) -> str | None:
    if not _source_exists(sources):
        return None
    path = create()
    if not path:
        raise OSError("Safety backup could not be created")
    return path


def restore_config_backup(backup_path: str) -> bool:
    """Restore .claude.json from a backup file."""
    src = Path(backup_path)
    if not src.exists():
        return False
    try:
        _validate_restore_source(src, "config")
        payload = src.read_bytes()
        _require_safety_backup(create_config_backup, [CLAUDE_JSON])
        temp = CLAUDE_JSON.with_name(f".{CLAUDE_JSON.name}.restoring")
        temp.write_bytes(payload)
        os.replace(temp, CLAUDE_JSON)
        return True
    except (OSError, ValueError) as e:
        logger.warning("Failed to restore config backup: %s", e)
        if 'temp' in locals() and temp.exists():
            temp.unlink()
        return False


def restore_full_backup(backup_path: str) -> bool:
    """Restore a full backup with rename+rollback for safety.

    Both .claude directory and .claude.json are restored atomically --
    if either step fails, both are rolled back.
    """
    src = Path(backup_path)
    if not src.exists():
        return False
    old_dir = CLAUDE_DIR.with_name(".claude.restore-old")
    old_json = CLAUDE_JSON.with_name(f".{CLAUDE_JSON.name}.restore-old")
    stage_dir = CLAUDE_DIR.with_name(".claude.restoring")
    stage_json = CLAUDE_JSON.with_name(f".{CLAUDE_JSON.name}.restoring")
    moved_dir = moved_json = installed_dir = installed_json = False
    try:
        _validate_restore_source(src, "full")
        _require_safety_backup(create_full_backup, [CLAUDE_DIR, CLAUDE_JSON])

        claude_backup = src / ".claude"
        json_backup = src / ".claude.json"
        for path in (old_dir, old_json, stage_dir, stage_json):
            if path.exists() or path.is_symlink():
                _remove_path(path)
        if json_backup.exists():
            shutil.copy2(str(json_backup), str(stage_json))
        if claude_backup.exists():
            _copytree(claude_backup, stage_dir)

        if CLAUDE_DIR.exists() or CLAUDE_DIR.is_symlink():
            CLAUDE_DIR.rename(old_dir)
            moved_dir = True
        if stage_dir.exists():
            stage_dir.rename(CLAUDE_DIR)
            installed_dir = True
        if CLAUDE_JSON.exists() or CLAUDE_JSON.is_symlink():
            CLAUDE_JSON.rename(old_json)
            moved_json = True
        if stage_json.exists():
            os.replace(stage_json, CLAUDE_JSON)
            installed_json = True
        if moved_dir:
            _remove_path(old_dir)
        if moved_json:
            _remove_path(old_json)

        return True
    except (OSError, ValueError, shutil.Error) as e:
        logger.warning("Failed to restore full backup: %s", e)
        try:
            if installed_dir and (CLAUDE_DIR.exists() or CLAUDE_DIR.is_symlink()):
                _remove_path(CLAUDE_DIR)
            if moved_dir and (old_dir.exists() or old_dir.is_symlink()):
                old_dir.rename(CLAUDE_DIR)
            if installed_json and (CLAUDE_JSON.exists() or CLAUDE_JSON.is_symlink()):
                _remove_path(CLAUDE_JSON)
            if moved_json and (old_json.exists() or old_json.is_symlink()):
                old_json.rename(CLAUDE_JSON)
            for path in (stage_dir, stage_json):
                if path.exists() or path.is_symlink():
                    _remove_path(path)
        except OSError as rollback_error:
            logger.error("Full restore rollback failed: %s", rollback_error)
        return False


def _replace_dir_with_rollback(src_dir: Path, dest_dir: Path) -> None:
    """Replace dest_dir with a copy of src_dir, rolling back on failure.

    The live dir is renamed aside first; if the copy fails the original is
    restored, so a mid-copy error never leaves the user with no data.
    """
    temp = dest_dir.with_name(dest_dir.name + ".restoring")
    if temp.exists():
        shutil.rmtree(str(temp))
    moved = False
    if dest_dir.exists():
        dest_dir.rename(temp)
        moved = True
    try:
        _copytree(src_dir, dest_dir)
    except (OSError, shutil.Error):
        if moved:
            if dest_dir.exists():
                shutil.rmtree(str(dest_dir), ignore_errors=True)
            temp.rename(dest_dir)
        raise
    if moved and temp.exists():
        shutil.rmtree(str(temp))


def _restore_files_with_rollback(files: list[tuple[Path, Path]]) -> None:
    staged: list[tuple[Path, Path]] = []
    old: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for index, (source, destination) in enumerate(files):
            stage = destination.with_name(f".{destination.name}.restoring-{index}")
            if stage.exists() or stage.is_symlink():
                _remove_path(stage)
            shutil.copy2(str(source), str(stage))
            staged.append((stage, destination))
        for index, (_stage, destination) in enumerate(staged):
            old_path = destination.with_name(f".{destination.name}.restore-old-{index}")
            if old_path.exists() or old_path.is_symlink():
                _remove_path(old_path)
            if destination.exists() or destination.is_symlink():
                destination.rename(old_path)
                old.append((old_path, destination))
            _stage.rename(destination)
            installed.append(destination)
        for old_path, _destination in old:
            _remove_path(old_path)
    except (OSError, shutil.Error):
        for destination in installed:
            if destination.exists() or destination.is_symlink():
                _remove_path(destination)
        for old_path, destination in reversed(old):
            if old_path.exists() or old_path.is_symlink():
                old_path.rename(destination)
        for stage, _destination in staged:
            if stage.exists() or stage.is_symlink():
                _remove_path(stage)
        raise


def restore_settings_backup(backup_path: str) -> bool:
    """Restore settings files from a backup directory."""
    src = Path(backup_path)
    if not src.exists():
        return False
    try:
        _validate_restore_source(src, "settings")
        files = [
            (src / name, CLAUDE_DIR / name)
            for name in ("settings.json", "settings.local.json", "keybindings.json")
            if (src / name).is_file()
        ]
        _require_safety_backup(create_settings_backup, SETTINGS_FILES)
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        _restore_files_with_rollback(files)
        return True
    except (OSError, ValueError, shutil.Error) as e:
        logger.warning("Failed to restore settings backup: %s", e)
        return False


def restore_plugins_backup(backup_path: str) -> tuple[bool, list[str]]:
    """Restore plugins/skills from a backup directory.

    Returns (success, list of symlink warnings).
    Symlink-based items are restored but a warning is returned for broken ones.
    """
    src = Path(backup_path)
    if not src.exists():
        return False, []
    try:
        _validate_restore_source(src, "plugins")
        _require_safety_backup(create_plugins_backup, [PLUGINS_DIR, SKILLS_DIR])

        plugins_src = src / "plugins"
        skills_src = src / "skills"

        stage_root = BACKUP_BASE_DIR / ".plugins-restore-staging"
        if stage_root.exists():
            _remove_path(stage_root)
        stage_root.mkdir(parents=True)
        staged_dirs: list[tuple[Path, Path]] = []
        for name, destination in (("plugins", PLUGINS_DIR), ("skills", SKILLS_DIR)):
            source = src / name
            if source.exists():
                staged = stage_root / name
                _copytree(source, staged)
                staged_dirs.append((staged, destination))
        old_dirs: list[tuple[Path, Path]] = []
        installed_dirs: list[Path] = []
        try:
            for index, (staged, destination) in enumerate(staged_dirs):
                old = destination.with_name(f".{destination.name}.restore-old-{index}")
                if old.exists() or old.is_symlink():
                    _remove_path(old)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    destination.rename(old)
                    old_dirs.append((old, destination))
                staged.rename(destination)
                installed_dirs.append(destination)
            for old, _destination in old_dirs:
                _remove_path(old)
        except (OSError, shutil.Error):
            for destination in installed_dirs:
                if destination.exists() or destination.is_symlink():
                    _remove_path(destination)
            for old, destination in reversed(old_dirs):
                if old.exists() or old.is_symlink():
                    old.rename(destination)
            raise
        finally:
            if stage_root.exists():
                _remove_path(stage_root)

        # Detect broken symlinks after restore
        broken = []
        broken.extend(detect_broken_symlinks(PLUGINS_DIR))
        broken.extend(detect_broken_symlinks(SKILLS_DIR))

        return True, broken
    except (OSError, ValueError, shutil.Error) as e:
        logger.warning("Failed to restore plugins backup: %s", e)
        return False, []


def restore_sessions_backup(backup_path: str) -> bool:
    """Restore projects/ directory from a backup."""
    src = Path(backup_path)
    projects_src = src / "projects"
    if not src.exists() or not projects_src.exists():
        return False
    record = src / "codex_session_imports.json"
    record_target = _app_data_dir() / record.name
    record_stage = record_target.with_name(f".{record_target.name}.restoring")
    record_old = record_target.with_name(f".{record_target.name}.restore-old")
    safety = None
    record_installed = False
    try:
        _validate_restore_source(src, "sessions")
        safety = _require_safety_backup(create_sessions_backup, [PROJECTS_DIR])
        if record.exists():
            record_target.parent.mkdir(parents=True, exist_ok=True)
            if record_stage.exists() or record_stage.is_symlink():
                _remove_path(record_stage)
            shutil.copy2(str(record), str(record_stage))
            _restrict(record_stage)

        _replace_dir_with_rollback(projects_src, PROJECTS_DIR)
        if record_stage.exists():
            if record_target.exists() or record_target.is_symlink():
                record_target.rename(record_old)
            record_stage.replace(record_target)
            record_installed = True
        if record_old.exists():
            _remove_path(record_old)
        return True
    except (OSError, ValueError, shutil.Error) as e:
        logger.warning("Failed to restore sessions backup: %s", e)
        try:
            if record_installed and (record_target.exists() or record_target.is_symlink()):
                _remove_path(record_target)
            if record_old.exists() or record_old.is_symlink():
                record_old.rename(record_target)
            if record_stage.exists() or record_stage.is_symlink():
                _remove_path(record_stage)
            if safety is not None and (safety_path := Path(safety) / "projects").exists():
                _replace_dir_with_rollback(safety_path, PROJECTS_DIR)
        except OSError as rollback_error:
            logger.error("Sessions restore rollback failed: %s", rollback_error)
        return False


def restore_codex_backup(backup_path: str) -> bool:
    """Restore ~/.codex/sessions (and index/config files) from a codex backup."""
    src = Path(backup_path)
    sessions_src = src / "sessions"
    if not src.exists() or not sessions_src.exists():
        return False
    stage = CODEX_SESSIONS_DIR.with_name(".sessions.restoring")
    old = CODEX_SESSIONS_DIR.with_name(".sessions.restore-old")
    stage_meta: list[tuple[Path, Path]] = []
    old_meta: list[tuple[Path, Path]] = []
    installed_meta: list[Path] = []
    moved_sessions = installed_sessions = False
    try:
        _validate_restore_source(src, "codex")
        _require_safety_backup(create_codex_backup, [CODEX_SESSIONS_DIR])
        for path in (stage, old):
            if path.exists() or path.is_symlink():
                _remove_path(path)
        _copytree(sessions_src, stage)
        for name in ("session_index.jsonl", "history.jsonl", "config.toml", "external_agent_session_imports.json"):
            source = src / name
            if source.exists():
                staged = stage.with_name(f".{name}.restoring")
                shutil.copy2(str(source), str(staged))
                _restrict(staged)
                stage_meta.append((staged, CODEX_DIR / name))
        if CODEX_SESSIONS_DIR.exists() or CODEX_SESSIONS_DIR.is_symlink():
            CODEX_SESSIONS_DIR.rename(old)
            moved_sessions = True
        stage.rename(CODEX_SESSIONS_DIR)
        installed_sessions = True
        for staged, destination in stage_meta:
            old_path = destination.with_name(f".{destination.name}.restore-old")
            if destination.exists() or destination.is_symlink():
                destination.rename(old_path)
                old_meta.append((old_path, destination))
            staged.rename(destination)
            installed_meta.append(destination)
        if moved_sessions:
            _remove_path(old)
        for old_path, _destination in old_meta:
            _remove_path(old_path)
        return True
    except (OSError, ValueError, shutil.Error) as e:
        logger.warning("Failed to restore codex backup: %s", e)
        try:
            if installed_sessions and (CODEX_SESSIONS_DIR.exists() or CODEX_SESSIONS_DIR.is_symlink()):
                _remove_path(CODEX_SESSIONS_DIR)
            if moved_sessions and (old.exists() or old.is_symlink()):
                old.rename(CODEX_SESSIONS_DIR)
            for destination in installed_meta:
                if destination.exists() or destination.is_symlink():
                    _remove_path(destination)
            for old_path, destination in reversed(old_meta):
                if old_path.exists() or old_path.is_symlink():
                    old_path.rename(destination)
            if stage.exists() or stage.is_symlink():
                _remove_path(stage)
            for staged, _destination in stage_meta:
                if staged.exists() or staged.is_symlink():
                    _remove_path(staged)
        except OSError as rollback_error:
            logger.error("Codex restore rollback failed: %s", rollback_error)
        return False


# ── Delete backup ────────────────────────────────────────────────


def delete_backup(backup_path: str) -> bool:
    """Delete a backup (moves to OS trash for safety)."""
    p = Path(backup_path)
    if not p.exists():
        return False
    try:
        _validate_backup_path(p)
        from asm.services.cleaner import record_trash

        record_trash(p, "backup")
        send2trash(str(p))
        return True
    except (OSError, ValueError) as e:
        logger.warning("Failed to delete backup: %s", e)
        return False


# ── Export / Import ──────────────────────────────────────────────


def export_backup(backup_path: str, dest_dir: str | None = None) -> str | None:
    """Export a backup as .tar.gz for server migration.

    Args:
        backup_path: Path to the backup (file or directory).
        dest_dir: Where to write the archive. Defaults to ~/Desktop or home.

    Returns:
        Path to the created .tar.gz or None on failure.
    """
    src = Path(backup_path)
    if not src.exists():
        return None
    archive_path = None
    try:
        _validate_backup_path(src)
        if dest_dir:
            out_dir = Path(dest_dir)
        else:
            desktop = Path.home() / "Desktop"
            out_dir = desktop if desktop.exists() else Path.home()
        out_dir.mkdir(parents=True, exist_ok=True)

        archive_name = src.name if src.is_dir() else src.stem
        archive_path = out_dir / f"{archive_name}.tar.gz"

        # Avoid overwrite
        counter = 1
        while archive_path.exists():
            archive_path = out_dir / f"{archive_name}_{counter}.tar.gz"
            counter += 1

        with tarfile.open(str(archive_path), "w:gz") as tar:
            tar.add(str(src), arcname=src.name)
        # May bundle ~/.claude.json (OAuth tokens) — don't leave it world-readable.
        _restrict(archive_path)

        return str(archive_path)
    except (OSError, ValueError, tarfile.TarError) as e:
        logger.warning("Failed to export backup: %s", e)
        if archive_path is not None and archive_path.exists():
            try:
                archive_path.unlink()
            except OSError:
                logger.error("Failed to remove partial archive %s", archive_path)
        return None


def import_backup(archive_path: str) -> str | None:
    """Import a .tar.gz backup into the backup directory.

    Returns the extracted backup path or None on failure.
    """
    src = Path(archive_path)
    if not src.exists() or not src.name.endswith(".tar.gz"):
        return None
    staging = None
    try:
        backup_dir = _ensure_backup_dir()
        archive_size = src.stat().st_size
        with tarfile.open(str(src), "r:gz") as tar:
            members, top = _validated_archive_members(tar, archive_size)
        final = backup_dir / top
        if final.exists() or final.is_symlink():
            raise FileExistsError(f"Backup already exists: {final}")
        staging = Path(tempfile.mkdtemp(prefix=".backup-import-", dir=str(backup_dir)))
        with tarfile.open(str(src), "r:gz") as tar:
            members, checked_top = _validated_archive_members(tar, archive_size)
            if checked_top != top:
                raise ValueError("Archive changed while it was being read")
            _extract_archive_safely(tar, members, staging)
        (staging / top).rename(final)
        staging.rmdir()
        staging = None
        return str(final)
    except (OSError, ValueError, tarfile.TarError) as e:
        logger.warning("Failed to import backup: %s", e)
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        return None
