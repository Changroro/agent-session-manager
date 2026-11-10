"""Service for migrating Claude Code sessions between projects."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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
    new_ids: dict[str, str] = field(default_factory=dict)  # source id -> id in the target


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
    copy: bool = False,
) -> MigrateResult:
    """Move (or with ``copy=True``, duplicate) sessions into the target project.

    Each selected ``<id>.jsonl`` and its ``<id>/`` sidecar dir (subagents, tool
    results) is staged and path-rewritten, then renamed into the target; only
    after that are the originals trashed with recovery snapshots. Target
    sessions not being moved are never touched. ``append`` skips ids already in
    the target; ``overwrite`` trashes (with snapshot) and replaces them. Memory
    files missing from the target are added. ``session_ids=None`` moves all.

    ``copy`` keeps the originals and gives every copy a fresh session id (file
    names and ``sessionId`` fields), so both can be resumed independently.
    """
    import uuid

    from asm.services import cleaner

    if mode not in {"append", "overwrite"}:
        return MigrateResult(success=False, message=f"Unknown migration mode: {mode}")
    # Shell tab-completion appends "/", which would encode to a different dir.
    source_path = os.path.normpath(source_path) if source_path else source_path
    target_path = os.path.normpath(target_path) if target_path else target_path
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

    if not source_dir.exists():
        similar = find_similar_dirs(source_encoded)
        msg = f"Source not found: {source_dir}"
        if similar:
            msg += f"\nSimilar: {', '.join(similar)}"
        return MigrateResult(success=False, message=msg)

    all_sessions = list(source_dir.glob("*.jsonl"))
    if session_ids is not None:
        id_set = set(session_ids)
        source_sessions = [f for f in all_sessions if f.stem in id_set]
    else:
        source_sessions = all_sessions
    if not source_sessions:
        return MigrateResult(success=False, message="No session files to migrate")
    if any(f.is_symlink() or (source_dir / f.stem).is_symlink() for f in source_sessions):
        return MigrateResult(success=False, message="Source contains a session symlink")
    if not copy:  # a copy never touches the originals
        from asm.services.live import LiveSessionError, ensure_claude_not_live

        try:
            ensure_claude_not_live([f.stem for f in source_sessions], source_sessions)
        except LiveSessionError as exc:
            return MigrateResult(success=False, message=str(exc))

    stage = Path(tempfile.mkdtemp(prefix=".asm-migrate-", dir=str(PROJECTS_DIR)))
    staged: list[str] = []  # source ids staged for the target
    id_map: dict[str, str] = {}  # source id -> id in the target (new only when copying)
    moved: list[str] = []
    skipped = 0
    memory_copied = False
    try:
        for src_file in source_sessions:
            sid = src_file.stem
            new_id = str(uuid.uuid4()) if copy else sid
            if not copy and mode == "append" and (
                (target_dir / src_file.name).exists() or (target_dir / sid).exists()
            ):
                skipped += 1
                continue
            shutil.copy2(str(src_file), str(stage / f"{new_id}.jsonl"))
            rewrite = [stage / f"{new_id}.jsonl"]
            if (source_dir / sid).is_dir():
                shutil.copytree(str(source_dir / sid), str(stage / new_id), symlinks=True)
                rewrite += sorted((stage / new_id).rglob("*.jsonl"))
            if source_path != target_path or copy:
                _rewrite_paths(rewrite, source_path, target_path, {sid: new_id} if copy else None)
            staged.append(sid)
            id_map[sid] = new_id

        memory_files = _stage_missing_memory(source_dir / "memory", target_dir / "memory", stage / "memory")
        if staged:
            _merge_index(
                source_dir / "sessions-index.json",
                target_dir / "sessions-index.json",
                stage / "sessions-index.json",
                id_map,
                source_path,
                target_path,
                target_dir,
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        for sid in staged:
            tid = id_map[sid]
            dest_file = target_dir / f"{tid}.jsonl"
            dest_side = target_dir / tid
            if not copy and mode == "overwrite" and dest_file.exists():
                if not cleaner.trash_single_session_file(target_encoded, tid):
                    raise OSError(f"could not trash existing target session {tid}")
            if dest_file.exists() or dest_side.exists() or dest_side.is_symlink():
                raise OSError(f"target already has session data for {tid}: {target_dir}")
            if (stage / tid).exists():
                os.rename(stage / tid, dest_side)
            os.rename(stage / f"{tid}.jsonl", dest_file)
            moved.append(sid)
        for rel in memory_files:
            dest = target_dir / "memory" / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.rename(stage / "memory" / rel, dest)
                memory_copied = True
        if (stage / "sessions-index.json").exists():
            os.replace(stage / "sessions-index.json", target_dir / "sessions-index.json")

        if copy:
            kept = []
            msg = f"{len(moved)} sessions copied (new ids: {', '.join(id_map[s] for s in moved)})"
        else:
            kept = [sid for sid in moved if not cleaner.trash_single_session_file(source_encoded, sid)]
            _drop_index_entries(source_dir / "sessions-index.json", set(moved) - set(kept))
            msg = f"{len(moved)} sessions moved"
        if skipped:
            msg += f", {skipped} already in target (skipped)"
        if kept:
            msg += f"; {len(kept)} original(s) could not be trashed and remain in the source"
        return MigrateResult(
            success=True,
            sessions_copied=len(moved),
            sessions_skipped=skipped,
            memory_copied=memory_copied,
            message=f"{msg} ({source_path} -> {target_path})",
            new_ids={sid: id_map[sid] for sid in moved},
        )
    except (OSError, ValueError, json.JSONDecodeError, shutil.Error) as exc:
        logger.warning("Migration failed: %s", exc)
        note = f" ({len(moved)} already moved; originals kept)" if moved else ""
        return MigrateResult(
            success=False, sessions_copied=len(moved), sessions_skipped=skipped, message=f"{exc}{note}"
        )
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _stage_missing_memory(source: Path, target: Path, stage: Path) -> list[Path]:
    """Copy memory files absent from ``target`` into ``stage``; return their relative paths."""
    if not source.exists():
        return []
    rels = []
    for f in source.rglob("*"):
        if f.is_symlink():
            raise OSError(f"Source memory contains a symlink: {f}")
        if f.is_file():
            rel = f.relative_to(source)
            if not (target / rel).exists():
                (stage / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(f), str(stage / rel))
                rels.append(rel)
    return rels


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
    out: Path,
    id_map: dict[str, str],
    source_path: str,
    target_path: str,
    target_dir: Path,
) -> None:
    """Write ``target``'s index plus entries for the sessions in ``id_map`` to ``out``."""
    if not source.exists() and not target.exists():
        return
    source_data = _read_index(source) if source.exists() else {"entries": []}
    target_data = _read_index(target) if target.exists() else {"entries": []}
    added = []
    for entry in source_data["entries"]:
        new_id = id_map.get(entry.get("sessionId"))
        if new_id is None:
            continue
        entry = json.loads(json.dumps(entry))
        _replace_in_obj(entry, source_path, target_path, restrict_keys=False)
        entry["sessionId"] = new_id
        if "fullPath" in entry:
            entry["fullPath"] = str(target_dir / f"{new_id}.jsonl")
        added.append(entry)
    new_ids = set(id_map.values())
    entries = [e for e in target_data["entries"] if e.get("sessionId") not in new_ids] + added
    out.write_text(json.dumps({**target_data, "entries": entries}, ensure_ascii=False, indent=2) + "\n")


def _drop_index_entries(path: Path, ids: set[str]) -> None:
    if not ids or not path.exists():
        return
    data = _read_index(path)
    data["entries"] = [e for e in data["entries"] if e.get("sessionId") not in ids]
    tmp = path.with_suffix(".json.rewrite")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def _rewrite_paths(
    files: list[Path], source_path: str, target_path: str, id_map: dict[str, str] | None = None
) -> None:
    """Rewrite path fields (and, when copying, session ids) in the staged JSONL copies."""
    for jsonl in files:
        updated = []
        # split("\n"), not splitlines(): U+2028 etc. may appear inside JSON strings.
        for line in jsonl.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError(f"Malformed migrated session: {jsonl}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"Unexpected migrated session record: {jsonl}")
            _replace_in_obj(obj, source_path, target_path, restrict_keys=True)
            if id_map:
                _replace_ids(obj, id_map)
            updated.append(json.dumps(obj, ensure_ascii=False))
        jsonl.write_text("\n".join(updated) + "\n", encoding="utf-8")


_ID_KEYS = {"sessionId", "session_id"}


def _replace_ids(obj: object, id_map: dict[str, str]) -> None:
    """Point every ``sessionId``/``session_id`` field at the copy's new id."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _ID_KEYS and isinstance(v, str) and v in id_map:
                obj[k] = id_map[v]
            elif isinstance(v, (dict, list)):
                _replace_ids(v, id_map)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _replace_ids(item, id_map)
