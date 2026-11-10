"""End-to-end feature coverage for both Claude and Codex modes.

Destructive operations run against sandboxed temp dirs (send2trash is faked);
the TUI is driven headlessly through every tab in both modes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from asm.app import CCTuiApp
from asm.services import backup as backup_service
from asm.services import claude_data, cleaner, codex_data, recovery
from asm import models

from tests.test_feature_smoke import _fake_send2trash, _setup_fake_claude


# --------------------------------------------------------------------------- #
# Claude mode — service-level features not covered by the smoke test
# --------------------------------------------------------------------------- #

class TestClaudeExtraFeatures:
    def test_duplicate_sessions_detect_and_trash(self, monkeypatch, tmp_path):
        env = _setup_fake_claude(monkeypatch, tmp_path)
        # Put the same session id into project B as well -> duplicate.
        dup = env["session_id"]
        (env["projects_dir"] / env["encoded_b"] / f"{dup}.jsonl").write_text("{}\n")

        dups = claude_data.find_duplicate_sessions()
        assert dup in dups
        assert set(dups[dup]) == {env["encoded_a"], env["encoded_b"]}

        # Trash just the copy in project B.
        assert cleaner.trash_single_session_file(env["encoded_b"], dup) is True

    def test_find_empty_sessions(self, monkeypatch, tmp_path):
        import json as _json
        env = _setup_fake_claude(monkeypatch, tmp_path)
        d = env["projects_dir"] / env["encoded_a"]
        # a stub session (title only, no conversation)
        (d / "stub00000-0000-0000-0000-000000000000.jsonl").write_text(
            _json.dumps({"type": "ai-title", "aiTitle": "stub task"}) + "\n"
        )
        empties = claude_data.find_empty_sessions()
        ids = {e["session_id"] for e in empties}
        assert "stub00000-0000-0000-0000-000000000000" in ids
        # the real session (env session_id, has user+assistant) is NOT flagged
        assert env["session_id"] not in ids

    def test_metadata_only_session_preview(self, monkeypatch, tmp_path):
        import json as _json
        env = _setup_fake_claude(monkeypatch, tmp_path)
        d = env["projects_dir"] / env["encoded_a"]
        sid = "meta0nly-0000-0000-0000-000000000000"
        (d / f"{sid}.jsonl").write_text(
            _json.dumps({"type": "ai-title", "aiTitle": "just a title"}) + "\n"
            + _json.dumps({"type": "pr-link", "url": "https://x/pr/1"}) + "\n"
        )
        msgs = claude_data.get_session_messages(sid, project_dir=env["encoded_a"], limit=10)
        assert len(msgs) == 1 and msgs[0]["type"] == "meta"
        assert "just a title" in msgs[0]["content"]
        assert "https://x/pr/1" in msgs[0]["content"]

    def test_remove_project_from_json(self, monkeypatch, tmp_path):
        env = _setup_fake_claude(monkeypatch, tmp_path)
        assert claude_data.remove_project_from_json(env["project_b"]) is True
        remaining = claude_data.get_project_paths()
        assert env["project_b"] not in remaining
        assert env["project_a"] in remaining

    def test_tasks_based_todos(self, monkeypatch, tmp_path):
        env = _setup_fake_claude(monkeypatch, tmp_path)
        tasks_dir = env["claude_dir"] / "tasks"
        active = env["session_id"]
        (tasks_dir / active).mkdir(parents=True)
        (tasks_dir / active / "1.json").write_text('{"subject":"do","status":"pending"}')
        (tasks_dir / "orphan-sess").mkdir()  # empty -> prunable + orphaned
        monkeypatch.setattr(claude_data, "TASKS_DIR", tasks_dir)
        monkeypatch.setattr(cleaner, "TASKS_DIR", tasks_dir)

        todos = {t.name: t for t in claude_data.get_todos()}
        assert active in todos and not todos[active].is_orphaned
        assert "orphan-sess" in todos and todos["orphan-sess"].is_orphaned
        assert cleaner.count_empty_todos() == 1
        ok, fail = cleaner.prune_empty_todo_files()
        assert ok == 1

    def test_backup_restore_roundtrip(self, monkeypatch, tmp_path):
        env = _setup_fake_claude(monkeypatch, tmp_path)
        cfg = backup_service.create_config_backup()
        full = backup_service.create_full_backup()
        assert cfg and full

        # Corrupt the live config, then restore it.
        env["claude_json"].write_text('{"projects": {}}')
        assert backup_service.restore_config_backup(cfg) is True
        restored = json.loads(env["claude_json"].read_text())
        assert env["project_a"] in restored.get("projects", {})

        # Full restore should rebuild the .claude dir.
        assert backup_service.restore_full_backup(full) is True
        assert (env["claude_dir"] / "projects").exists()

    def test_recovery_snapshot_and_restore(self, monkeypatch, tmp_path):
        env = _setup_fake_claude(monkeypatch, tmp_path)
        recovery_dir = env["home"] / ".cc-tui" / "recovery"
        monkeypatch.setattr(recovery, "RECOVERY_BASE_DIR", recovery_dir)
        monkeypatch.setattr(recovery, "CLAUDE_DIR", env["claude_dir"])
        monkeypatch.setattr(models, "RECOVERY_BASE_DIR", recovery_dir)

        target = env["debug_dir"] / f"{env['session_id']}.log"
        snap_id = recovery.create_recovery_snapshot(target, "debug")
        assert snap_id is not None
        items = recovery.list_recovery_items()
        assert any(i.id == snap_id for i in items)

        target.unlink()  # simulate deletion
        ok, _msg = recovery.restore_recovery_item(snap_id)
        assert ok is True
        assert target.exists()


# --------------------------------------------------------------------------- #
# Codex mode
# --------------------------------------------------------------------------- #

def _write_codex_rollout(path: Path, sid: str, cwd: str, model: str, first_user: str):
    rows = [
        {"type": "session_meta", "payload": {"id": sid, "timestamp": "2026-06-01T09:00:00.000Z",
                                             "cwd": cwd, "model": model, "git": {"branch": "main"}}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": first_user}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "done"}]}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 200, "total_tokens": 1200}}}},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _setup_fake_codex(monkeypatch, tmp_path) -> dict:
    codex_dir = tmp_path / ".codex"
    sessions = codex_dir / "sessions" / "2026" / "06" / "01"
    _write_codex_rollout(sessions / "rollout-a.jsonl", "aaaa", "/work/proj-a", "gpt-5.5", "first task")
    _write_codex_rollout(sessions / "rollout-b.jsonl", "bbbb", "/work/proj-b", "gpt-5", "second task")
    (codex_dir / "config.toml").write_text("x=1")
    backups_dir = tmp_path / ".cc-tui" / "backups"
    backups_dir.mkdir(parents=True)

    codex_data.refresh()
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", codex_dir / "sessions")
    # Isolate the Claude side (the combined app loads both) to an empty dir.
    empty_claude = tmp_path / ".claude-empty"
    (empty_claude / "projects").mkdir(parents=True)
    monkeypatch.setattr(claude_data, "PROJECTS_DIR", empty_claude / "projects")
    monkeypatch.setattr(claude_data, "CLAUDE_DIR", empty_claude)
    monkeypatch.setattr(claude_data, "CLAUDE_JSON", empty_claude / ".claude.json")
    monkeypatch.setattr(backup_service, "CODEX_DIR", codex_dir)
    monkeypatch.setattr(backup_service, "CODEX_SESSIONS_DIR", codex_dir / "sessions")
    monkeypatch.setattr(backup_service, "BACKUP_BASE_DIR", backups_dir)
    monkeypatch.setattr(cleaner, "send2trash", _fake_send2trash)
    monkeypatch.setattr(cleaner, "_TRASH_LOG", tmp_path / ".asm" / "trash-log.jsonl")
    monkeypatch.setattr(recovery, "RECOVERY_BASE_DIR", tmp_path / ".cc-tui" / "recovery")
    return {"codex_dir": codex_dir, "sessions_dir": codex_dir / "sessions", "backups_dir": backups_dir}


class TestCodexFeatures:
    def test_projects_sessions_messages(self, monkeypatch, tmp_path):
        _setup_fake_codex(monkeypatch, tmp_path)
        assert codex_data.is_available()
        assert codex_data.total_session_count() == 2
        projects = {p.path for p in codex_data.get_projects()}
        assert projects == {"/work/proj-a", "/work/proj-b"}

        ses = codex_data.get_project_sessions("/work/proj-a")
        assert len(ses) == 1 and ses[0].session_id == "aaaa"
        msgs = codex_data.get_session_messages("aaaa", ses[0].project_dir)
        assert {m["type"] for m in msgs} == {"user", "assistant"}

    def test_dashboard_aggregations(self, monkeypatch, tmp_path):
        _setup_fake_codex(monkeypatch, tmp_path)
        usage = codex_data.get_usage_data()
        assert usage["total_cost"] > 0
        assert codex_data.get_period_usage("daily")
        assert codex_data.get_stats().total_sessions == 2

    def test_codex_backup(self, monkeypatch, tmp_path):
        _setup_fake_codex(monkeypatch, tmp_path)
        path = backup_service.create_codex_backup()
        assert path is not None
        assert (Path(path) / "sessions").exists()
        assert (Path(path) / "config.toml").exists()
        assert any(b.backup_type == "codex" for b in backup_service.list_backups())

    def test_trash_codex_session(self, monkeypatch, tmp_path):
        env = _setup_fake_codex(monkeypatch, tmp_path)
        rollout = env["sessions_dir"] / "2026" / "06" / "01" / "rollout-a.jsonl"
        assert cleaner.trash_codex_session(str(rollout)) is True
        # Outside ~/.codex must be refused.
        outside = tmp_path / "evil.jsonl"
        outside.write_text("{}")
        assert cleaner.trash_codex_session(str(outside)) is False

    def test_move_codex_session_rewrites_cwd(self, monkeypatch, tmp_path):
        env = _setup_fake_codex(monkeypatch, tmp_path)
        ses = codex_data.get_project_sessions("/work/proj-a")
        rollout = ses[0].project_dir
        assert codex_data.move_session(rollout, "/work/proj-b") is True
        codex_data.refresh()
        # The session now belongs to proj-b.
        a = codex_data.get_project_sessions("/work/proj-a")
        b = codex_data.get_project_sessions("/work/proj-b")
        assert all(s.session_id != "aaaa" for s in a)
        assert any(s.session_id == "aaaa" for s in b)

    def test_move_codex_session_updates_state_db_and_keeps_subdirs(self, monkeypatch, tmp_path):
        import sqlite3

        env = _setup_fake_codex(monkeypatch, tmp_path)
        rollout = codex_data.get_project_sessions("/work/proj-a")[0].project_dir
        with open(rollout, "a") as fh:
            fh.write(json.dumps({"type": "turn_context", "payload": {"cwd": "/work/proj-a/sub"}}) + "\n")
            fh.write(json.dumps({"type": "turn_context", "payload": {"cwd": "/elsewhere"}}) + "\n")
        db = env["codex_dir"] / "state_5.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT)")
            conn.execute("INSERT INTO threads VALUES ('aaaa', ?, '/work/proj-a')", (rollout,))
            conn.execute("INSERT INTO threads VALUES ('bbbb', 'x', '/work/proj-b')")

        assert codex_data.move_session(rollout, "/work/proj-c/") is True

        cwds = [
            (json.loads(line).get("payload") or {}).get("cwd")
            for line in Path(rollout).read_text().split("\n") if line
        ]
        assert "/work/proj-c" in cwds and "/work/proj-c/sub" in cwds and "/elsewhere" in cwds
        assert "/work/proj-a" not in cwds
        with sqlite3.connect(db) as conn:
            assert dict(conn.execute("SELECT id, cwd FROM threads")) == {"aaaa": "/work/proj-c", "bbbb": "/work/proj-b"}

    def test_move_codex_session_leaves_file_alone_when_db_locked(self, monkeypatch, tmp_path):
        import sqlite3

        env = _setup_fake_codex(monkeypatch, tmp_path)
        rollout = codex_data.get_project_sessions("/work/proj-a")[0].project_dir
        before = Path(rollout).read_text()
        db = env["codex_dir"] / "state_5.sqlite"
        locker = sqlite3.connect(db, isolation_level=None)
        locker.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT)")
        locker.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(sqlite3, "connect", lambda path, **kw: sqlite3.Connection(path, timeout=0.1, isolation_level=None))
        try:
            assert codex_data.move_session(rollout, "/work/proj-c") is False
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        assert Path(rollout).read_text() == before

    def test_copy_codex_session_new_id_db_row_and_zero_usage(self, monkeypatch, tmp_path):
        import sqlite3

        env = _setup_fake_codex(monkeypatch, tmp_path)
        sess = env["sessions_dir"] / "2026" / "06" / "01"
        old_id = "019ddddd-da44-7a72-8b78-912063531fae"
        orig = sess / f"rollout-2026-06-01T09-00-00-{old_id}.jsonl"
        orig.write_text("\n".join(json.dumps(r) for r in [
            {"type": "session_meta", "payload": {"id": old_id, "cwd": "/work/proj-a"}},
            {"type": "turn_context", "payload": {"cwd": "/work/proj-a/sub", "model": "gpt-5.5"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 500, "output_tokens": 70, "total_tokens": 570}}}},
        ]) + "\n")
        before = orig.read_text()
        db = env["codex_dir"] / "state_5.sqlite"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT, tokens_used INTEGER)")
            conn.execute("INSERT INTO threads VALUES (?, ?, '/work/proj-a', 't', 570)", (old_id, str(orig)))

        new_id = codex_data.copy_session(str(orig), "/work/proj-c")

        assert new_id and new_id != old_id
        assert orig.read_text() == before
        copy = sess / f"rollout-2026-06-01T09-00-00-{new_id}.jsonl"
        recs = [json.loads(line) for line in copy.read_text().split("\n") if line]
        assert recs[0]["payload"] == {"id": new_id, "cwd": "/work/proj-c"}
        assert recs[1]["payload"]["cwd"] == "/work/proj-c/sub"
        usage = recs[2]["payload"]["info"]["total_token_usage"]
        assert usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 570}
        with sqlite3.connect(db) as conn:
            rows = dict((r[0], r[1:]) for r in conn.execute("SELECT id, rollout_path, cwd, title, tokens_used FROM threads"))
        assert rows[old_id] == (str(orig), "/work/proj-a", "t", 570)
        assert rows[new_id] == (str(copy), "/work/proj-c", "t", 0)

    def test_codex_backup_restore_roundtrip(self, monkeypatch, tmp_path):
        env = _setup_fake_codex(monkeypatch, tmp_path)
        path = backup_service.create_codex_backup()
        assert path is not None

        # Wipe live sessions, then restore from the backup.
        import shutil as _sh
        _sh.rmtree(env["sessions_dir"])
        assert not env["sessions_dir"].exists()
        assert backup_service.restore_codex_backup(path) is True
        assert (env["sessions_dir"] / "2026" / "06" / "01" / "rollout-a.jsonl").exists()

    def test_codex_restore_rolls_back_on_failure(self, monkeypatch, tmp_path):
        env = _setup_fake_codex(monkeypatch, tmp_path)
        path = backup_service.create_codex_backup()
        assert path is not None

        # Force the copy to fail mid-restore; the live sessions must survive.
        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(backup_service.shutil, "copytree", _boom)
        assert backup_service.restore_codex_backup(path) is False
        assert (env["sessions_dir"] / "2026" / "06" / "01" / "rollout-a.jsonl").exists()


# --------------------------------------------------------------------------- #
# UI drive — every tab in both modes
# --------------------------------------------------------------------------- #

class TestUIBothModes:
    def test_claude_app_all_tabs(self, monkeypatch, tmp_path):
        env = _setup_fake_claude(monkeypatch, tmp_path)
        # Add a duplicate so the Projects "Duplicate Sessions" group renders.
        (env["projects_dir"] / env["encoded_b"] / f"{env['session_id']}.jsonl").write_text("{}\n")

        async def run():
            app = CCTuiApp(source="claude")
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause(); await asyncio.sleep(0.3); await pilot.pause()
                for tab in ("tab-dashboard", "tab-projects", "tab-file-history",
                            "tab-debug-todos", "tab-migrate", "tab-backups"):
                    app.action_tab(tab)
                    await pilot.pause(); await asyncio.sleep(0.2); await pilot.pause()
                # Projects tree should include the duplicate group.
                from textual.widgets import Tree
                app.action_tab("tab-projects")
                await pilot.pause(); await asyncio.sleep(0.4); await pilot.pause()
                tree = app.query_one("#project-tree", Tree)
                kinds = {c.data[0] for c in tree.root.children if c.data}
                assert "dup_group" in kinds
                app.action_refresh()
                await pilot.pause()

        asyncio.run(run())

    def test_codex_merged_into_projects_tab(self, monkeypatch, tmp_path):
        _setup_fake_codex(monkeypatch, tmp_path)

        async def run():
            app = CCTuiApp()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause(); await asyncio.sleep(0.8); await pilot.pause()
                from textual.widgets import TabbedContent
                ids = {tp.id for tp in app.query_one("#main-tabs", TabbedContent).query("TabPane")}
                # Codex now lives inside the unified Projects tab, not its own tab.
                assert "tab-codex-sessions" not in ids
                assert "tab-projects" in ids

                app.action_tab("tab-projects")  # panes load on first view
                await pilot.pause(); await asyncio.sleep(0.8); await pilot.pause()
                pane = app.query_one("ProjectsPane")
                assert "/work/proj-a" in pane._codex_paths
                paths = {p.path for p in pane._all_projects}
                assert {"/work/proj-a", "/work/proj-b"} <= paths

        asyncio.run(run())


class TestDashboardPeriodAndEditor:
    def test_period_tab_click_switches(self, monkeypatch, tmp_path):
        """Clicking the Weekly/Monthly sub-tabs must switch the displayed period."""
        _setup_fake_claude(monkeypatch, tmp_path)

        async def run():
            app = CCTuiApp()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause(); await asyncio.sleep(1.0); await pilot.pause()
                d = app.query_one("DashboardPane")
                for label, expected in (("Weekly", "weekly"), ("Monthly", "monthly")):
                    for tb in app.query("#period-tabs Tab"):
                        if label in str(tb.label):
                            await pilot.click(tb)
                            break
                    await pilot.pause(); await asyncio.sleep(0.3); await pilot.pause()
                    assert d._period == expected

        asyncio.run(run())

    def test_instruction_file_editor_saves(self, monkeypatch, tmp_path):
        _setup_fake_claude(monkeypatch, tmp_path)
        from asm.screens.file_editor import FileEditorScreen
        from textual.widgets import TextArea

        target = tmp_path / "CLAUDE.md"

        async def run():
            app = CCTuiApp()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                saved = []
                app.push_screen(FileEditorScreen(target), callback=lambda s: saved.append(s))
                await pilot.pause(); await asyncio.sleep(0.3); await pilot.pause()
                app.screen.query_one("#editor-area", TextArea).text = "# rules\n- concise"
                await pilot.pause()
                app.screen.action_save()
                await pilot.pause(); await asyncio.sleep(0.2); await pilot.pause()
                assert target.read_text().startswith("# rules")
                assert saved == [True]

        asyncio.run(run())


def test_projects_resume_action_sets_target_and_exits(monkeypatch, tmp_path):
    _setup_fake_claude(monkeypatch, tmp_path)

    async def run():
        app = CCTuiApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            pane = app.query_one("ProjectsPane")
            pane._preview_target = ("sid-1", "claude", "/work/proj")
            pane.action_resume_session()
            await pilot.pause()
        assert app.resume_target == ("sid-1", "claude", "/work/proj")

    asyncio.run(run())
