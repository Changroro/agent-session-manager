"""Cross-agent import of MCP servers and sessions between Claude Code and Codex."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from asm import models
from asm.models import CLAUDE_JSON, CODEX_DIR, CODEX_SESSIONS_DIR, PROJECTS_DIR, encode_path
from asm.utils import strict_child, validate_identifier

logger = logging.getLogger(__name__)

CODEX_CONFIG = CODEX_DIR / "config.toml"
CODEX_IMPORT_RECORDS = CODEX_DIR / "external_agent_session_imports.json"


# Tests point the CLIs at throwaway homes via CODEX_HOME / CLAUDE_CONFIG_DIR.
SUBPROCESS_ENV: dict[str, str] = {}

CLI_TIMEOUT = 60

CLAUDE = "claude"
CODEX = "codex"

IMPORT_ORIGINATOR = "asm"
TITLE_MAX_CHARS = 80
# Hashing every transcript costs a full read; this box already holds 25k Codex
# rollouts, so a plan covers the newest N and reports the rest as truncated.
SESSION_PLAN_LIMIT = 200


def _has_entries(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_file():
        return True
    return any(path.iterdir())


@dataclass(frozen=True)
class McpServer:
    """An MCP server definition normalized across both agents."""

    name: str
    transport: str
    url: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class McpPlan:
    """What an MCP import would do, before anything is written."""

    source: str
    target: str
    new: list[McpServer] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class McpResult:
    """Outcome of applying an :class:`McpPlan`."""

    imported: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    backup_path: str | None = None


class AgentImportError(RuntimeError):
    """Raised when an import cannot proceed."""


def claude_import_records() -> Path:
    """Claude Code keeps no importer ledger of its own, so asm tracks this direction.

    Resolved on each call so tests that repoint ``models.APP_DATA_DIR`` are honored.
    """
    return models.APP_DATA_DIR / "codex_session_imports.json"


def read_codex_mcp() -> dict[str, McpServer]:
    """Read MCP servers from Codex's config.toml."""
    if not CODEX_CONFIG.exists():
        return {}
    try:
        with CODEX_CONFIG.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentImportError(f"cannot read {CODEX_CONFIG}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("mcp_servers", {}), dict):
        raise AgentImportError(f"unexpected MCP schema in {CODEX_CONFIG}")
    servers: dict[str, McpServer] = {}
    for name, entry in data.get("mcp_servers", {}).items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise AgentImportError(f"unexpected MCP entry in {CODEX_CONFIG}")
        args = entry.get("args", [])
        env = entry.get("env", {})
        headers = entry.get("http_headers", {})
        if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
            raise AgentImportError(f"unexpected MCP args for {name}")
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise AgentImportError(f"unexpected MCP env for {name}")
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise AgentImportError(f"unexpected MCP headers for {name}")
        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AgentImportError(f"unexpected MCP enabled flag for {name}")
        url = entry.get("url")
        servers[name] = McpServer(
            name=name,
            transport="http" if url else "stdio",
            url=url,
            command=entry.get("command"),
            args=args,
            env=env,
            headers=headers,
            enabled=enabled,
        )
    return servers


def read_claude_mcp() -> dict[str, McpServer]:
    """Read user-scoped MCP servers from Claude Code's .claude.json."""
    if not CLAUDE_JSON.exists():
        return {}
    try:
        data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentImportError(f"cannot read {CLAUDE_JSON}: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise AgentImportError(f"unexpected MCP schema in {CLAUDE_JSON}")
    servers: dict[str, McpServer] = {}
    for name, entry in data.get("mcpServers", {}).items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise AgentImportError(f"unexpected MCP entry in {CLAUDE_JSON}")
        args = entry.get("args", [])
        env = entry.get("env", {})
        headers = entry.get("headers", {})
        if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
            raise AgentImportError(f"unexpected MCP args for {name}")
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise AgentImportError(f"unexpected MCP env for {name}")
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise AgentImportError(f"unexpected MCP headers for {name}")
        disabled = entry.get("disabled", False)
        if not isinstance(disabled, bool):
            raise AgentImportError(f"unexpected MCP disabled flag for {name}")
        transport = entry.get("type") or ("http" if entry.get("url") else "stdio")
        servers[name] = McpServer(
            name=name,
            transport=transport,
            url=entry.get("url"),
            command=entry.get("command"),
            args=args,
            env=env,
            headers=headers,
            enabled=not disabled,
        )
    return servers


def _unsupported_reason(server: McpServer, target: str) -> str | None:
    if server.transport not in ("http", "stdio"):
        return f"{server.transport} transport has no equivalent in {target}"
    if server.transport == "http" and not server.url:
        return "http server without a url"
    if server.transport == "stdio" and not server.command:
        return "stdio server without a command"
    return None


def plan_mcp(direction: str) -> McpPlan:
    """Compare both sides and report what a ``direction`` import would add.

    ``direction`` is ``"claude-to-codex"`` or ``"codex-to-claude"``.
    """
    if direction == "claude-to-codex":
        source, target = "claude", "codex"
        src, dst = read_claude_mcp(), read_codex_mcp()
    elif direction == "codex-to-claude":
        source, target = "codex", "claude"
        src, dst = read_codex_mcp(), read_claude_mcp()
    else:
        raise ValueError(f"unknown direction: {direction}")

    plan = McpPlan(source=source, target=target)
    for name, server in src.items():
        if name in dst:
            plan.already_present.append(name)
            continue
        if not server.enabled:
            plan.unsupported.append((name, "disabled at source"))
            continue
        reason = _unsupported_reason(server, target)
        if reason:
            plan.unsupported.append((name, reason))
            continue
        plan.new.append(server)
    return plan


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **SUBPROCESS_ENV}
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=CLI_TIMEOUT, env=env, check=False
    )


def _codex_add_argv(server: McpServer) -> list[str]:
    argv = [CODEX, "mcp", "add", server.name]
    if server.transport == "http":
        argv += ["--url", server.url or ""]
        return argv
    for key, value in server.env.items():
        argv += ["--env", f"{key}={value}"]
    argv += ["--", server.command or "", *server.args]
    return argv


def _claude_add_argv(server: McpServer) -> list[str]:
    # -H/-e are variadic, so they have to trail the positional name and url.
    argv = [CLAUDE, "mcp", "add", "-s", "user"]
    if server.transport == "http":
        argv += ["--transport", "http", server.name, server.url or ""]
        for key, value in server.headers.items():
            argv += ["-H", f"{key}: {value}"]
        return argv
    argv += [server.name]
    for key, value in server.env.items():
        argv += ["-e", f"{key}={value}"]
    argv += ["--", server.command or "", *server.args]
    return argv


def _append_codex_headers(server: McpServer) -> None:
    """Write http_headers for a server `codex mcp add` cannot express itself."""
    if not CODEX_CONFIG.exists():
        raise AgentImportError(f"Codex CLI did not create {CODEX_CONFIG}")
    lines = [f"\n[mcp_servers.{json.dumps(server.name)}.http_headers]\n"]
    lines += [f"{json.dumps(key)} = {json.dumps(value)}\n" for key, value in server.headers.items()]
    payload = CODEX_CONFIG.read_text(encoding="utf-8") + "".join(lines)
    tmp = CODEX_CONFIG.with_suffix(".toml.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(CODEX_CONFIG)
    except OSError:
        if tmp.exists():
            tmp.unlink()
        raise


def _redact_server_text(server: McpServer, text: str) -> str:
    redacted = text
    for value in (*server.env.values(), *server.headers.values(), server.url or ""):
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", redacted)


def _restore_config_bytes(before: bytes | None) -> None:
    if before is None:
        if CODEX_CONFIG.exists():
            CODEX_CONFIG.unlink()
        return
    tmp = CODEX_CONFIG.with_suffix(".rollback.tmp")
    tmp.write_bytes(before)
    tmp.replace(CODEX_CONFIG)


def apply_mcp(plan: McpPlan, dry_run: bool = False) -> McpResult:
    """Add every server in ``plan.new`` to the target agent via its own CLI."""
    result = McpResult()
    if dry_run:
        result.imported = [s.name for s in plan.new]
        return result
    if not plan.new:
        return result

    from asm.services import backup

    target_config = CODEX_CONFIG if plan.target == "codex" else CLAUDE_JSON
    if target_config.exists():
        create_backup = backup.create_codex_backup if plan.target == "codex" else backup.create_config_backup
        result.backup_path = create_backup()
        if not result.backup_path:
            raise AgentImportError("MCP target safety backup failed")

    for server in plan.new:
        argv = _codex_add_argv(server) if plan.target == "codex" else _claude_add_argv(server)
        before = CODEX_CONFIG.read_bytes() if plan.target == "codex" and CODEX_CONFIG.exists() else None
        try:
            proc = _run(argv)
        except subprocess.TimeoutExpired:
            result.failed.append((server.name, "MCP command timed out"))
            continue
        except OSError as exc:
            result.failed.append((server.name, _redact_server_text(server, f"MCP command failed: {exc}")))
            continue
        if proc.returncode != 0:
            detail = _redact_server_text(server, (proc.stderr or proc.stdout).strip()[:200])
            result.failed.append((server.name, detail or "MCP command failed"))
            continue
        if plan.target == "codex" and server.headers:
            try:
                _append_codex_headers(server)
            except (OSError, AgentImportError) as exc:
                try:
                    _restore_config_bytes(before)
                except OSError as rollback_error:
                    raise AgentImportError(f"MCP rollback failed: {rollback_error}") from rollback_error
                result.failed.append((server.name, f"headers not written: {_redact_server_text(server, str(exc))}"))
                continue
        result.imported.append(server.name)
    return result


# --- Session import: Claude Code -> Codex ---


@dataclass(frozen=True)
class Turn:
    """One text message lifted out of a session transcript."""

    role: str
    text: str
    timestamp: str


@dataclass
class SessionCandidate:
    """A Claude session file considered for import into Codex."""

    path: str
    title: str
    turns: int
    modified_at: int
    digest: str
    cwd: str
    total_tokens: int = 0


@dataclass
class SessionPlan:
    """What a session import would do, before anything is written."""

    new: list[SessionCandidate] = field(default_factory=list)
    already_imported: list[SessionCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    truncated: int = 0


@dataclass
class SessionResult:
    """Outcome of applying a :class:`SessionPlan`."""

    imported: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    backup_path: str | None = None


def _claude_text(content) -> str:
    """Join the text blocks of a Claude message, dropping thinking and tool calls."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            raise AgentImportError("unexpected Claude content block")
        if block.get("type") == "text":
            text = block.get("text")
            if text is not None and not isinstance(text, str):
                raise AgentImportError("unexpected Claude text block")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _is_injected_user_line(obj: dict, text: str) -> bool:
    """Turns Claude Code inserts itself (`isMeta`, slash-command wrappers).

    Codex has no equivalent: a `<teammate-message>` there is real conversation
    input, so the rollout reader deliberately keeps those.
    """
    if obj.get("isMeta"):
        return True
    return (
        text.startswith(("<command-name>", "<local-command", "<system-reminder>", "<task-notification>"))
        or text.startswith(("# AGENTS.md", "# CLAUDE.md"))
    )


def read_claude_session(path: Path) -> tuple[list[Turn], str, int]:
    """Extract text turns, cwd and total token usage from a Claude session file."""
    turns: list[Turn] = []
    cwd = ""
    total_tokens = 0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise AgentImportError(f"malformed Claude transcript: {path}") from exc
                if not isinstance(obj, dict):
                    continue
                kind = obj.get("type")
                if kind not in ("user", "assistant"):
                    continue
                value_cwd = obj.get("cwd")
                if value_cwd is not None and not isinstance(value_cwd, str):
                    raise AgentImportError(f"unexpected cwd in {path}")
                cwd = value_cwd or cwd
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if isinstance(usage, dict):
                    try:
                        total_tokens += int(usage.get("input_tokens") or 0)
                        total_tokens += int(usage.get("output_tokens") or 0)
                    except (TypeError, ValueError) as exc:
                        raise AgentImportError(f"unexpected token usage in {path}") from exc
                text = _claude_text(message.get("content"))
                if not text:
                    continue
                if kind == "user" and _is_injected_user_line(obj, text):
                    continue
                turns.append(
                    Turn(role=kind, text=text, timestamp=obj.get("timestamp") or "")
                )
    except OSError as exc:
        raise AgentImportError(f"cannot read {path}: {exc}") from exc
    return turns, cwd, total_tokens


def _title_of(turns: list[Turn]) -> str:
    """Label a session by its first real user turn.

    Harness preambles (`<recommended_plugins>`, a leading `# AGENTS.md` dump) are
    kept in the transcript but make useless labels — every session would read the
    same. Matches the prefixes Claude's own reader treats as injected.
    """
    users = [turn for turn in turns if turn.role == "user"]
    spoken = next(
        (turn for turn in users if not turn.text.startswith(("<", "#"))), None
    )
    chosen = spoken or (users[0] if users else turns[0])
    return chosen.text.replace("\n", " ")[:TITLE_MAX_CHARS]


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_import_records() -> dict:
    if not CODEX_IMPORT_RECORDS.exists():
        return {"records": []}
    try:
        data = json.loads(CODEX_IMPORT_RECORDS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentImportError(f"cannot read {CODEX_IMPORT_RECORDS}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise AgentImportError(f"unexpected shape in {CODEX_IMPORT_RECORDS}")
    return data


def _newest_first(paths) -> list[Path]:
    def when(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(paths, key=when, reverse=True)


def claude_session_files() -> list[Path]:
    """Every Claude session transcript under ~/.claude/projects, newest first."""
    if not PROJECTS_DIR.exists():
        return []
    # One level only — deeper files are <session>/subagents/agent-*.jsonl, which
    # belong to a parent session rather than being sessions of their own.
    return _newest_first(path for path in PROJECTS_DIR.glob("*/*.jsonl") if not path.is_symlink())


def plan_sessions_to_codex(
    paths: list[Path] | None = None, limit: int | None = None
) -> SessionPlan:
    """Decide which Claude sessions still need importing into Codex.

    Sessions Codex already imported are matched by content hash, the same key the
    official importer uses, so re-importing an unchanged transcript is a no-op.
    """
    records = _load_import_records()["records"]
    known = {
        rec.get("content_sha256")
        for rec in records
        if isinstance(rec, dict) and rec.get("content_sha256")
    }

    plan = SessionPlan()
    limit = SESSION_PLAN_LIMIT if limit is None else limit
    considered = paths if paths is not None else claude_session_files()
    if paths is None and len(considered) > limit:
        plan.truncated = len(considered) - limit
        considered = considered[:limit]
    for path in considered:
        path = Path(path)
        try:
            if path.is_symlink():
                raise AgentImportError(f"Refusing to import symlink: {path}")
            strict_child(path, PROJECTS_DIR)
            turns, cwd, total_tokens = read_claude_session(path)
            digest = _digest(path)
            stat = path.stat()
        except (AgentImportError, OSError) as exc:
            plan.skipped.append((str(path), str(exc)))
            continue
        if not turns:
            plan.skipped.append((str(path), "no text turns"))
            continue
        if not cwd:
            plan.skipped.append((str(path), "transcript has no cwd"))
            continue
        candidate = SessionCandidate(
            path=str(path),
            title=_title_of(turns),
            turns=len(turns),
            modified_at=stat.st_mtime_ns,
            digest=digest,
            cwd=cwd,
            total_tokens=total_tokens,
        )
        if digest in known:
            plan.already_imported.append(candidate)
        else:
            plan.new.append(candidate)
    return plan


def codex_cli_version() -> str:
    """Version string Codex stamps into session_meta; it refuses a rollout without it."""
    try:
        proc = _run([CODEX, "--version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentImportError(f"cannot run `{CODEX} --version`: {exc}") from exc
    if proc.returncode != 0:
        raise AgentImportError(f"`{CODEX} --version` failed: {(proc.stderr or '').strip()}")
    parts = proc.stdout.split()
    if not parts:
        raise AgentImportError(f"`{CODEX} --version` printed nothing")
    return parts[-1]


def _rollout_lines(
    turns: list[Turn],
    cwd: str,
    session_id: str,
    created: datetime,
    total_tokens: int,
    cli_version: str,
) -> list[dict]:
    stamp = created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def wrap(payload: dict, kind: str, when: str = "") -> dict:
        return {"timestamp": when or stamp, "type": kind, "payload": payload}

    lines: list[dict] = [
        wrap(
            {
                "session_id": session_id,
                "id": session_id,
                "timestamp": stamp,
                "cwd": cwd,
                "originator": IMPORT_ORIGINATOR,
                "cli_version": cli_version,
                "model_provider": "openai",
            },
            "session_meta",
        )
    ]

    turn_index = 0
    started = int(created.timestamp())
    for turn in turns:
        when = turn.timestamp or stamp
        if turn.role == "user":
            turn_index += 1
            lines.append(
                wrap(
                    {
                        "type": "task_started",
                        "turn_id": f"external-import-turn-{turn_index}",
                        "started_at": started,
                        "model_context_window": None,
                    },
                    "event_msg",
                    when,
                )
            )
            lines.append(wrap({"type": "user_message", "message": turn.text}, "event_msg", when))
            lines.append(
                wrap(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": turn.text}],
                    },
                    "response_item",
                    when,
                )
            )
            continue
        lines.append(wrap({"type": "agent_message", "message": turn.text}, "event_msg", when))
        lines.append(
            wrap(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": turn.text}],
                },
                "response_item",
                when,
            )
        )

    if turn_index:
        lines.append(
            wrap(
                {
                    "type": "task_complete",
                    "turn_id": f"external-import-turn-{turn_index}",
                    "last_agent_message": None,
                    "started_at": started,
                },
                "event_msg",
            )
        )

    # input/output stay zero so asm never prices an imported copy a second time.
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": total_tokens,
    }
    lines.append(
        wrap(
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": usage,
                    "last_token_usage": usage,
                    "model_context_window": None,
                },
                "rate_limits": None,
            },
            "event_msg",
        )
    )
    return lines


def _rollout_path(session_id: str, created: datetime) -> Path:
    directory = CODEX_SESSIONS_DIR / created.strftime("%Y") / created.strftime("%m") / created.strftime("%d")
    name = f"rollout-{created.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
    return directory / name


def _write_import_record(candidate: SessionCandidate, thread_id: str, when: datetime) -> None:
    data = _load_import_records()
    data["records"].append(
        {
            "source_path": candidate.path,
            "content_sha256": candidate.digest,
            "imported_thread_id": thread_id,
            "imported_at": int(when.timestamp()),
            "source_modified_at": candidate.modified_at,
            "connector_names": [],
            "title": candidate.title,
        }
    )
    CODEX_IMPORT_RECORDS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CODEX_IMPORT_RECORDS.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CODEX_IMPORT_RECORDS)
    except OSError:
        if tmp.exists():
            tmp.unlink()
        raise


def apply_sessions_to_codex(plan: SessionPlan, dry_run: bool = False) -> SessionResult:
    """Write each planned Claude session into Codex as a rollout thread."""
    result = SessionResult()
    if dry_run:
        result.imported = [(c.path, "") for c in plan.new]
        return result
    if not plan.new:
        return result

    from asm.services import backup

    cli_version = codex_cli_version()
    if _has_entries(CODEX_SESSIONS_DIR):
        result.backup_path = backup.create_codex_backup()
        if not result.backup_path:
            raise AgentImportError("Codex session safety backup failed")

    for candidate in plan.new:
        created = datetime.now().astimezone()
        thread_id = str(uuid.uuid4())
        try:
            turns, cwd, total_tokens = read_claude_session(Path(candidate.path))
            lines = _rollout_lines(turns, cwd, thread_id, created, total_tokens, cli_version)
            target = _rollout_path(thread_id, created)
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
            tmp = target.with_suffix(".jsonl.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(target)
            try:
                _write_import_record(candidate, thread_id, created)
            except (AgentImportError, OSError):
                if target.exists():
                    target.unlink()
                raise
        except (AgentImportError, OSError) as exc:
            result.failed.append((candidate.path, str(exc)))
            continue
        result.imported.append((candidate.path, thread_id))
    return result


# --- Session import: Codex -> Claude Code ---


def claude_cli_version() -> str:
    """Version string Claude Code stamps into every transcript line."""
    try:
        proc = _run([CLAUDE, "--version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentImportError(f"cannot run `{CLAUDE} --version`: {exc}") from exc
    if proc.returncode != 0:
        raise AgentImportError(f"`{CLAUDE} --version` failed: {(proc.stderr or '').strip()}")
    parts = proc.stdout.split()
    if not parts:
        raise AgentImportError(f"`{CLAUDE} --version` printed nothing")
    return parts[0]


def _rollout_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            raise AgentImportError("unexpected Codex content block")
        if block.get("type") in ("input_text", "output_text"):
            text = block.get("text")
            if text is not None and not isinstance(text, str):
                raise AgentImportError("unexpected Codex text block")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def read_codex_rollout(path: Path) -> tuple[list[Turn], str, str]:
    """Extract text turns, cwd and model from a Codex rollout file."""
    turns: list[Turn] = []
    cwd = ""
    model = ""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise AgentImportError(f"malformed Codex rollout: {path}") from exc
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                kind = obj.get("type")
                if kind == "session_meta":
                    cwd = payload.get("cwd") or cwd
                    continue
                if kind == "turn_context":
                    model = payload.get("model") or model
                    continue
                if kind != "response_item" or payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _rollout_text(payload.get("content"))
                if not text:
                    continue
                turns.append(Turn(role=role, text=text, timestamp=obj.get("timestamp") or ""))
    except OSError as exc:
        raise AgentImportError(f"cannot read {path}: {exc}") from exc
    return turns, cwd, model


def codex_rollout_files() -> list[Path]:
    """Every rollout transcript in the active CODEX_HOME, newest first."""
    if not CODEX_SESSIONS_DIR.exists():
        return []
    try:
        files = list(CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))
    except (OSError, PermissionError) as exc:
        raise AgentImportError(f"cannot read {CODEX_SESSIONS_DIR}: {exc}") from exc
    root = CODEX_SESSIONS_DIR.resolve()
    unsafe = [path for path in files if path.is_symlink() or not path.resolve().is_relative_to(root)]
    if unsafe:
        raise AgentImportError(f"refusing rollout outside active Codex home: {unsafe[0]}")
    return _newest_first(files)


def find_codex_rollout(session_id: str) -> Path | None:
    """Locate a rollout by session id in the active CODEX_HOME."""
    validate_identifier(session_id, "session id")
    if not CODEX_SESSIONS_DIR.exists():
        return None
    for path in codex_rollout_files():
        if path.name.endswith(f"-{session_id}.jsonl"):
            return path
    return None


def _imported_from_codex_digests() -> set[str]:
    """Digests asm already wrote into Claude, recorded in the asm data dir."""
    path = claude_import_records()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentImportError(f"cannot read {path}: {exc}") from exc
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise AgentImportError(f"unexpected shape in {path}")
    return {
        rec.get("content_sha256")
        for rec in records
        if isinstance(rec, dict) and rec.get("content_sha256")
    }


def plan_sessions_to_claude(
    paths: list[Path] | None = None, limit: int | None = None
) -> SessionPlan:
    """Decide which Codex rollouts still need importing into Claude Code."""
    known = _imported_from_codex_digests()

    plan = SessionPlan()
    limit = SESSION_PLAN_LIMIT if limit is None else limit
    considered = paths if paths is not None else codex_rollout_files()
    if paths is None and len(considered) > limit:
        plan.truncated = len(considered) - limit
        considered = considered[:limit]
    for path in considered:
        path = Path(path)
        try:
            turns, cwd, _model = read_codex_rollout(path)
            digest = _digest(path)
            stat = path.stat()
        except (AgentImportError, OSError) as exc:
            plan.skipped.append((str(path), str(exc)))
            continue
        if not turns:
            plan.skipped.append((str(path), "no text turns"))
            continue
        if not cwd:
            plan.skipped.append((str(path), "rollout has no cwd"))
            continue
        candidate = SessionCandidate(
            path=str(path),
            title=_title_of(turns),
            turns=len(turns),
            modified_at=stat.st_mtime_ns,
            digest=digest,
            cwd=cwd,
        )
        if digest in known:
            plan.already_imported.append(candidate)
        else:
            plan.new.append(candidate)
    return plan


def _claude_lines(
    turns: list[Turn], cwd: str, session_id: str, model: str, version: str
) -> list[dict]:
    lines: list[dict] = []
    parent: str | None = None
    for index, turn in enumerate(turns):
        node = str(uuid.uuid4())
        common = {
            "type": turn.role,
            "uuid": node,
            "parentUuid": parent,
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": turn.timestamp,
            "version": version,
            "userType": "external",
        }
        if turn.role == "user":
            common["message"] = {"role": "user", "content": turn.text}
        else:
            message = {
                "role": "assistant",
                "type": "message",
                    "id": f"msg_import_{session_id}_{index}",
                "content": [{"type": "text", "text": turn.text}],
                "stop_reason": None,
                "stop_sequence": None,
                # Zeroed so an imported copy is never priced a second time.
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
            if model:
                message["model"] = model
            common["message"] = message
        lines.append(common)
        parent = node
    return lines


def _write_claude_import_record(candidate: SessionCandidate, session_id: str, when: datetime) -> None:
    path = claude_import_records()
    records = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentImportError(f"cannot read {path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise AgentImportError(f"unexpected shape in {path}")
        records = data["records"]
    records.append(
        {
            "source_path": candidate.path,
            "content_sha256": candidate.digest,
            "imported_session_id": session_id,
            "imported_at": int(when.timestamp()),
            "source_modified_at": candidate.modified_at,
            "title": candidate.title,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps({"records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        if tmp.exists():
            tmp.unlink()
        raise


def apply_sessions_to_claude(plan: SessionPlan, dry_run: bool = False) -> SessionResult:
    """Write each planned Codex rollout into Claude Code as a resumable transcript."""
    result = SessionResult()
    if dry_run:
        result.imported = [(c.path, "") for c in plan.new]
        return result
    if not plan.new:
        return result

    from asm.services import backup

    version = claude_cli_version()
    if _has_entries(PROJECTS_DIR):
        result.backup_path = backup.create_sessions_backup()
        if not result.backup_path:
            raise AgentImportError("Claude session safety backup failed")

    for candidate in plan.new:
        when = datetime.now().astimezone()
        session_id = str(uuid.uuid4())
        try:
            turns, cwd, model = read_codex_rollout(Path(candidate.path))
            if not cwd or not os.path.isabs(cwd) or not turns:
                raise AgentImportError("rollout has no working directory or no conversation turns")
            lines = _claude_lines(turns, cwd, session_id, model, version)
            target = PROJECTS_DIR / encode_path(cwd) / f"{session_id}.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
            tmp = target.with_suffix(".jsonl.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(target)
            try:
                _write_claude_import_record(candidate, session_id, when)
            except (AgentImportError, OSError):
                if target.exists():
                    target.unlink()
                raise
        except (AgentImportError, OSError) as exc:
            result.failed.append((candidate.path, str(exc)))
            continue
        result.imported.append((candidate.path, session_id))
    return result
