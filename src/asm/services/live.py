"""Detect sessions that a running Claude Code or Codex process is still using.

Moving or trashing such a session is refused: Claude would keep appending to
the old location, and Codex's app server holds rollouts open for append, so it
would keep writing to the replaced (unlinked) file and lose those turns.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

_PROC = Path("/proc")


class LiveSessionError(OSError):
    pass


def _proc_alive(pid: str, proc_start: str | None) -> bool:
    stat = _PROC / pid / "stat"
    if _PROC.is_dir():
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return False
        # Field 22 (starttime) guards against a recycled pid.
        return not proc_start or fields[19] == str(proc_start)
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def live_claude_sessions() -> dict[str, dict]:
    """``{session_id: {"pid", "cwd"}}`` for Claude Code processes still running."""
    from asm import models

    live: dict[str, dict] = {}
    current = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if current:
        live[current] = {"pid": os.environ.get("CLAUDE_PID", "?"), "cwd": None}
    sessions_dir = models.CLAUDE_DIR / "sessions"
    if not sessions_dir.is_dir():
        return live
    for f in sessions_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # rewritten concurrently; the process re-registers itself
        sid, pid = data.get("sessionId"), str(data.get("pid", ""))
        if isinstance(sid, str) and pid.isdigit() and _proc_alive(pid, data.get("procStart")):
            live[sid] = {"pid": pid, "cwd": data.get("cwd")}
    return live


def open_writers(paths: Iterable[Path]) -> dict[str, list[str]]:
    """``{path: [pid, ...]}`` for processes holding any of ``paths`` open for writing.

    shortcut: Linux /proc only; on other platforms this returns {} and only the
    session registries / environment ids guard against live sessions.
    """
    targets = {os.path.realpath(p) for p in paths}
    found: dict[str, list[str]] = {}
    if not targets or not _PROC.is_dir():
        return found
    for proc in _PROC.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fds = list((proc / "fd").iterdir())
        except OSError:
            continue  # other users' processes / exited
        for fd in fds:
            try:
                target = os.readlink(fd)
                if target not in targets:
                    continue
                flags = next(
                    int(line.split()[1], 8)
                    for line in (proc / "fdinfo" / fd.name).read_text().splitlines()
                    if line.startswith("flags:")
                )
            except (OSError, StopIteration, ValueError):
                continue
            if flags & (os.O_WRONLY | os.O_RDWR):
                found.setdefault(target, []).append(proc.name)
    return found


def ensure_claude_not_live(session_ids: Iterable[str], files: Iterable[Path] = ()) -> None:
    live = live_claude_sessions()
    busy = [f"{sid} (pid {live[sid]['pid']})" for sid in session_ids if sid in live]
    busy += [f"{p} (pid {', '.join(pids)})" for p, pids in open_writers(files).items()]
    if busy:
        raise LiveSessionError(
            "session in use by a running Claude Code; exit it first: " + "; ".join(busy)
        )


def ensure_codex_not_live(session_ids: Iterable[str], rollouts: Iterable[Path]) -> None:
    current = os.environ.get("CODEX_THREAD_ID")
    busy = [f"{sid} (this Codex session)" for sid in session_ids if current and sid == current]
    busy += [f"{p} (pid {', '.join(pids)})" for p, pids in open_writers(rollouts).items()]
    if busy:
        raise LiveSessionError(
            "session in use by a running Codex; close that thread or stop the app server first: "
            + "; ".join(busy)
        )
