"""Tests for cross-agent MCP import (never touches the real ~/.claude or ~/.codex)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from asm.services import agent_import


@pytest.fixture(autouse=True)
def _clear_cross_test_caches():
    """claude_data/codex_data memoize scans in module globals; leaving them
    populated makes later tests judge sessions against this test's tmp tree."""
    yield
    from asm.services import claude_data, codex_data

    claude_data.refresh_usage_cache()
    codex_data.refresh()

CODEX_TOML = """
[mcp_servers.plain-http]
url = "https://example.com/mcp"

[mcp_servers.with-headers]
url = "https://api.example.com/mcp"

[mcp_servers.with-headers.http_headers]
Authorization = "Bearer secret-token"

[mcp_servers.local-stdio]
command = "npx"
args = ["-y", "some-mcp@latest"]

[mcp_servers.local-stdio.env]
FOO = "bar"
"""

CLAUDE_JSON_DATA = {
    "mcpServers": {
        "plain-http": {"type": "http", "url": "https://example.com/mcp"},
        "claude-only": {
            "type": "stdio",
            "command": "uv",
            "args": ["run", "thing"],
            "env": {"KEY": "val"},
        },
        "sse-server": {"type": "sse", "url": "https://example.com/sse"},
    }
}


@pytest.fixture
def both_configs(tmp_path, monkeypatch):
    codex_config = tmp_path / "codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(CODEX_TOML, encoding="utf-8")

    claude_json = tmp_path / "claude" / ".claude.json"
    claude_json.parent.mkdir(parents=True)
    claude_json.write_text(json.dumps(CLAUDE_JSON_DATA), encoding="utf-8")

    monkeypatch.setattr(agent_import, "CODEX_CONFIG", codex_config)
    monkeypatch.setattr(agent_import, "CLAUDE_JSON", claude_json)
    return codex_config, claude_json


def test_read_codex_mcp_parses_transports_and_secrets(both_configs):
    servers = agent_import.read_codex_mcp()

    assert servers["plain-http"].transport == "http"
    assert servers["local-stdio"].transport == "stdio"
    assert servers["local-stdio"].args == ["-y", "some-mcp@latest"]
    assert servers["local-stdio"].env == {"FOO": "bar"}
    assert servers["with-headers"].headers == {"Authorization": "Bearer secret-token"}


def test_read_claude_mcp_parses_entries(both_configs):
    servers = agent_import.read_claude_mcp()

    assert servers["plain-http"].transport == "http"
    assert servers["claude-only"].command == "uv"
    assert servers["claude-only"].env == {"KEY": "val"}
    assert servers["sse-server"].transport == "sse"


def test_read_missing_files_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_import, "CODEX_CONFIG", tmp_path / "nope.toml")
    monkeypatch.setattr(agent_import, "CLAUDE_JSON", tmp_path / "nope.json")

    assert agent_import.read_codex_mcp() == {}
    assert agent_import.read_claude_mcp() == {}


def test_read_codex_mcp_raises_on_malformed_toml(tmp_path, monkeypatch):
    bad = tmp_path / "config.toml"
    bad.write_text("[mcp_servers.broken\n", encoding="utf-8")
    monkeypatch.setattr(agent_import, "CODEX_CONFIG", bad)

    with pytest.raises(agent_import.AgentImportError):
        agent_import.read_codex_mcp()


def test_plan_codex_to_claude_skips_duplicates(both_configs):
    plan = agent_import.plan_mcp("codex-to-claude")

    assert plan.already_present == ["plain-http"]
    assert {s.name for s in plan.new} == {"with-headers", "local-stdio"}
    assert plan.unsupported == []


def test_plan_claude_to_codex_reports_unsupported_sse(both_configs):
    plan = agent_import.plan_mcp("claude-to-codex")

    assert {s.name for s in plan.new} == {"claude-only"}
    assert [name for name, _ in plan.unsupported] == ["sse-server"]


def test_plan_rejects_unknown_direction(both_configs):
    with pytest.raises(ValueError):
        agent_import.plan_mcp("sideways")


def test_dry_run_writes_nothing(both_configs, monkeypatch):
    codex_config, _ = both_configs
    before = codex_config.read_text(encoding="utf-8")

    def explode(argv):
        raise AssertionError(f"dry run must not shell out: {argv}")

    monkeypatch.setattr(agent_import, "_run", explode)

    plan = agent_import.plan_mcp("claude-to-codex")
    result = agent_import.apply_mcp(plan, dry_run=True)

    assert result.imported == ["claude-only"]
    assert result.backup_path is None
    assert codex_config.read_text(encoding="utf-8") == before


def test_argv_carries_secrets_verbatim(both_configs):
    servers = agent_import.read_codex_mcp()

    claude_argv = agent_import._claude_add_argv(servers["with-headers"])
    assert "Authorization: Bearer secret-token" in claude_argv

    codex_argv = agent_import._codex_add_argv(servers["local-stdio"])
    assert "FOO=bar" in codex_argv
    assert codex_argv[codex_argv.index("--") + 1 :] == ["npx", "-y", "some-mcp@latest"]


def test_apply_records_cli_failure(both_configs, monkeypatch):
    monkeypatch.setattr(agent_import, "SUBPROCESS_ENV", {})
    monkeypatch.setattr(
        agent_import,
        "_run",
        lambda argv: subprocess.CompletedProcess(argv, 1, "", "boom"),
    )
    monkeypatch.setattr(agent_import, "apply_mcp", agent_import.apply_mcp)
    from asm.services import backup

    monkeypatch.setattr(backup, "create_codex_backup", lambda: "/tmp/fake-backup")

    plan = agent_import.plan_mcp("claude-to-codex")
    result = agent_import.apply_mcp(plan)

    assert result.imported == []
    assert result.failed == [("claude-only", "boom")]


# --- Live CLI round-trip: uses throwaway CODEX_HOME / CLAUDE_CONFIG_DIR ---

codex_cli = pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
claude_cli = pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")


@codex_cli
def test_live_import_into_codex_home(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps(CLAUDE_JSON_DATA), encoding="utf-8")

    monkeypatch.setattr(agent_import, "CODEX_CONFIG", codex_home / "config.toml")
    monkeypatch.setattr(agent_import, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(agent_import, "SUBPROCESS_ENV", {"CODEX_HOME": str(codex_home)})
    from asm.services import backup

    monkeypatch.setattr(backup, "create_codex_backup", lambda: None)

    plan = agent_import.plan_mcp("claude-to-codex")
    result = agent_import.apply_mcp(plan)

    assert result.failed == []
    assert sorted(result.imported) == ["claude-only", "plain-http"]

    with (codex_home / "config.toml").open("rb") as fh:
        written = tomllib.load(fh)["mcp_servers"]["claude-only"]
    assert written["command"] == "uv"
    assert written["args"] == ["run", "thing"]
    assert written["env"] == {"KEY": "val"}


@codex_cli
def test_live_http_headers_survive_round_trip(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "hdr": {
                        "type": "http",
                        "url": "https://api.example.com/mcp",
                        "headers": {"Authorization": "Bearer secret-token"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(agent_import, "CODEX_CONFIG", codex_home / "config.toml")
    monkeypatch.setattr(agent_import, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(agent_import, "SUBPROCESS_ENV", {"CODEX_HOME": str(codex_home)})
    from asm.services import backup

    monkeypatch.setattr(backup, "create_codex_backup", lambda: None)

    result = agent_import.apply_mcp(agent_import.plan_mcp("claude-to-codex"))

    assert result.failed == []
    with (codex_home / "config.toml").open("rb") as fh:
        entry = tomllib.load(fh)["mcp_servers"]["hdr"]
    assert entry["http_headers"] == {"Authorization": "Bearer secret-token"}


@claude_cli
def test_live_import_into_claude_config_dir(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(CODEX_TOML, encoding="utf-8")

    monkeypatch.setattr(agent_import, "CODEX_CONFIG", codex_config)
    monkeypatch.setattr(agent_import, "CLAUDE_JSON", claude_home / ".claude.json")
    monkeypatch.setattr(agent_import, "SUBPROCESS_ENV", {"CLAUDE_CONFIG_DIR": str(claude_home)})
    from asm.services import backup

    monkeypatch.setattr(backup, "create_config_backup", lambda: None)

    result = agent_import.apply_mcp(agent_import.plan_mcp("codex-to-claude"))

    assert result.failed == []
    assert sorted(result.imported) == ["local-stdio", "plain-http", "with-headers"]

    written = json.loads((claude_home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
    assert written["with-headers"]["headers"] == {"Authorization": "Bearer secret-token"}
    assert written["local-stdio"]["command"] == "npx"


def test_live_import_leaves_real_user_data_untouched():
    from asm import models

    assert agent_import.CLAUDE_JSON == models.CLAUDE_JSON
    assert agent_import.CODEX_CONFIG == models.CODEX_DIR / "config.toml"
    assert agent_import.SUBPROCESS_ENV == {}


# --- Session import: Claude Code -> Codex ---


def _claude_line(kind, text, uuid_, ts, cwd="/work/proj", usage=None, **extra):
    message = {"role": kind, "content": text}
    if usage is not None:
        message["usage"] = usage
    line = {
        "type": kind,
        "uuid": uuid_,
        "sessionId": "sess-1",
        "cwd": cwd,
        "timestamp": ts,
        "message": message,
    }
    line.update(extra)
    return line


@pytest.fixture
def claude_session(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    proj = projects / "-work-proj"
    proj.mkdir(parents=True)
    session = proj / "sess-1.jsonl"
    rows = [
        _claude_line("user", "첫 질문입니다", "u1", "2026-08-01T00:00:00.000Z"),
        _claude_line(
            "assistant",
            [
                {"type": "thinking", "thinking": "숨겨야 함"},
                {"type": "text", "text": "첫 답변입니다"},
            ],
            "a1",
            "2026-08-01T00:00:01.000Z",
            usage={"input_tokens": 100, "output_tokens": 50},
        ),
        _claude_line("user", "<command-name>/foo</command-name>", "u2", "2026-08-01T00:00:02.000Z"),
        _claude_line("user", "메타 라인", "u3", "2026-08-01T00:00:03.000Z", isMeta=True),
        _claude_line("user", "둘째 질문", "u4", "2026-08-01T00:00:04.000Z"),
        _claude_line(
            "assistant",
            [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            "a2",
            "2026-08-01T00:00:05.000Z",
            usage={"input_tokens": 10, "output_tokens": 5},
        ),
    ]
    session.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    monkeypatch.setattr(agent_import, "PROJECTS_DIR", projects)
    monkeypatch.setattr(agent_import, "CODEX_SESSIONS_DIR", codex_root / "sessions")
    monkeypatch.setattr(agent_import, "CODEX_IMPORT_RECORDS", codex_root / "imports.json")
    return session


def test_read_claude_session_keeps_text_and_drops_noise(claude_session):
    turns, cwd, total_tokens = agent_import.read_claude_session(claude_session)

    assert [t.role for t in turns] == ["user", "assistant", "user"]
    assert [t.text for t in turns] == ["첫 질문입니다", "첫 답변입니다", "둘째 질문"]
    assert cwd == "/work/proj"
    assert total_tokens == 165


def test_plan_sessions_lists_new_session(claude_session):
    plan = agent_import.plan_sessions_to_codex()

    assert [c.path for c in plan.new] == [str(claude_session)]
    assert plan.new[0].title == "첫 질문입니다"
    assert plan.new[0].turns == 3
    assert plan.already_imported == []


def test_plan_skips_session_without_text(claude_session, tmp_path):
    empty = claude_session.parent / "empty.jsonl"
    empty.write_text(json.dumps({"type": "system", "uuid": "s1"}) + "\n", encoding="utf-8")

    plan = agent_import.plan_sessions_to_codex()

    assert [reason for path, reason in plan.skipped if path == str(empty)] == ["no text turns"]


def test_session_dry_run_writes_nothing(claude_session):
    plan = agent_import.plan_sessions_to_codex()
    result = agent_import.apply_sessions_to_codex(plan, dry_run=True)

    assert [path for path, _ in result.imported] == [str(claude_session)]
    assert not agent_import.CODEX_SESSIONS_DIR.exists()
    assert not agent_import.CODEX_IMPORT_RECORDS.exists()


def test_apply_writes_rollout_matching_codex_schema(claude_session, monkeypatch):
    from asm.services import backup

    monkeypatch.setattr(backup, "create_codex_backup", lambda: None)
    monkeypatch.setattr(agent_import, "codex_cli_version", lambda: "9.9.9")

    result = agent_import.apply_sessions_to_codex(agent_import.plan_sessions_to_codex())

    assert result.failed == []
    assert len(result.imported) == 1
    _, thread_id = result.imported[0]

    written = list(agent_import.CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))
    assert len(written) == 1
    assert thread_id in written[0].name

    lines = [json.loads(line) for line in written[0].read_text(encoding="utf-8").splitlines()]
    meta = lines[0]
    assert meta["type"] == "session_meta"
    assert meta["payload"]["session_id"] == thread_id
    assert meta["payload"]["cwd"] == "/work/proj"
    # Codex rejects a rollout whose session_meta has no cli_version.
    assert meta["payload"]["cli_version"] == "9.9.9"

    kinds = [(line["type"], line["payload"].get("type")) for line in lines]
    assert ("event_msg", "user_message") in kinds
    assert ("response_item", None) not in kinds
    assert kinds.count(("event_msg", "task_started")) == 2
    assert kinds[-1] == ("event_msg", "token_count")

    roles = [
        line["payload"]["role"] for line in lines if line["type"] == "response_item"
    ]
    assert roles == ["user", "assistant", "user"]


def test_imported_thread_reports_zero_billable_tokens(claude_session, monkeypatch):
    from asm.services import backup

    monkeypatch.setattr(backup, "create_codex_backup", lambda: None)
    monkeypatch.setattr(agent_import, "codex_cli_version", lambda: "9.9.9")
    agent_import.apply_sessions_to_codex(agent_import.plan_sessions_to_codex())

    written = next(agent_import.CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))
    lines = [json.loads(line) for line in written.read_text(encoding="utf-8").splitlines()]
    usage = lines[-1]["payload"]["info"]["total_token_usage"]

    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
    assert usage["total_tokens"] == 165


def test_reimport_is_skipped_by_content_hash(claude_session, monkeypatch):
    from asm.services import backup

    monkeypatch.setattr(backup, "create_codex_backup", lambda: None)
    monkeypatch.setattr(agent_import, "codex_cli_version", lambda: "9.9.9")
    agent_import.apply_sessions_to_codex(agent_import.plan_sessions_to_codex())

    second = agent_import.plan_sessions_to_codex()

    assert second.new == []
    assert [c.path for c in second.already_imported] == [str(claude_session)]

    result = agent_import.apply_sessions_to_codex(second)
    assert result.imported == []
    assert len(list(agent_import.CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"))) == 1


def test_import_record_matches_official_field_names(claude_session, monkeypatch):
    from asm.services import backup

    monkeypatch.setattr(backup, "create_codex_backup", lambda: None)
    monkeypatch.setattr(agent_import, "codex_cli_version", lambda: "9.9.9")
    agent_import.apply_sessions_to_codex(agent_import.plan_sessions_to_codex())

    records = json.loads(agent_import.CODEX_IMPORT_RECORDS.read_text(encoding="utf-8"))["records"]

    assert len(records) == 1
    assert set(records[0]) == {
        "source_path",
        "content_sha256",
        "imported_thread_id",
        "imported_at",
        "source_modified_at",
        "connector_names",
        "title",
    }


def test_asm_codex_parser_reads_the_written_rollout(claude_session, monkeypatch):
    from asm.services import backup, codex_data

    monkeypatch.setattr(backup, "create_codex_backup", lambda: None)
    monkeypatch.setattr(agent_import, "codex_cli_version", lambda: "0.145.0")
    agent_import.apply_sessions_to_codex(agent_import.plan_sessions_to_codex())
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", agent_import.CODEX_SESSIONS_DIR)
    codex_data.refresh()

    sessions = codex_data.get_project_sessions("/work/proj")

    assert len(sessions) == 1
    assert sessions[0].first_prompt.startswith("첫 질문")


# --- Session import: Codex -> Claude Code ---


def _rollout_rows(cwd="/work/x"):
    def item(role, key, text, ts):
        return {
            "timestamp": ts,
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"type": key, "text": text}]},
        }

    return [
        {
            "timestamp": "2026-08-05T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"session_id": "abc", "cwd": cwd, "cli_version": "0.145.0"},
        },
        {
            "timestamp": "2026-08-05T00:00:00.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.5"},
        },
        item("user", "input_text", "코덱스에서 한 질문", "2026-08-05T00:00:01.000Z"),
        {
            "timestamp": "2026-08-05T00:00:02.000Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "중복이라 무시돼야 함"},
        },
        item("assistant", "output_text", "코덱스의 답변", "2026-08-05T00:00:02.000Z"),
    ]


@pytest.fixture
def codex_rollout(tmp_path, monkeypatch):
    sessions = tmp_path / "codex" / "sessions" / "2026" / "08" / "05"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-08-05T09-00-00-abc.jsonl"
    rollout.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _rollout_rows()),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_import, "CODEX_SESSIONS_DIR", tmp_path / "codex" / "sessions")
    monkeypatch.setattr(agent_import, "PROJECTS_DIR", tmp_path / "claude" / "projects")
    return rollout


def test_read_codex_rollout_keeps_teammate_messages(codex_rollout):
    """Codex has no meta-turn concept — a <teammate-message> is real input."""
    rows = _rollout_rows()
    rows.insert(
        2,
        {
            "timestamp": "2026-08-05T00:00:00.500Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": '<teammate-message id="lead">do X</teammate-message>'}
                ],
            },
        },
    )
    codex_rollout.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    turns, _cwd, _model = agent_import.read_codex_rollout(codex_rollout)

    assert [t.text for t in turns] == [
        '<teammate-message id="lead">do X</teammate-message>',
        "코덱스에서 한 질문",
        "코덱스의 답변",
    ]


def test_read_codex_rollout_uses_response_items_only(codex_rollout):
    turns, cwd, model = agent_import.read_codex_rollout(codex_rollout)

    assert [t.text for t in turns] == ["코덱스에서 한 질문", "코덱스의 답변"]
    assert cwd == "/work/x"
    assert model == "gpt-5.5"


def test_plan_sessions_to_claude_lists_rollout(codex_rollout):
    plan = agent_import.plan_sessions_to_claude()

    assert [c.path for c in plan.new] == [str(codex_rollout)]
    assert plan.new[0].cwd == "/work/x"


def test_plan_to_claude_skips_rollout_without_cwd(codex_rollout):
    orphan = codex_rollout.parent / "rollout-2026-08-05T09-30-00-def.jsonl"
    rows = [r for r in _rollout_rows() if r["type"] != "session_meta"]
    orphan.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    plan = agent_import.plan_sessions_to_claude()

    assert [reason for path, reason in plan.skipped if path == str(orphan)] == [
        "rollout has no cwd"
    ]


def test_claude_dry_run_writes_nothing(codex_rollout):
    plan = agent_import.plan_sessions_to_claude()
    result = agent_import.apply_sessions_to_claude(plan, dry_run=True)

    assert [path for path, _ in result.imported] == [str(codex_rollout)]
    assert not agent_import.PROJECTS_DIR.exists()


def test_apply_to_claude_builds_parent_uuid_chain(codex_rollout, monkeypatch):
    from asm.services import backup

    monkeypatch.setattr(backup, "create_sessions_backup", lambda: None)
    monkeypatch.setattr(agent_import, "claude_cli_version", lambda: "2.1.228")

    result = agent_import.apply_sessions_to_claude(agent_import.plan_sessions_to_claude())

    assert result.failed == []
    _, session_id = result.imported[0]

    written = list(agent_import.PROJECTS_DIR.rglob("*.jsonl"))
    assert len(written) == 1
    assert written[0].name == f"{session_id}.jsonl"
    assert written[0].parent.name == "-work-x"

    lines = [json.loads(line) for line in written[0].read_text(encoding="utf-8").splitlines()]
    assert [line["type"] for line in lines] == ["user", "assistant"]
    assert lines[0]["parentUuid"] is None
    assert lines[1]["parentUuid"] == lines[0]["uuid"]
    assert {line["sessionId"] for line in lines} == {session_id}
    assert {line["version"] for line in lines} == {"2.1.228"}
    assert lines[1]["message"]["model"] == "gpt-5.5"


def test_claude_import_is_not_priced_again(codex_rollout, monkeypatch):
    from asm.services import backup

    monkeypatch.setattr(backup, "create_sessions_backup", lambda: None)
    monkeypatch.setattr(agent_import, "claude_cli_version", lambda: "2.1.228")
    agent_import.apply_sessions_to_claude(agent_import.plan_sessions_to_claude())

    written = next(agent_import.PROJECTS_DIR.rglob("*.jsonl"))
    lines = [json.loads(line) for line in written.read_text(encoding="utf-8").splitlines()]

    assert lines[1]["message"]["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_claude_reimport_skipped_by_hash(codex_rollout, monkeypatch):
    from asm.services import backup

    monkeypatch.setattr(backup, "create_sessions_backup", lambda: None)
    monkeypatch.setattr(agent_import, "claude_cli_version", lambda: "2.1.228")
    agent_import.apply_sessions_to_claude(agent_import.plan_sessions_to_claude())

    second = agent_import.plan_sessions_to_claude()

    assert second.new == []
    assert [c.path for c in second.already_imported] == [str(codex_rollout)]
    assert len(list(agent_import.PROJECTS_DIR.rglob("*.jsonl"))) == 1


def test_asm_claude_parser_reads_the_written_transcript(codex_rollout, monkeypatch):
    from asm.services import backup, claude_data

    monkeypatch.setattr(backup, "create_sessions_backup", lambda: None)
    monkeypatch.setattr(agent_import, "claude_cli_version", lambda: "2.1.228")
    agent_import.apply_sessions_to_claude(agent_import.plan_sessions_to_claude())
    monkeypatch.setattr(claude_data, "PROJECTS_DIR", agent_import.PROJECTS_DIR)

    sessions = claude_data.get_project_sessions("/work/x")

    assert len(sessions) == 1
    assert "코덱스에서 한 질문" in sessions[0].first_prompt


def test_plan_limit_reports_dropped_count(claude_session, monkeypatch):
    for i in range(4):
        extra = claude_session.parent / f"extra{i}.jsonl"
        extra.write_text(claude_session.read_text(encoding="utf-8"), encoding="utf-8")

    plan = agent_import.plan_sessions_to_codex(limit=2)

    assert len(plan.new) + len(plan.already_imported) + len(plan.skipped) == 2
    assert plan.truncated == 3


def test_explicit_paths_are_never_truncated(claude_session):
    paths = [claude_session] * 5

    plan = agent_import.plan_sessions_to_codex(paths, limit=2)

    assert plan.truncated == 0
    assert len(plan.new) + len(plan.already_imported) == 5


def test_session_files_are_listed_newest_first(claude_session, monkeypatch):
    import os
    import time

    older = claude_session.parent / "older.jsonl"
    older.write_text(claude_session.read_text(encoding="utf-8"), encoding="utf-8")
    past = time.time() - 86400
    os.utime(older, (past, past))

    listed = agent_import.claude_session_files()

    assert listed[0] == claude_session
    assert listed[-1] == older


def test_subagent_transcripts_are_not_listed_as_sessions(claude_session):
    """<project>/<session>/subagents/*.jsonl belong to a parent session."""
    sub = claude_session.parent / claude_session.stem / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc.jsonl").write_text(
        claude_session.read_text(encoding="utf-8"), encoding="utf-8"
    )

    listed = agent_import.claude_session_files()

    assert listed == [claude_session]


def test_title_skips_harness_preamble_but_keeps_it_in_the_transcript(codex_rollout):
    rows = _rollout_rows()
    rows.insert(
        2,
        {
            "timestamp": "2026-08-05T00:00:00.500Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "<recommended_plugins> a list"}],
            },
        },
    )
    codex_rollout.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    plan = agent_import.plan_sessions_to_claude()

    assert plan.new[0].title == "코덱스에서 한 질문"
    turns, _cwd, _model = agent_import.read_codex_rollout(codex_rollout)
    assert turns[0].text == "<recommended_plugins> a list"


def test_title_falls_back_when_every_user_turn_is_a_preamble(claude_session, tmp_path):
    only_preamble = claude_session.parent / "preamble-only.jsonl"
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s",
            "cwd": "/work/proj",
            "timestamp": "2026-08-01T00:00:00.000Z",
            "message": {"role": "user", "content": "<wrapper>only this</wrapper>"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s",
            "cwd": "/work/proj",
            "timestamp": "2026-08-01T00:00:01.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "답변"}]},
        },
    ]
    only_preamble.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    plan = agent_import.plan_sessions_to_codex([only_preamble])

    assert plan.new[0].title == "<wrapper>only this</wrapper>"


# --- Single active Codex home ---


def test_rollouts_ignore_sibling_legacy_home(monkeypatch, tmp_path):
    active = tmp_path / ".codex" / "sessions"
    legacy = tmp_path / ".codex-hermes" / "sessions"
    active_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    legacy_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    for directory, sid in ((active, active_id), (legacy, legacy_id)):
        target = directory / "2026" / "08" / "05"
        target.mkdir(parents=True)
        (target / f"rollout-2026-08-05T09-00-00-{sid}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in _rollout_rows()),
            encoding="utf-8",
        )
    monkeypatch.setattr(agent_import, "CODEX_SESSIONS_DIR", active)

    listed = agent_import.codex_rollout_files()

    assert len(listed) == 1
    assert active_id in listed[0].name
    assert agent_import.find_codex_rollout(active_id) == listed[0]
    assert agent_import.find_codex_rollout(legacy_id) is None

def test_title_skips_hash_prefixed_preamble(claude_session):
    """A leading `# AGENTS.md ...` dump is injected context, not a label."""
    rows = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s",
            "cwd": "/work/proj",
            "timestamp": "2026-08-01T00:00:00.000Z",
            "message": {"role": "user", "content": "# AGENTS.md instructions for /home/x"},
        },
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": "s",
            "cwd": "/work/proj",
            "timestamp": "2026-08-01T00:00:01.000Z",
            "message": {"role": "user", "content": "실제 질문"},
        },
    ]
    target = claude_session.parent / "hash-preamble.jsonl"
    target.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    turns, _cwd, _tokens = agent_import.read_claude_session(target)
    assert agent_import._title_of(turns) == "실제 질문"


def test_apply_to_claude_refuses_candidate_without_cwd(codex_rollout, monkeypatch):
    """A wrong-direction candidate (e.g. a Claude transcript) must not land in projects/ root."""
    from asm.services import backup

    monkeypatch.setattr(backup, "create_sessions_backup", lambda: None)
    monkeypatch.setattr(agent_import, "claude_cli_version", lambda: "2.1.228")
    stray = codex_rollout.parent / "not-a-rollout.jsonl"
    stray.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    candidate = agent_import.plan_sessions_to_claude().new[0]
    candidate.path = str(stray)

    result = agent_import.apply_sessions_to_claude(agent_import.SessionPlan(new=[candidate]))

    assert result.imported == [] and len(result.failed) == 1
    assert not list(agent_import.PROJECTS_DIR.glob("*.jsonl"))
