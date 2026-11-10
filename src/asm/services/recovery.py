"""App-managed recovery snapshots for trashed Claude Code data."""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from send2trash import send2trash

from asm.models import CLAUDE_DIR, RECOVERY_BASE_DIR, RecoveryInfo
from asm.utils import strict_child, validate_identifier

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")

# Retention for recovery snapshots — without this the "cleanup" tool would grow
# ~/.asm/recovery without bound (every trash duplicates data here + OS trash).
_MAX_ITEMS = 100
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
_DEFAULT_CLAUDE_DIR = CLAUDE_DIR
_DEFAULT_RECOVERY_BASE_DIR = RECOVERY_BASE_DIR


def _claude_root() -> Path:
    if CLAUDE_DIR == _DEFAULT_CLAUDE_DIR:
        from asm import models

        return models.CLAUDE_DIR
    return CLAUDE_DIR


def _recovery_base() -> Path:
    if RECOVERY_BASE_DIR == _DEFAULT_RECOVERY_BASE_DIR:
        from asm import models

        return models.APP_DATA_DIR / "recovery"
    return RECOVERY_BASE_DIR


def _validate_original_path(path: Path) -> Path:
    from asm.services import codex_data

    roots = (_claude_root().resolve(), codex_data.CODEX_SESSIONS_DIR.parent.resolve())
    lexical = path.absolute()
    if not any(
        lexical != root and lexical.is_relative_to(root)
        for root in (root.absolute() for root in roots)
    ):
        raise ValueError(f"Recovery path outside managed data dirs: {path}")
    if path.is_symlink():
        resolved = path.resolve()
        if not any(
            resolved != root and resolved.is_relative_to(root) for root in roots
        ):
            raise ValueError(f"Recovery symlink target outside managed data dirs: {path}")
        return path
    for root in roots:
        try:
            return strict_child(path, root)
        except ValueError:
            continue
    raise ValueError(f"Recovery path outside managed data dirs: {path}")


def _recovery_root(item_id: str) -> Path:
    validate_identifier(item_id, "recovery id")
    base = _recovery_base()
    return strict_child(base / item_id, base)


def _payload_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            total += entry.stat().st_size
    return total


def _safe_name(name: str) -> str:
    return (_SAFE_NAME_RE.sub("-", name).strip("-") or "item")[:48]


def create_recovery_snapshot(path: Path, category: str) -> str | None:
    """Create a recovery snapshot before the item is trashed."""
    if not path.exists() and not path.is_symlink():
        return None

    try:
        original = _validate_original_path(path)
        base = _recovery_base()
        base.mkdir(parents=True, exist_ok=True)
        base.chmod(0o700)

        item_id = f"{datetime.now():%Y%m%d-%H%M%S-%f}-{_safe_name(original.name)}"
        item_root = base / item_id
        payload_root = item_root / "payload"
        payload_root.mkdir(parents=True, exist_ok=True)

        snapshot = payload_root / original.name
        if original.is_symlink():
            snapshot.symlink_to(original.readlink())
        elif original.is_dir():
            shutil.copytree(
                str(original),
                str(snapshot),
                symlinks=True,
            )
        else:
            shutil.copy2(str(original), str(snapshot), follow_symlinks=False)

        metadata = {
            "id": item_id,
            "name": original.name,
            "category": category,
            "original_path": str(original),
            "snapshot_path": str(snapshot),
            "created": datetime.now().timestamp(),
            "size_bytes": _payload_size(snapshot),
        }
        metadata_path = item_root / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2)
        )
        item_root.chmod(0o700)
        metadata_path.chmod(0o600)
        if not _prune_snapshots():
            raise OSError("recovery retention could not be enforced")
        return item_id
    except (OSError, ValueError, shutil.Error) as exc:
        logger.warning("Failed to create recovery snapshot for %s: %s", path, exc)
        return None


def _prune_snapshots() -> bool:
    """Drop oldest snapshots beyond the item-count or total-size cap."""
    try:
        base = _recovery_base()
        items = [d for d in base.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError:
        return False
    items.sort(key=lambda d: d.name)  # timestamp-prefixed → chronological
    sizes = {d: _payload_size(d) for d in items}
    total = sum(sizes.values())
    # Oldest-first removal until within both caps.
    while items and (len(items) > _MAX_ITEMS or total > _MAX_TOTAL_BYTES):
        victim = items.pop(0)
        total -= sizes.get(victim, 0)
        try:
            shutil.rmtree(victim)
        except OSError as exc:
            logger.warning("Failed to prune recovery snapshot %s: %s", victim, exc)
            return False
    return True


def list_recovery_items() -> list[RecoveryInfo]:
    """List app-managed recovery snapshots."""
    base = _recovery_base()
    if not base.exists():
        return []

    result: list[RecoveryInfo] = []
    try:
        for item_root in sorted(base.iterdir(), reverse=True):
            if item_root.is_symlink() or not item_root.is_dir():
                continue
            meta_path = item_root / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                data = json.loads(meta_path.read_text())
                original_path = Path(data["original_path"])
                result.append(
                    RecoveryInfo(
                        id=data["id"],
                        name=data.get("name", item_root.name),
                        category=data.get("category", "generic"),
                        original_path=str(original_path),
                        snapshot_path=data["snapshot_path"],
                        created=float(data.get("created", 0)),
                        size_bytes=int(data.get("size_bytes", 0)),
                        original_exists=original_path.exists(),
                    )
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                logger.warning("Skipping invalid recovery item %s: %s", item_root, exc)
    except OSError as exc:
        logger.warning("Failed to list recovery items: %s", exc)
    return result


def restore_recovery_item(item_id: str, overwrite: bool = False) -> tuple[bool, str]:
    """Restore a recovery snapshot to its original path."""
    try:
        item_root = _recovery_root(item_id)
        data = json.loads((item_root / "metadata.json").read_text())
        if not isinstance(data, dict) or data.get("id") != item_id:
            return False, "Recovery metadata identity mismatch"
        original = _validate_original_path(Path(data["original_path"]))
        # Trust boundary: the snapshot source must live under the recovery dir,
        # so a tampered metadata.json can't copy an arbitrary file into a managed
        # location (CWE-22/502).
        snapshot = Path(data["snapshot_path"])
        payload_root = item_root / "payload"
        if snapshot.parent.resolve() != payload_root.resolve() or snapshot.name != original.name:
            return False, "Snapshot path outside recovery item"
        if not snapshot.exists() and not snapshot.is_symlink():
            return False, "Snapshot payload is missing"

        original.parent.mkdir(parents=True, exist_ok=True)
        if original.exists():
            if not overwrite:
                return False, "Original path already exists"
        stage = original.with_name(f".{original.name}.recovery-staging")
        old = original.with_name(f".{original.name}.recovery-old")
        if stage.exists() or stage.is_symlink():
            if stage.is_dir() and not stage.is_symlink():
                shutil.rmtree(stage)
            else:
                stage.unlink()
        if old.exists() or old.is_symlink():
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old)
            else:
                old.unlink()
        if snapshot.is_symlink():
            stage.symlink_to(snapshot.readlink())
        elif snapshot.is_dir():
            shutil.copytree(str(snapshot), str(stage), symlinks=True)
        else:
            shutil.copy2(str(snapshot), str(stage), follow_symlinks=False)
        moved_old = False
        try:
            if original.exists() or original.is_symlink():
                original.rename(old)
                moved_old = True
            stage.rename(original)
            if moved_old:
                from asm.services.cleaner import record_trash

                record_trash(old, "recovery-overwrite")
                send2trash(str(old))
        except OSError:
            if original.exists() or original.is_symlink():
                if original.is_dir() and not original.is_symlink():
                    shutil.rmtree(original)
                else:
                    original.unlink()
            if moved_old:
                old.rename(original)
            if stage.exists() or stage.is_symlink():
                if stage.is_dir() and not stage.is_symlink():
                    shutil.rmtree(stage)
                else:
                    stage.unlink()
            raise
        rows_file = item_root / "codex_threads.json"
        if rows_file.exists():
            from asm.services import codex_data

            try:
                codex_data.restore_thread_rows(json.loads(rows_file.read_text()))
            except Exception as exc:  # the file is back; say what is missing instead of failing
                return True, f"{original} (Codex index rows not restored: {exc})"
        return True, str(original)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, shutil.Error) as exc:
        logger.warning("Failed to restore recovery item %s: %s", item_id, exc)
        return False, str(exc)


def delete_recovery_item(item_id: str) -> bool:
    """Delete a recovery snapshot."""
    try:
        item_root = _recovery_root(item_id)
        if not item_root.exists() or not (item_root / "metadata.json").is_file():
            return False
        from asm.services.cleaner import record_trash

        record_trash(item_root, "recovery")
        send2trash(str(item_root))
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Failed to delete recovery item %s: %s", item_id, exc)
        return False
