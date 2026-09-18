"""Service for migrating Claude Code sessions between projects."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from send2trash import send2trash

from asm.models import PROJECTS_DIR, decode_path_hint, encode_path
from asm.utils import strict_child, validate_identifier

logger = logging.getLogger(__name__)


@dataclass
class MigrateResult:
    """Result of a migration operation."""

    success: bool
    sessions_copied: int = 0
    sessions_skipped: int = 0
    memory_copied: bool = False
    message: str = ""


def get_available_projects() -> list[tuple[str, str]]:
    """Get list of (encoded_name, decoded_hint) for available project dirs."""
    if not PROJECTS_DIR.exists():
        return []
    result = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir():
            hint = decode_path_hint(d.name)
            result.append((d.name, hint))
    return result


def find_similar_dirs(target_encoded: str) -> list[str]:
    """Find directories similar to the target encoded name."""
    if not PROJECTS_DIR.exists():
        return []
    # Extract key parts for matching
    parts = set(target_encoded.strip("-").split("-"))
    results = []
    for d in PROJECTS_DIR.iterdir():
        if d.is_dir():
            dir_parts = set(d.name.strip("-").split("-"))
            overlap = parts & dir_parts
            if len(overlap) >= len(parts) * 0.5:
                results.append(d.name)
    return results


def migrate_sessions(
    source_path: str,
    target_path: str,
    mode: str = "append",
    source_encoded: str | None = None,
    target_encoded: str | None = None,
    session_ids: list[str] | None = None,
) -> MigrateResult:
    """Migrate sessions from source project to target project.

    Args:
        source_path: Absolute path of source project (for display/path updates)
        target_path: Absolute path of target project (for display/path updates)
        mode: "append" (keep existing, skip duplicates) or "overwrite"
        source_encoded: Pre-encoded source dir name (avoids lossy re-encoding)
        target_encoded: Pre-encoded target dir name (avoids lossy re-encoding)
        session_ids: If provided, only migrate these specific session IDs.
                     If None, migrate all sessions.
    """
    if mode not in {"append", "overwrite"}:
        return MigrateResult(success=False, message=f"Unknown migration mode: {mode}")
    source_encoded = source_encoded or encode_path(source_path)
    target_encoded = target_encoded or encode_path(target_path)
    try:
        validate_identifier(source_encoded, "source project directory")
        validate_identifier(target_encoded, "target project directory")
        source_dir = strict_child(PROJECTS_DIR / source_encoded, PROJECTS_DIR)
        target_dir = strict_child(PROJECTS_DIR / target_encoded, PROJECTS_DIR)
    except ValueError as exc:
        return MigrateResult(success=False, message=str(exc))
    if source_encoded == target_encoded or source_dir == target_dir:
        return MigrateResult(success=False, message="Source and target are the same")

    # Validate source
    if not source_dir.exists():
        similar = find_similar_dirs(source_encoded)
        msg = f"Source not found: {source_dir}"
        if similar:
            msg += f"\nSimilar: {', '.join(similar)}"
        return MigrateResult(success=False, message=msg)

    # Check for session files (filter by session_ids if provided)
    all_sessions = list(source_dir.glob("*.jsonl"))
    if any(f.is_symlink() for f in all_sessions):
        return MigrateResult(success=False, message="Source contains a session symlink")
    if session_ids is not None:
        id_set = set(session_ids)
        source_sessions = [f for f in all_sessions if f.stem in id_set]
    else:
        source_sessions = all_sessions
    if not source_sessions:
        return MigrateResult(success=False, message="No session files to migrate")

    stage = Path(tempfile.mkdtemp(prefix=".asm-migrate-", dir=str(PROJECTS_DIR)))
    copied = 0
    skipped = 0
    copied_files: list[Path] = []
    memory_copied = False
    try:
        if mode == "append" and target_dir.exists():
            shutil.copytree(str(target_dir), str(stage), symlinks=True, dirs_exist_ok=True)
        for src_file in source_sessions:
            dest_file = stage / src_file.name
            if mode == "append" and (target_dir / src_file.name).exists():
                skipped += 1
                continue
            shutil.copy2(str(src_file), str(dest_file))
            copied += 1
            copied_files.append(dest_file)

        source_memory = source_dir / "memory"
        if source_memory.exists():
            target_memory = stage / "memory"
            if mode == "overwrite" and target_memory.exists():
                shutil.rmtree(str(target_memory))
            if not target_memory.exists():
                shutil.copytree(str(source_memory), str(target_memory), symlinks=True)
                memory_copied = True
            else:
                for f in source_memory.rglob("*"):
                    if f.is_symlink():
                        raise OSError(f"Source memory contains a symlink: {f}")
                    if f.is_file():
                        rel = f.relative_to(source_memory)
                        dest = target_memory / rel
                        if not dest.exists():
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(str(f), str(dest))
                            memory_copied = True

        _merge_index(
            source_dir / "sessions-index.json",
            stage / "sessions-index.json",
            {f.stem for f in source_sessions},
            mode,
            source_path,
            target_path,
            source_encoded,
            target_encoded,
        )
        if source_path != target_path:
            _update_paths(copied_files, None, source_path, target_path, source_encoded, target_encoded)
        if mode == "overwrite" and target_dir.exists():
            from asm.services.recovery import create_recovery_snapshot

            if not create_recovery_snapshot(target_dir, "migration"):
                raise OSError("Migration safety snapshot failed")
        _commit_stage(stage, target_dir, mode)
        stage = None
        msg = f"{copied} sessions migrated"
        if skipped:
            msg += f", {skipped} already in target (skipped)"
        return MigrateResult(
            success=True,
            sessions_copied=copied,
            sessions_skipped=skipped,
            memory_copied=memory_copied,
            message=f"{msg} ({source_path} -> {target_path})",
        )
    except (OSError, ValueError, json.JSONDecodeError, shutil.Error) as exc:
        logger.warning("Migration failed: %s", exc)
        return MigrateResult(success=False, sessions_copied=copied, sessions_skipped=skipped, message=str(exc))
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


_PATH_FIELDS = {"cwd", "projectPath", "path", "directory", "projectDir", "workingDirectory"}


def _replace_in_obj(obj: object, old: str, new: str, restrict_keys: bool = True) -> None:
    """Recursively replace string values in a JSON-like object.

    When restrict_keys is True, only replaces values in known path-containing
    fields to avoid corrupting message content.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and _path_replacement(v, old, new) != v:
                if not restrict_keys or k in _PATH_FIELDS:
                    obj[k] = _path_replacement(v, old, new)
            elif isinstance(v, (dict, list)):
                _replace_in_obj(v, old, new, restrict_keys)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                _replace_in_obj(item, old, new, restrict_keys)


def _path_replacement(value: str, old: str, new: str) -> str:
    if value == old:
        return new
    prefixes = (old + os.sep, old + "/", old + "\\")
    for prefix in prefixes:
        if value.startswith(prefix):
            return new + value[len(old):]
    return value


def _merge_index(
    source: Path,
    target: Path,
    selected: set[str],
    mode: str,
    source_path: str,
    target_path: str,
    source_encoded: str,
    target_encoded: str,
) -> None:
    if not source.exists() and not target.exists():
        return
    source_data = _read_index(source) if source.exists() else {"entries": []}
    target_data = _read_index(target) if target.exists() else {"entries": []}
    source_entries = [entry for entry in source_data["entries"] if entry.get("sessionId") in selected]
    for entry in source_entries:
        _replace_in_obj(entry, source_path, target_path, restrict_keys=False)
        _replace_in_obj(entry, source_encoded, target_encoded, restrict_keys=False)
    if mode == "overwrite":
        entries = source_entries
    else:
        entries = target_data["entries"]
        known = {entry.get("sessionId") for entry in entries}
        entries.extend(entry for entry in source_entries if entry.get("sessionId") not in known)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({**target_data, "entries": entries}, ensure_ascii=False, indent=2) + "\n")


def _read_index(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid sessions index: {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
        raise ValueError(f"Invalid sessions index: {path}")
    entries = data.get("entries", [])
    if any(not isinstance(entry, dict) or not entry.get("sessionId") for entry in entries):
        raise ValueError(f"Invalid sessions index entries: {path}")
    data["entries"] = entries
    return data


def _commit_stage(stage: Path, target: Path, mode: str) -> None:
    if mode == "append":
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(stage), str(target), symlinks=True, dirs_exist_ok=True)
        shutil.rmtree(stage)
        return
    old = target.with_name(f".{target.name}.migration-old")
    if old.exists() or old.is_symlink():
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
        else:
            old.unlink()
    moved = False
    try:
        if target.exists() or target.is_symlink():
            target.rename(old)
            moved = True
        stage.rename(target)
        if moved:
            from asm.services.cleaner import record_trash

            record_trash(old, "migration")
            send2trash(str(old))
    except OSError:
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if moved and (old.exists() or old.is_symlink()):
            old.rename(target)
        raise


def _update_paths(
    copied_files: list[Path],
    copied_index: Path | None,
    source_path: str,
    target_path: str,
    source_encoded: str,
    target_encoded: str,
) -> None:
    """Update path references in the files copied by this run."""
    for jsonl in copied_files:
        lines = jsonl.read_text(encoding="utf-8").splitlines()
        updated = []
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError(f"Malformed migrated session: {jsonl}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"Unexpected migrated session record: {jsonl}")
            _replace_in_obj(obj, source_path, target_path, restrict_keys=True)
            updated.append(json.dumps(obj, ensure_ascii=False))
        tmp = jsonl.with_suffix(".jsonl.rewrite")
        tmp.write_text("\n".join(updated) + "\n", encoding="utf-8")
        os.replace(tmp, jsonl)

    idx = copied_index
    if idx is not None and idx.exists():
        data = _read_index(idx)
        _replace_in_obj(data, source_path, target_path, restrict_keys=False)
        _replace_in_obj(data, source_encoded, target_encoded, restrict_keys=False)
        tmp = idx.with_suffix(".json.rewrite")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, idx)
