"""End-to-end feature smoke tests for the main app and services."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from asm.app import CCTuiApp
from asm import models
from asm.screens import debug_todos as debug_todos_screen
from asm.screens import file_history as file_history_screen
from asm.screens import migrate as migrate_screen
from asm.screens import projects as projects_screen
from asm.services import backup as backup_service
from asm.services import claude_data, cleaner, migrate

def _fake_send2trash(path: str) -> None:
    target = Path(path)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _setup_fake_claude(monkeypatch, tmp_path: Path) -> dict[str, Path | str]:
    home = tmp_path / "home"
    home.mkdir()

    claude_dir = home / ".claude"
    claude_dir.mkdir()
    projects_dir = claude_dir / "projects"
    projects_dir.mkdir()
    session_env_dir = claude_dir / "session-env"
    session_env_dir.mkdir()
    file_history_dir = claude_dir / "file-history"
    file_history_dir.mkdir()
    debug_dir = claude_dir / "debug"
    debug_dir.mkdir()
    todos_dir = claude_dir / "todos"
    todos_dir.mkdir()
    # New-style task dir (Claude Code >= 2.1). Left absent here so the legacy
    # todos/ fallback path is exercised by this fixture.
    tasks_dir = claude_dir / "tasks"
    plugins_dir = claude_dir / "plugins"
    plugins_dir.mkdir()
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir()

    backups_dir = home / ".cc-tui" / "backups"
    backups_dir.mkdir(parents=True)
    app_data_dir = home / ".asm"
    app_data_dir.mkdir()

    project_a = str(home / "work" / "project-alpha")
    project_b = str(home / "work" / "project-beta")
    encoded_a = claude_data.encode_path(project_a)
    encoded_b = claude_data.encode_path(project_b)

    proj_a_dir = projects_dir / encoded_a
    proj_b_dir = projects_dir / encoded_b
    proj_a_dir.mkdir()
    proj_b_dir.mkdir()

    session_id = "11111111-1111-1111-1111-111111111111"
    other_session_id = "22222222-2222-2222-2222-222222222222"

    _write_jsonl(
        proj_a_dir / f"{session_id}.jsonl",
        [
            {
                "type": "user",
                "message": {"content": "alpha prompt"},
                "timestamp": "2026-03-31T10:00:00Z",
            },
            {
                "type": "assistant",
                "timestamp": "2026-03-31T10:00:01Z",
                "message": {
                    "id": "msg-1",
                    "model": "claude-sonnet-4-20250514",
                    "content": "alpha answer",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                    },
                },
            },
        ],
    )
    _write_jsonl(
        proj_b_dir / f"{other_session_id}.jsonl",
        [
            {
                "type": "user",
                "message": {"content": "beta prompt"},
                "timestamp": "2026-03-30T09:00:00Z",
            }
        ],
    )

    (proj_a_dir / "sessions-index.json").write_text("{}")
    (proj_a_dir / "memory").mkdir()
    (proj_a_dir / "memory" / "note.txt").write_text("memory")

    (file_history_dir / session_id).mkdir()
    (file_history_dir / "orphaned-session").mkdir()
    (debug_dir / f"{session_id}.log").write_text("debug content")
    (debug_dir / "orphaned.log").write_text("[]")
    (todos_dir / f"{session_id}-agent-test.json").write_text('{"todo": true}')
    (todos_dir / "orphaned-agent-test.json").write_text("{}")
    (plugins_dir / "installed.json").write_text("{}")
    (skills_dir / "sample-skill").mkdir()
    (skills_dir / "sample-skill" / "SKILL.md").write_text("# Skill")

    claude_json = home / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "firstStartTime": "2026-03-01T00:00:00Z",
                "numStartups": 3,
                "projects": {
                    project_a: {
                        "lastCost": 1.25,
                        "lastDuration": 2500,
                        "lastSessionId": session_id,
                        "lastModelUsage": {
                            "claude-sonnet-4-20250514": {
                                "inputTokens": 100,
                                "outputTokens": 50,
                                "cacheReadInputTokens": 10,
                                "costUSD": 1.25,
                            }
                        },
                    },
                    project_b: {
                        "lastCost": 0.5,
                        "lastDuration": 1000,
                        "lastSessionId": other_session_id,
                        "allowedTools": ["bash"],
                    },
                },
            }
        )
    )

    history_jsonl = claude_dir / "history.jsonl"
    history_jsonl.write_text(
        json.dumps({"timestamp": 1711843200000}) + "\n" + json.dumps({"timestamp": 1711843200000}) + "\n"
    )

    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    monkeypatch.setattr(models, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(models, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(models, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(models, "SESSION_ENV_DIR", session_env_dir)
    monkeypatch.setattr(models, "FILE_HISTORY_DIR", file_history_dir)
    monkeypatch.setattr(models, "DEBUG_DIR", debug_dir)
    monkeypatch.setattr(models, "TODOS_DIR", todos_dir)
    monkeypatch.setattr(models, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(models, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(models, "BACKUP_BASE_DIR", backups_dir)
    monkeypatch.setattr(models, "APP_DATA_DIR", app_data_dir)
    monkeypatch.setattr(models, "RECOVERY_BASE_DIR", app_data_dir / "recovery")

    monkeypatch.setattr(claude_data, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(claude_data, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(claude_data, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(cleaner, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(cleaner, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(backup_service, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(backup_service, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(backup_service, "PROJECTS_DIR", projects_dir)

    monkeypatch.setattr(claude_data, "SESSION_ENV_DIR", session_env_dir)
    monkeypatch.setattr(claude_data, "FILE_HISTORY_DIR", file_history_dir)
    monkeypatch.setattr(claude_data, "DEBUG_DIR", debug_dir)
    monkeypatch.setattr(claude_data, "TODOS_DIR", todos_dir)
    monkeypatch.setattr(claude_data, "TASKS_DIR", tasks_dir)
    # Keep Claude-only tests isolated from any real ~/.codex on this machine,
    # and drop usage caches primed by earlier tests against other tmp dirs.
    from asm.services import codex_data
    from asm.services import artifacts as artifacts_service, search as search_service
    from asm.services import agent_import as agent_import_service
    from asm.screens import backups as backups_screen
    codex_data.refresh()
    claude_data.refresh_usage_cache()
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", claude_dir / "no-codex")
    isolated_codex = claude_dir / "no-codex-home"
    monkeypatch.setattr(backup_service, "CODEX_DIR", isolated_codex)
    monkeypatch.setattr(backup_service, "CODEX_SESSIONS_DIR", isolated_codex / "sessions")
    monkeypatch.setattr(backups_screen, "CODEX_DIR", isolated_codex)
    monkeypatch.setattr(backups_screen, "CODEX_SESSIONS_DIR", isolated_codex / "sessions")
    monkeypatch.setattr(agent_import_service, "CODEX_CONFIG", isolated_codex / "config.toml")
    monkeypatch.setattr(agent_import_service, "CODEX_SESSIONS_DIR", isolated_codex / "sessions")
    monkeypatch.setattr(agent_import_service, "CODEX_IMPORT_RECORDS", isolated_codex / "imports.json")
    monkeypatch.setattr(agent_import_service, "CLAUDE_JSON", claude_json)
    monkeypatch.setattr(agent_import_service, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(artifacts_service, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(search_service, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(search_service, "CODEX_SESSIONS_DIR", claude_dir / "no-codex")
    monkeypatch.setattr(cleaner, "SESSION_ENV_DIR", session_env_dir)
    monkeypatch.setattr(cleaner, "FILE_HISTORY_DIR", file_history_dir)
    monkeypatch.setattr(cleaner, "DEBUG_DIR", debug_dir)
    monkeypatch.setattr(cleaner, "TODOS_DIR", todos_dir)
    monkeypatch.setattr(cleaner, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(cleaner, "_ALLOWED_ROOTS", (claude_dir,))
    monkeypatch.setattr(cleaner, "_TRASH_LOG", home / ".asm" / "trash-log.jsonl")
    monkeypatch.setattr(backup_service, "PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(backup_service, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(backup_service, "BACKUP_BASE_DIR", backups_dir)
    monkeypatch.setattr(
        backup_service,
        "SETTINGS_FILES",
        [claude_dir / "settings.json", claude_dir / "settings.local.json", claude_dir / "keybindings.json"],
    )

    monkeypatch.setattr(migrate, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(projects_screen, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(debug_todos_screen, "DEBUG_DIR", debug_dir)
    monkeypatch.setattr(migrate_screen, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(migrate, "send2trash", _fake_send2trash)
    monkeypatch.setattr(cleaner, "send2trash", _fake_send2trash)
    monkeypatch.setattr(backup_service, "send2trash", _fake_send2trash)

    return {
        "home": home,
        "claude_dir": claude_dir,
        "claude_json": claude_json,
        "projects_dir": projects_dir,
        "file_history_dir": file_history_dir,
        "debug_dir": debug_dir,
        "todos_dir": todos_dir,
        "backups_dir": backups_dir,
        "project_a": project_a,
        "project_b": project_b,
        "encoded_a": encoded_a,
        "encoded_b": encoded_b,
        "session_id": session_id,
        "other_session_id": other_session_id,
    }


def test_service_feature_smoke(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)

    stats = claude_data.get_stats()
    assert stats.total_projects == 2
    assert stats.total_sessions == 2
    assert stats.total_file_history == 2
    assert stats.total_debug == 2
    assert stats.total_todos == 2

    usage = claude_data.get_usage_data()
    # Cost is now derived from the actual session scan (project_a's assistant
    # message), not the .claude.json lastCost sum.
    assert usage["total_cost"] > 0
    assert usage["project_costs"][0]["path"] == env["project_a"]

    periods = claude_data.get_period_usage("daily")
    assert periods
    assert periods[0]["total_messages"] >= 1

    sessions = claude_data.get_project_sessions(env["project_a"])
    assert len(sessions) == 1
    assert sessions[0].session_id == env["session_id"]

    messages = claude_data.get_session_messages(env["session_id"], project_dir=env["encoded_a"], limit=10)
    assert messages[0]["content"] == "alpha prompt"

    file_history = claude_data.get_file_history()
    assert any(entry.is_orphaned for entry in file_history)
    debug_entries = claude_data.get_debug_files()
    assert any(entry.is_orphaned for entry in debug_entries)
    todo_entries = claude_data.get_todos()
    assert any(entry.is_orphaned for entry in todo_entries)

    ok, fail = cleaner.prune_empty_debug_files()
    assert (ok, fail) == (1, 0)
    ok, fail = cleaner.prune_empty_todo_files()
    assert (ok, fail) == (1, 0)

    assert cleaner.trash_file_history("orphaned-session") is True
    assert cleaner.trash_single_session_file(env["encoded_b"], env["other_session_id"]) is True

    backup_service.CLAUDE_DIR.joinpath("settings.json").write_text("{}")
    config_backup = backup_service.create_config_backup()
    settings_backup = backup_service.create_settings_backup()
    plugins_backup = backup_service.create_plugins_backup()
    sessions_backup = backup_service.create_sessions_backup()
    full_backup = backup_service.create_full_backup()
    assert all([config_backup, settings_backup, plugins_backup, sessions_backup, full_backup])

    listed = backup_service.list_backups()
    assert len(listed) >= 5

    export_path = backup_service.export_backup(config_backup, dest_dir=str(tmp_path / "exports"))
    assert export_path is not None
    imported_path = backup_service.import_backup(export_path)
    assert imported_path is not None
    assert backup_service.delete_backup(imported_path) is True

    result = migrate.migrate_sessions(
        source_path=env["project_a"],
        target_path=str(Path(env["home"]) / "work" / "project-gamma"),
        source_encoded=env["encoded_a"],
        target_encoded="project-gamma",
        session_ids=[env["session_id"]],
    )
    assert result.success is True
    assert result.sessions_copied == 1


def test_app_feature_smoke(monkeypatch, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)

    async def run_app() -> None:
        app = CCTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_tab("tab-dashboard")
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            dashboard = app.query_one("DashboardPane")
            assert dashboard._period == "weekly"
            await pilot.press("tab")
            await pilot.pause()
            assert dashboard._period == "monthly"
            app.action_tab("tab-projects")
            await pilot.pause()
            app.action_tab("tab-file-history")
            await pilot.pause()
            app.action_tab("tab-debug-todos")
            await pilot.pause()
            app.action_tab("tab-migrate")
            await pilot.pause()
            app.action_tab("tab-backups")
            await pilot.pause()
            app.action_refresh()
            await pilot.pause()

            assert app.query_one("#main-tabs") is not None
            assert app.query_one("#project-tree") is not None
            assert app.query_one("#fh-table") is not None
            assert app.query_one("#debug-table") is not None
            assert app.query_one("#session-table") is not None
            assert app.query_one("#backups-table") is not None

    asyncio.run(run_app())


def test_projects_filter_by_session_title(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)

    async def run_app() -> None:
        from textual.widgets import Input, Tree

        app = CCTuiApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_tab("tab-projects")
            await pilot.pause()

            pane = app.query_one(projects_screen.ProjectsPane)
            for _ in range(100):  # wait for the initial project load worker
                if pane._all_projects:
                    break
                await pilot.pause(0.05)
            assert pane._all_projects

            # "alpha prompt" is project_a's session title and matches no path.
            filter_input = app.query_one("#projects-filter", Input)
            filter_input.value = "alpha prompt"
            for _ in range(100):  # wait for the title-cache build worker
                if pane._session_match_map:
                    break
                await pilot.pause(0.05)

            tree = app.query_one("#project-tree", Tree)

            def walk(node, acc):
                for child in node.children:
                    acc.append(child)
                    walk(child, acc)
                return acc

            nodes = walk(tree.root, [])
            project_paths = [n.data[1] for n in nodes if n.data and n.data[0] == "project"]
            session_ids = [n.data[1] for n in nodes if n.data and n.data[0] == "session"]
            assert env["project_a"] in project_paths
            assert env["project_b"] not in project_paths
            assert env["session_id"] in session_ids

    asyncio.run(run_app())
