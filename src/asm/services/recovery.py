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

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")

# Retention for recovery snapshots — without this the "cleanup" tool would grow
# ~/.asm/recovery without bound (every trash duplicates data here + OS trash).
_MAX_ITEMS = 100
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def _validate_original_path(path: Path) -> Path:
    from asm.services import codex_data

    resolved = path.resolve()
    roots = (CLAUDE_DIR.resolve(), codex_data.CODEX_SESSIONS_DIR.parent.resolve())
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError(f"Recovery path outside managed data dirs: {path}")
    return resolved


def _recovery_root(item_id: str) -> Path:
    root = (RECOVERY_BASE_DIR / item_id).resolve()
    if not root.is_relative_to(RECOVERY_BASE_DIR.resolve()):
        raise ValueError(f"Recovery item outside recovery dir: {item_id}")
    return root


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
    if not path.exists():
        return None

    try:
        original = _validate_original_path(path)
        RECOVERY_BASE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            RECOVERY_BASE_DIR.chmod(0o700)  # may hold session transcripts
        except OSError:
            pass

        item_id = f"{datetime.now():%Y%m%d-%H%M%S-%f}-{_safe_name(original.name)}"
        item_root = RECOVERY_BASE_DIR / item_id
        payload_root = item_root / "payload"
        payload_root.mkdir(parents=True, exist_ok=True)

        snapshot = payload_root / original.name
        if original.is_dir():
            shutil.copytree(
                str(original),
                str(snapshot),
                symlinks=True,
                ignore_dangling_symlinks=True,
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
        (item_root / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2)
        )
        _prune_snapshots()
        return item_id
    except (OSError, ValueError, shutil.Error) as exc:
        logger.warning("Failed to create recovery snapshot for %s: %s", path, exc)
        return None


def _prune_snapshots() -> None:
    """Drop oldest snapshots beyond the item-count or total-size cap."""
    try:
        items = [d for d in RECOVERY_BASE_DIR.iterdir() if d.is_dir()]
    except OSError:
        return
    items.sort(key=lambda d: d.name)  # timestamp-prefixed → chronological
    sizes = {d: _payload_size(d) for d in items}
    total = sum(sizes.values())
    # Oldest-first removal until within both caps.
    while items and (len(items) > _MAX_ITEMS or total > _MAX_TOTAL_BYTES):
        victim = items.pop(0)
        total -= sizes.get(victim, 0)
        try:
            shutil.rmtree(victim, ignore_errors=True)
        except OSError:
            pass


def list_recovery_items() -> list[RecoveryInfo]:
    """List app-managed recovery snapshots."""
    if not RECOVERY_BASE_DIR.exists():
        return []

    result: list[RecoveryInfo] = []
    try:
        for item_root in sorted(RECOVERY_BASE_DIR.iterdir(), reverse=True):
            if not item_root.is_dir():
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
        original = _validate_original_path(Path(data["original_path"]))
        # Trust boundary: the snapshot source must live under the recovery dir,
        # so a tampered metadata.json can't copy an arbitrary file into a managed
        # location (CWE-22/502).
        snapshot = Path(data["snapshot_path"])
        if not snapshot.resolve().is_relative_to(RECOVERY_BASE_DIR.resolve()):
            return False, "Snapshot path outside recovery dir"
        if not snapshot.exists():
            return False, "Snapshot payload is missing"

        original.parent.mkdir(parents=True, exist_ok=True)
        if original.exists():
            if not overwrite:
                return False, "Original path already exists"
            send2trash(str(original))

        if snapshot.is_dir():
            shutil.copytree(
                str(snapshot),
                str(original),
                symlinks=True,
                ignore_dangling_symlinks=True,
            )
        else:
            shutil.copy2(str(snapshot), str(original), follow_symlinks=False)
        return True, str(original)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, shutil.Error) as exc:
        logger.warning("Failed to restore recovery item %s: %s", item_id, exc)
        return False, str(exc)


def delete_recovery_item(item_id: str) -> bool:
    """Delete a recovery snapshot."""
    try:
        item_root = _recovery_root(item_id)
        if not item_root.exists():
            return False
        send2trash(str(item_root))
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Failed to delete recovery item %s: %s", item_id, exc)
        return False
