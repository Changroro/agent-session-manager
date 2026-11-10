"""Relocate a whole project folder together with its Claude Code / Codex data.

Everything here is mechanical: the folder rename, session moves (via the same
code as ``asm move``), the project keys in ~/.claude.json and Codex's
config.toml, and ``git worktree repair``. Other files that still mention the
old path are only *listed* for a human or agent to judge; nothing guesses.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from asm.models import encode_path

_REFERENCE_LIMIT = 50
_TOML_PROJECT_HEADER = re.compile(r'^(\[projects\.)"([^"\\]*)"(\]\s*)$', re.M)


class RelocateError(ValueError):
    pass


@dataclass
class RelocatePlan:
    old: str
    new: str
    claude_projects: list[tuple[str, str, list[str]]] = field(default_factory=list)  # (old, new, session ids)
    codex_sessions: list[tuple[str, str, str]] = field(default_factory=list)  # (id, rollout path, new cwd)
    claude_config_keys: list[str] = field(default_factory=list)
    codex_config_keys: list[str] = field(default_factory=list)
    git: bool = False


@dataclass
class RelocateResult:
    folder_moved: bool = False
    claude_moved: int = 0
    codex_moved: int = 0
    failures: list[str] = field(default_factory=list)
    git_repair: str = ""
    venv: bool = False
    references: list[str] = field(default_factory=list)


def _under(path: str, root: str) -> str | None:
    """Suffix of ``path`` below ``root`` ("" for root itself), or None if outside."""
    if path == root:
        return ""
    if path.startswith(root + "/"):
        return path[len(root):]
    return None


def _codex_config() -> Path:
    from asm.services import codex_data

    return codex_data.CODEX_SESSIONS_DIR.parent / "config.toml"


def plan(old: str, new: str) -> RelocatePlan:
    from asm.services import claude_data, codex_data

    old = os.path.normpath(os.path.abspath(os.path.expanduser(old)))
    new = os.path.normpath(os.path.abspath(os.path.expanduser(new)))
    src, dest = Path(old), Path(new)
    if old == new:
        raise RelocateError("source and destination are the same")
    if src.is_symlink() or not src.is_dir():
        raise RelocateError(f"not a project folder: {old}")
    if dest.exists() or dest.is_symlink():
        raise RelocateError(f"destination already exists: {new}")
    if not dest.parent.is_dir():
        raise RelocateError(f"destination parent does not exist: {dest.parent}")
    if _under(new, old) is not None:
        raise RelocateError("destination is inside the source folder")
    if src.stat().st_dev != dest.parent.stat().st_dev:
        raise RelocateError("destination is on another filesystem; move the folder yourself, then use `asm move`")

    projects_dir = claude_data.PROJECTS_DIR
    p = RelocatePlan(old=old, new=new, git=(src / ".git").exists())
    for proj in claude_data.get_projects():
        rest = _under(proj.path, old)
        if rest is None:
            continue
        sdir = projects_dir / encode_path(proj.path)
        ids = sorted(f.stem for f in sdir.glob("*.jsonl")) if sdir.is_dir() else []
        target = new + rest
        tdir = projects_dir / encode_path(target)
        clash = [i for i in ids if (tdir / f"{i}.jsonl").exists() or (tdir / i).exists()]
        if clash:
            raise RelocateError(f"sessions already exist for {target}: {', '.join(clash[:3])}")
        if ids:
            p.claude_projects.append((proj.path, target, ids))
    if codex_data.is_available():
        for info in codex_data._scanned_sessions():
            rest = _under(info["cwd"] or "", old)
            if rest is not None and Path(info["path"]).exists():
                p.codex_sessions.append((info["id"], info["path"], new + rest))
    p.claude_config_keys = [k for k in claude_data.load_claude_json().get("projects", {}) if _under(k, old) is not None]
    cfg = _codex_config()
    if cfg.exists():
        p.codex_config_keys = [
            m.group(2) for m in _TOML_PROJECT_HEADER.finditer(cfg.read_text(encoding="utf-8"))
            if _under(m.group(2), old) is not None
        ]
    return p


def _ensure_nothing_live(p: RelocatePlan) -> None:
    from asm.services import claude_data
    from asm.services.live import LiveSessionError, ensure_claude_not_live, ensure_codex_not_live, live_claude_sessions

    inside = [
        f"pid {info['pid']} in {info['cwd']}"
        for info in live_claude_sessions().values()
        if info.get("cwd") and _under(info["cwd"], p.old) is not None
    ]
    if inside:
        raise LiveSessionError("a running Claude Code is working inside the folder; exit it first: " + "; ".join(inside))
    ensure_claude_not_live(
        [sid for _o, _n, ids in p.claude_projects for sid in ids],
        [claude_data.PROJECTS_DIR / encode_path(o) / f"{sid}.jsonl" for o, _n, ids in p.claude_projects for sid in ids],
    )
    ensure_codex_not_live([sid for sid, _r, _c in p.codex_sessions], [Path(r) for _s, r, _c in p.codex_sessions])


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise


def _move_path(value: str, old: str, new: str) -> str:
    rest = _under(value, old)
    return value if rest is None else new + rest


def _rewrite_claude_config(old: str, new: str) -> None:
    from asm.services import claude_data

    data = claude_data.load_claude_json()
    if not data:
        return
    projects = data.get("projects", {})
    moved = {_move_path(k, old, new): v for k, v in projects.items() if _under(k, old) is not None}
    data["projects"] = {k: v for k, v in projects.items() if _under(k, old) is None and k not in moved} | moved
    repos = data.get("githubRepoPaths")
    if isinstance(repos, dict):
        for repo, paths in repos.items():
            if isinstance(paths, list):
                repos[repo] = list(dict.fromkeys(_move_path(x, old, new) if isinstance(x, str) else x for x in paths))
    _atomic_write(claude_data.CLAUDE_JSON, json.dumps(data, indent=2, ensure_ascii=False))


def _rewrite_codex_config(old: str, new: str) -> None:
    import tomllib

    cfg = _codex_config()
    text = cfg.read_text(encoding="utf-8")
    updated = _TOML_PROJECT_HEADER.sub(lambda m: f'{m.group(1)}"{_move_path(m.group(2), old, new)}"{m.group(3)}', text)
    tomllib.loads(updated)  # never write a config Codex can't parse
    if updated != text:
        _atomic_write(cfg, updated)


def _references(root: Path, old: str) -> list[str]:
    """Files under ``root`` that still contain ``old`` (gitignored files skipped)."""
    rg = shutil.which("rg")
    if rg:
        try:
            out = subprocess.run(
                [rg, "-l", "-F", "--hidden", "-g", "!.git", "-g", "!.venv", "-g", "!node_modules", old, str(root)],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return [f"(reference scan timed out; search {root} for {old} yourself)"]
        return sorted(out.stdout.splitlines())[:_REFERENCE_LIMIT]
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", ".venv", "node_modules"}]
        for name in filenames:
            f = Path(dirpath) / name
            try:
                if old.encode() in f.read_bytes():
                    hits.append(str(f))
            except OSError:
                continue
            if len(hits) >= _REFERENCE_LIMIT:
                return hits
    return hits


def apply(p: RelocatePlan) -> RelocateResult:
    """Run a plan. The folder moves first (one atomic rename); later steps that
    fail are reported in ``failures`` and can be retried with ``asm move``."""
    from asm.services import backup, claude_data, codex_data, migrate
    from asm.services.recovery import create_recovery_snapshot

    _ensure_nothing_live(p)
    if p.claude_config_keys and not backup.create_config_backup():
        raise RelocateError("could not back up ~/.claude.json; nothing was changed")
    if p.codex_config_keys and not create_recovery_snapshot(_codex_config(), "codex-config"):
        raise RelocateError("could not snapshot Codex config.toml; nothing was changed")

    result = RelocateResult()
    os.rename(p.old, p.new)
    result.folder_moved = True

    for old_path, new_path, ids in p.claude_projects:
        r = migrate.migrate_sessions(old_path, new_path, session_ids=ids)
        result.claude_moved += r.sessions_copied
        if not r.success:
            result.failures.append(f"Claude {old_path}: {r.message}")
            continue
        old_dir = claude_data.PROJECTS_DIR / encode_path(old_path)
        for d in (old_dir / "memory", old_dir):
            try:
                d.rmdir()  # only when empty; anything left over stays put
            except OSError:
                pass
    for sid, rollout, cwd in p.codex_sessions:
        try:
            ok = codex_data.move_session(rollout, cwd)
        except OSError as exc:
            ok, reason = False, str(exc)
        else:
            reason = "see log"
        if ok:
            result.codex_moved += 1
        else:
            result.failures.append(f"Codex {sid}: {reason}")
    try:
        if p.claude_config_keys:
            _rewrite_claude_config(p.old, p.new)
    except (OSError, ValueError) as exc:
        result.failures.append(f"~/.claude.json: {exc}")
    try:
        if p.codex_config_keys:
            _rewrite_codex_config(p.old, p.new)
    except (OSError, ValueError) as exc:
        result.failures.append(f"Codex config.toml: {exc}")

    if p.git and shutil.which("git"):
        out = subprocess.run(["git", "-C", p.new, "worktree", "repair"], capture_output=True, text=True)
        result.git_repair = (out.stdout + out.stderr).strip() or "ok"
        if out.returncode != 0:
            result.failures.append(f"git worktree repair: {result.git_repair}")
    result.venv = (Path(p.new) / ".venv").is_dir()
    result.references = _references(Path(p.new), p.old)
    return result
