"""Full-text search over session bodies.

The project filter matches session *titles* instantly (see ``ProjectsScreen``);
this module backs the heavier full-text pass that scans the actual conversation
JSONL bodies so a query can find sessions where the term only appears mid-chat.

ripgrep is used when available (fast, literal, case-insensitive) with a plain
Python scan as a fallback. Hits stream out as they are found and a scan is
killed as soon as the caller cancels it. Claude stores ``<session_id>.jsonl``
(plus ``<session_id>/subagents/*.jsonl``) per project dir, so a hit maps to the
session id path component; Codex stores global
``rollout-*.jsonl`` files, identified by absolute path (matching
``SessionDetail.project_dir`` for Codex sessions).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from asm.models import CODEX_SESSIONS_DIR, PROJECTS_DIR


class SearchError(RuntimeError):
    pass


def normalize(text: str) -> str:
    """Casefold and strip all whitespace, so spacing never affects matching.

    A user who half-remembers a phrase can type "warp한글" or "warp 한글" and
    get the same hits. Title and body search both compare via this, keeping the
    two paths consistent.
    """
    return "".join(text.split()).casefold()


def _flexible_regex(query: str) -> str | None:
    """Regex matching ``query`` while ignoring whitespace differences.

    Each non-space character is joined by ``\\s*``, so "warp한글" also matches a
    body containing "warp 한글" and vice-versa. Returns None for an all-space /
    empty query. Characters are escaped, so regex metacharacters are literal.
    """
    chars = [re.escape(c) for c in query if not c.isspace()]
    if not chars:
        return None
    return r"\s*".join(chars)


def _never() -> bool:
    return False


def _iter_ripgrep(pattern: str, root: Path, glob: str, cancelled: Callable[[], bool]) -> Iterator[Path]:
    """Yield matching files as ripgrep reports them; killed early when cancelled."""
    # stderr to a file: a PIPE nobody drains can fill and stall ripgrep.
    errfile = tempfile.TemporaryFile()
    proc = subprocess.Popen(
        [shutil.which("rg"), "-l", "-i", "-e", pattern, "-g", glob, str(root)],
        stdout=subprocess.PIPE,
        stderr=errfile,
        text=True,
    )
    try:
        for line in proc.stdout:
            if cancelled():
                return
            if line := line.rstrip("\n"):
                yield Path(line)
        # exit 0 = matches, 1 = no matches, 2 = error (unreadable file, bad root).
        if proc.wait() not in (0, 1):
            errfile.seek(0)
            detail = errfile.read().decode(errors="replace").strip()
            raise SearchError(f"ripgrep failed under {root}: {detail}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        errfile.close()


def _iter_python(query: str, root: Path, glob: str, cancelled: Callable[[], bool]) -> Iterator[Path]:
    """Content scan for machines without ripgrep (slower)."""
    needle = normalize(query)
    try:
        for f in root.rglob(glob):
            if cancelled():
                return
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    if any(needle in normalize(line) for line in fh):
                        yield f
            except OSError as exc:
                raise SearchError(f"cannot read search file {f}: {exc}") from exc
    except OSError as exc:
        raise SearchError(f"cannot read search root {root}: {exc}") from exc


def _iter_grep(query: str, root: Path, glob: str, cancelled: Callable[[], bool] = _never) -> Iterator[Path]:
    """Files under ``root`` matching ``glob`` whose body contains ``query``
    (whitespace- and case-insensitive), streamed as they are found."""
    pattern = _flexible_regex(query)
    if pattern is None or not root.exists():
        return
    if shutil.which("rg"):
        yield from _iter_ripgrep(pattern, root, glob, cancelled)
    else:
        yield from _iter_python(query, root, glob, cancelled)


def _grep_files(query: str, root: Path, glob: str) -> list[Path]:
    return list(_iter_grep(query, root, glob))


def iter_claude_hits(query: str, cancelled: Callable[[], bool] = _never) -> Iterator[tuple[str, bool]]:
    """``(session_id, via_subagent)`` for Claude transcripts containing ``query``.

    A session may be yielded twice: once for a subagent transcript hit and once
    for its own transcript, in whichever order ripgrep finds them.
    """
    seen: set[tuple[str, bool]] = set()
    for f in _iter_grep(query.strip(), PROJECTS_DIR, "*.jsonl", cancelled):
        try:
            parts = f.relative_to(PROJECTS_DIR).parts
        except ValueError:
            continue
        # <project>/<sid>.jsonl, or <project>/<sid>/subagents/agent-*.jsonl
        if len(parts) < 2:
            continue
        hit = (parts[1].removesuffix(".jsonl"), len(parts) > 2)
        if hit not in seen:
            seen.add(hit)
            yield hit


def iter_claude_session_ids(query: str, cancelled: Callable[[], bool] = _never) -> Iterator[str]:
    """Claude session ids whose transcript (or subagent transcript) contains ``query``."""
    seen: set[str] = set()
    for sid, _sub in iter_claude_hits(query, cancelled):
        if sid not in seen:
            seen.add(sid)
            yield sid


def iter_codex_rollout_paths(query: str, cancelled: Callable[[], bool] = _never) -> Iterator[str]:
    """Absolute paths of Codex rollout files whose body contains ``query``."""
    for f in _iter_grep(query.strip(), CODEX_SESSIONS_DIR, "rollout-*.jsonl", cancelled):
        yield str(f)


def search_claude_session_ids(query: str) -> set[str]:
    return set(iter_claude_session_ids(query))


def search_codex_rollout_paths(query: str) -> set[str]:
    return set(iter_codex_rollout_paths(query))
