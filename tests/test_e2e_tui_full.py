"""E2E: every remaining user-facing TUI flow, exercised at least once.

Together with test_e2e_tui.py this drives each tab's actions the way a user
does — buttons, confirm dialogs, input dialogs — against the fake data tree.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from asm.app import CCTuiApp
from tests.test_codex_data import _write_rollout_real_layout
from tests.test_feature_smoke import _setup_fake_claude


def _run_app(coro_body, **app_kwargs):
    async def run():
        app = CCTuiApp(**app_kwargs)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.6)
            await pilot.pause()
            await coro_body(app, pilot)
        return app

    return asyncio.run(run())


async def _settle(pilot, seconds=0.5):
    await pilot.pause()
    await asyncio.sleep(seconds)
    await pilot.pause()


async def _confirm_yes(app, pilot):
    await pilot.pause()
    assert len(app.screen_stack) == 2, "confirm dialog expected"
    await pilot.press("y")
    await _settle(pilot)


# ── Projects tab ─────────────────────────────────────────────────────────


def test_projects_trash_orphaned_sessions_flow(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    orphan_dir = Path(env["projects_dir"]) / "-home-me-orphan"
    orphan_dir.mkdir()
    (orphan_dir / "dead.jsonl").write_text("{}\n")

    async def body(app, pilot):
        app.action_tab("tab-projects")  # panes load on first view
        await _settle(pilot)
        pane = app.query_one("ProjectsPane")
        assert "-home-me-orphan" in pane._orphaned_session_dirs
        pane.action_trash_orphaned()
        await _confirm_yes(app, pilot)
        assert not orphan_dir.exists()

    _run_app(body)


def test_projects_remove_config_flow(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    ghost = str(Path(env["home"]) / "work" / "ghost-project")
    cfg_path = Path(env["claude_json"])
    cfg = json.loads(cfg_path.read_text())
    cfg["projects"][ghost] = {}
    cfg_path.write_text(json.dumps(cfg))

    async def body(app, pilot):
        app.action_tab("tab-projects")  # panes load on first view
        await _settle(pilot)
        pane = app.query_one("ProjectsPane")
        pane._selected_project_path = ghost
        pane._handle_action("remove-config")
        await _confirm_yes(app, pilot)
        assert ghost not in json.loads(cfg_path.read_text())["projects"]

    _run_app(body)


def test_projects_clean_empty_sessions_flow(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    stub = Path(env["projects_dir"]) / env["encoded_a"] / "deadbeef-0000-0000-0000-000000000000.jsonl"
    stub.write_text(json.dumps({"type": "ai-title", "aiTitle": "stub only"}) + "\n")

    async def body(app, pilot):
        app.action_tab("tab-projects")  # panes load on first view
        await _settle(pilot)
        pane = app.query_one("ProjectsPane")
        assert any(e["session_id"].startswith("deadbeef") for e in pane._empty_sessions)
        pane._handle_action("trash-empty-sessions")
        await _confirm_yes(app, pilot)
        assert not stub.exists()

    _run_app(body)


def test_projects_move_codex_session_flow(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    from asm.services import codex_data
    codex_root = tmp_path / "codex-sessions"
    rollout = codex_root / "2026" / "07" / "01" / "rollout-2026-07-01T09-00-00-mv01.jsonl"
    _write_rollout_real_layout(rollout, "mv01", "/old/cwd", ["gpt-5.5"], None)
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", codex_root)
    codex_data.refresh()

    async def body(app, pilot):
        from textual.widgets import Input
        pane = app.query_one("ProjectsPane")
        pane._selected_session = ("mv01", str(rollout), "codex")
        pane._handle_action("move-codex-session")
        await pilot.pause()
        assert len(app.screen_stack) == 2, "input dialog expected"
        app.screen.query_one("#input-field", Input).value = "/new/cwd"
        await pilot.press("enter")
        await _settle(pilot)
        assert '"cwd": "/new/cwd"' in rollout.read_text().replace('":"', '": "')

    _run_app(body)


def test_projects_preview_renders_messages(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)

    async def body(app, pilot):
        from textual.widgets import Static
        pane = app.query_one("ProjectsPane")
        pane._show_messages(
            env["session_id"],
            [{"type": "user", "content": "alpha prompt"},
             {"type": "assistant", "content": "alpha answer"}],
            "claude", env["project_a"], env["encoded_a"],
        )
        body_text = str(app.query_one("#project-detail-body", Static).render())
        assert "alpha prompt" in body_text and "alpha answer" in body_text
        assert f"asm resume {env['session_id']}" in body_text

    _run_app(body)


def test_projects_instruction_editor_opens(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    proj_dir = Path(env["project_a"])
    proj_dir.mkdir(parents=True, exist_ok=True)

    async def body(app, pilot):
        from asm.screens.file_editor import FileEditorScreen
        pane = app.query_one("ProjectsPane")
        pane._selected_project_path = env["project_a"]
        pane._handle_action("edit::CLAUDE.md")
        await pilot.pause()
        assert isinstance(app.screen, FileEditorScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1

    _run_app(body)


# ── Migrate tab: full happy path through the UI ─────────────────────────


def test_migrate_full_ui_happy_path(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    target = str(Path(env["home"]) / "work" / "project-delta")
    from asm.models import encode_path
    dest = Path(env["projects_dir"]) / encode_path(target) / f"{env['session_id']}.jsonl"

    async def body(app, pilot):
        app.action_tab("tab-migrate")
        await _settle(pilot)
        pane = app.query_one("MigratePane")
        pane._source_hint = env["project_a"]
        pane._source_encoded = env["encoded_a"]
        pane._target_hint = target
        pane._target_encoded = encode_path(target)
        pane._populate_session_table(env["project_a"], [
            (env["session_id"], "07/24 10:00", "alpha prompt", "1KB"),
        ])
        pane.action_toggle_all()
        assert pane._selected_sessions == {env["session_id"]}
        pane._start_migrate()
        await _confirm_yes(app, pilot)
        assert dest.exists(), "selected session should be copied to the target"

    _run_app(body)


# ── Backups tab ──────────────────────────────────────────────────────────


def test_backups_full_backup_confirm_flow(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)

    async def body(app, pilot):
        app.action_tab("tab-backups")
        await _settle(pilot, 0.3)
        pane = app.query_one("BackupsPane")
        pane._handle_action("full-backup")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        from textual.widgets import Static
        dialog_text = str(app.screen.query_one("#confirm-question", Static).render())
        assert "will be copied" in dialog_text  # size preview shown
        await pilot.press("y")
        await _settle(pilot, 0.8)
        assert any(Path(env["backups_dir"]).glob("full-*")), "full backup dir created"

    _run_app(body)


def test_backups_delete_and_export_and_import_flows(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    from asm.services.backup import create_config_backup
    created = create_config_backup()
    assert created

    async def body(app, pilot):
        from textual.widgets import DataTable, Input
        app.action_tab("tab-backups")
        await _settle(pilot)
        pane = app.query_one("BackupsPane")
        table = pane.query_one("#backups-table", DataTable)
        assert table.row_count >= 1
        table.move_cursor(row=0)

        # Export the selected backup (lands in tmp home — no Desktop there).
        pane._handle_action("export")
        await _settle(pilot, 0.8)
        archives = list(Path(env["home"]).glob("*.tar.gz"))
        assert archives, "export should write a .tar.gz"

        # Import it back through the input dialog.
        pane._handle_action("import")
        await pilot.pause()
        app.screen.query_one("#input-field", Input).value = str(archives[0])
        await pilot.press("enter")
        await _settle(pilot, 0.8)

        # Delete the originally created backup through the confirm dialog.
        table.move_cursor(row=0)
        pane._handle_action("delete")
        await _confirm_yes(app, pilot)
        assert not Path(created).exists()

    _run_app(body)


def test_recovery_restore_flow(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    from asm.services import cleaner, recovery
    from tests.test_feature_smoke import _fake_send2trash
    monkeypatch.setattr(recovery, "CLAUDE_DIR", Path(env["claude_dir"]))
    from asm.services import codex_data as _codex_data

    monkeypatch.setattr(_codex_data, "CODEX_SESSIONS_DIR", tmp_path / "no-codex" / "sessions")
    monkeypatch.setattr(recovery, "RECOVERY_BASE_DIR", tmp_path / "recovery")
    monkeypatch.setattr(recovery, "send2trash", _fake_send2trash)

    jsonl = Path(env["projects_dir"]) / env["encoded_a"] / f"{env['session_id']}.jsonl"
    assert cleaner.trash_single_session_file(env["encoded_a"], env["session_id"])
    assert not jsonl.exists()
    items = recovery.list_recovery_items()
    assert len(items) == 1

    async def body(app, pilot):
        from textual.widgets import DataTable
        app.action_tab("tab-backups")
        await _settle(pilot)
        pane = app.query_one("BackupsPane")
        rec_table = pane.query_one("#recovery-table", DataTable)
        assert rec_table.row_count == 1
        rec_table.move_cursor(row=0)
        pane._handle_action("restore-recovery")
        await _confirm_yes(app, pilot)
        assert jsonl.exists(), "recovery restore should bring the session back"

    _run_app(body)


# ── File History / Debug-Todos remaining flows ──────────────────────────


def test_file_history_trash_selected_bulk(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    orphan = Path(env["file_history_dir"]) / "orphaned-session"

    async def body(app, pilot):
        app.action_tab("tab-file-history")
        await _settle(pilot)
        pane = app.query_one("FileHistoryPane")
        pane._selected.add("orphaned-session")
        pane._handle_action("trash-selected")
        await _confirm_yes(app, pilot)
        assert not orphan.exists()

    _run_app(body)


def test_debug_todos_prune_todo_and_orphaned_debug(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    empty_todo = Path(env["todos_dir"]) / "orphaned-agent-test.json"  # "{}"
    orphan_debug = Path(env["debug_dir"]) / "orphaned.log"

    async def body(app, pilot):
        app.action_tab("tab-debug-todos")
        await _settle(pilot)
        pane = app.query_one("DebugTodosPane")
        pane._handle_action("prune-todo")
        await pilot.pause()
        if len(app.screen_stack) == 2:
            await pilot.press("y")
        await _settle(pilot)
        assert not empty_todo.exists()

        pane._handle_action("trash-orphaned-debug")
        await pilot.pause()
        if len(app.screen_stack) == 2:
            await pilot.press("y")
        await _settle(pilot)
        assert not orphan_debug.exists()

    _run_app(body)


# ── App-level: --path filter, source chip, Korean locale ────────────────


def test_app_target_path_filters_projects(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)

    async def body(app, pilot):
        app.action_tab("tab-projects")  # panes load on first view
        await _settle(pilot)
        pane = app.query_one("ProjectsPane")
        assert {p.path for p in pane._all_projects} == {env["project_a"]}

    _run_app(body, target_path=env["project_a"])


def test_dashboard_source_chip_click(monkeypatch, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)

    async def body(app, pilot):
        app.action_dash_set_source("claude")
        dash = app.query_one("DashboardPane")
        assert dash._source == "claude"

    _run_app(body)


def test_app_runs_in_korean(monkeypatch, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)
    from asm import i18n
    i18n.init_lang("ko")
    try:
        async def body(app, pilot):
            from textual.widgets import TabbedContent
            tabs = app.query_one("#main-tabs", TabbedContent)
            labels = [str(tab.label) for tab in tabs.query("Tab")]
            assert any("대시보드" in label for label in labels)

        _run_app(body)
    finally:
        i18n.init_lang("en")


def test_hidden_tabs_load_only_when_first_shown(monkeypatch, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)

    async def body(app, pilot):
        pane = app.query_one("ProjectsPane")
        assert pane._all_projects == []  # not loaded while hidden
        app.action_tab("tab-projects")
        await _settle(pilot)
        assert pane._all_projects

    _run_app(body)
