"""TUI flows for the Agent Import tab — real app, real key actions."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from pathlib import Path

from textual.app import App, ComposeResult

from asm.app import CCTuiApp
from asm.screens.agent_import import AgentImportPane
from asm.services import agent_import, codex_data, ledger
from tests.test_feature_smoke import _setup_fake_claude


@pytest.fixture(autouse=True)
def _clear_cross_test_caches():
    """claude_data/codex_data memoize scans in module globals; leaving them
    populated makes later tests judge sessions against this test's tmp tree."""
    yield
    from asm.services import claude_data, codex_data

    claude_data.refresh_usage_cache()
    codex_data.refresh()


class _PaneApp(App):
    """Just the pane — the full app spawns dashboard scanners these tests don't need."""

    def compose(self) -> ComposeResult:
        yield AgentImportPane()


def _run_pane(coro_body):
    async def run():
        app = _PaneApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            pane = app.query_one(AgentImportPane)
            await coro_body(app, pilot, pane)

    asyncio.run(run())


def _run_app(coro_body):
    async def run():
        app = CCTuiApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.6)
            await pilot.pause()
            await coro_body(app, pilot)
        return app

    return asyncio.run(run())


def _isolate(monkeypatch, tmp_path: Path) -> Path:
    # The dashboard indexes into the usage ledger on a background thread; that
    # outlives the test app and contends on sqlite, so keep it out of these
    # tab-level tests.
    monkeypatch.setattr(ledger, "update_claude", lambda progress=None: 0)
    monkeypatch.setattr(ledger, "update_codex", lambda progress=None: 0)

    # The app itself scans Claude/Codex on startup; without this the dashboard
    # would index the real ~/.claude and ~/.codex.
    fake = tmp_path / "fake"
    fake.mkdir()
    _setup_fake_claude(monkeypatch, fake)
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", tmp_path / "codex-sessions")
    codex_data.refresh()

    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        '[mcp_servers.only-in-codex]\nurl = "https://example.com/mcp"\n', encoding="utf-8"
    )
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    monkeypatch.setattr(agent_import, "CODEX_CONFIG", codex_config)
    monkeypatch.setattr(agent_import, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(agent_import, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(agent_import, "CODEX_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(agent_import, "CODEX_IMPORT_RECORDS", tmp_path / "codex-imports.json")
    return tmp_path


def test_tab_opens_and_lists_importable_mcp(monkeypatch, tmp_path: Path):
    _isolate(monkeypatch, tmp_path)

    async def body(app, pilot, pane):
        pane.action_pick(1)  # MCP codex -> claude
        await asyncio.sleep(0.5)
        await pilot.pause()

        assert [row[0] for row in pane._rows] == ["only-in-codex"]
        # Nothing preselected — an import must be an explicit choice.
        assert pane._selected == set()

    _run_pane(body)


def test_toggle_and_toggle_all_change_selection(monkeypatch, tmp_path: Path):
    _isolate(monkeypatch, tmp_path)

    async def body(app, pilot, pane):
        pane.action_pick(1)
        await asyncio.sleep(0.5)
        await pilot.pause()

        pane.action_toggle_all()
        assert pane._selected == {"only-in-codex"}
        pane.action_toggle_all()
        assert pane._selected == set()

    _run_pane(body)


def test_import_with_nothing_selected_does_not_write(monkeypatch, tmp_path: Path):
    _isolate(monkeypatch, tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(agent_import, "apply_mcp", lambda *a, **k: calls.append("applied"))

    async def body(app, pilot, pane):
        pane.action_pick(1)
        await asyncio.sleep(0.5)
        await pilot.pause()

        pane.action_run_import()  # nothing was ever selected
        await pilot.pause()

    _run_pane(body)
    assert calls == []


def test_session_modes_render_without_error(monkeypatch, tmp_path: Path):
    _isolate(monkeypatch, tmp_path)

    async def body(app, pilot, pane):
        for index in (2, 3):
            pane.action_pick(index)
            await asyncio.sleep(0.5)
            await pilot.pause()
            assert pane._error is None
            assert pane._rows == []

    _run_pane(body)


def test_import_runs_end_to_end_through_the_tui(monkeypatch, tmp_path: Path):
    """Confirm dialog → worker → real `codex mcp add` into a throwaway CODEX_HOME."""
    _isolate(monkeypatch, tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "from-claude": {
                        "type": "stdio",
                        "command": "uv",
                        "args": ["run", "thing"],
                        "env": {"KEY": "val"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_import, "CODEX_CONFIG", codex_home / "config.toml")
    monkeypatch.setattr(agent_import, "SUBPROCESS_ENV", {"CODEX_HOME": str(codex_home)})
    from asm.services import backup

    monkeypatch.setattr(backup, "create_codex_backup", lambda: None)

    async def body(app, pilot, pane):
        pane.action_pick(0)  # MCP claude -> codex
        await asyncio.sleep(0.5)
        await pilot.pause()

        assert [row[0] for row in pane._rows] == ["from-claude"]
        pane.action_toggle_all()
        pane.action_run_import()
        await pilot.pause()
        await pilot.press("y")  # confirm
        await asyncio.sleep(2.0)
        await pilot.pause()

    if shutil.which("codex") is None:
        pytest.skip("codex CLI not installed")
    _run_pane(body)

    import tomllib

    with (codex_home / "config.toml").open("rb") as fh:
        written = tomllib.load(fh)["mcp_servers"]["from-claude"]
    assert written["command"] == "uv"
    assert written["env"] == {"KEY": "val"}


def test_truncation_is_reported_not_silent(monkeypatch, tmp_path: Path):
    _isolate(monkeypatch, tmp_path)
    sessions = tmp_path / "codex-plan-sessions" / "2026" / "08" / "05"
    sessions.mkdir(parents=True)
    rows = [
        {
            "timestamp": "2026-08-05T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"session_id": "s", "cwd": "/work/x", "cli_version": "0.145.0"},
        },
        {
            "timestamp": "2026-08-05T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
    ]
    body_text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    for i in range(5):
        (sessions / f"rollout-2026-08-05T09-00-0{i}-id{i}.jsonl").write_text(
            body_text, encoding="utf-8"
        )
    monkeypatch.setattr(agent_import, "CODEX_SESSIONS_DIR", tmp_path / "codex-plan-sessions")
    monkeypatch.setattr(agent_import, "SESSION_PLAN_LIMIT", 2)

    async def body(app, pilot, pane):
        pane.action_pick(3)  # sessions codex -> claude
        await asyncio.sleep(0.6)
        await pilot.pause()

        assert len(pane._rows) == 2
        assert pane._truncated == 3
        info = str(pane.query_one("#import-info").render())
        assert "3" in info  # the dropped count is shown, not silently hidden

    _run_pane(body)


def test_tab_is_wired_into_the_app(monkeypatch, tmp_path: Path):
    """The pane-level tests drive the pane directly; this one proves F8 reaches it."""
    _isolate(monkeypatch, tmp_path)

    async def body(app, pilot):
        app.action_tab("tab-agent-import")
        await pilot.pause()
        tabs = app.query_one("#main-tabs")
        assert tabs.active == "tab-agent-import"
        assert app.query_one("AgentImportPane") is not None

    _run_app(body)


def test_stale_plan_from_previous_direction_is_ignored(monkeypatch, tmp_path: Path):
    _isolate(monkeypatch, tmp_path)

    async def body(app, pilot, pane):
        pane.action_pick(1)
        await asyncio.sleep(0.5)
        await pilot.pause()
        rows = list(pane._rows)
        pane._on_loaded(3, [("stale", "from another direction", True, object())], None, 0)
        assert pane._rows == rows

    _run_pane(body)
