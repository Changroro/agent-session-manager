"""Regression tests for session migration (path rewrite scope, selection UX)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from asm.services import migrate

SRC = "/home/me"
TGT = "/home/me/work/proj"  # source path is a prefix of the target — the
                            # combination that turned repeated rewrites into
                            # nested paths before the fix


def _write_session(path: Path, sid: str, cwd: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": cwd, "message": {"content": "hi"}}) + "\n"
    )


def _cwd_of(path: Path) -> str:
    return json.loads(path.read_text().splitlines()[0])["cwd"]


def test_repeat_migrate_does_not_renest_paths(monkeypatch, tmp_path: Path):
    env = _fake_env(monkeypatch, tmp_path)
    src_dir, tgt_dir = env["projects_dir"] / migrate.encode_path(SRC), env["projects_dir"] / migrate.encode_path(TGT)
    _write_session(src_dir, "s1", SRC)

    r1 = migrate.migrate_sessions(SRC, TGT)
    assert r1.success and r1.sessions_copied == 1
    assert _cwd_of(tgt_dir / "s1.jsonl") == TGT
    assert not (src_dir / "s1.jsonl").exists()  # moved, not copied

    # A later move of a new session leaves the earlier one alone.
    _write_session(src_dir, "s2", SRC)
    r2 = migrate.migrate_sessions(SRC, TGT)
    assert r2.sessions_copied == 1
    assert _cwd_of(tgt_dir / "s1.jsonl") == TGT
    assert _cwd_of(tgt_dir / "s2.jsonl") == TGT


def _fake_env(monkeypatch, tmp_path: Path) -> dict:
    from tests.test_feature_smoke import _setup_fake_claude

    return _setup_fake_claude(monkeypatch, tmp_path)


def _moved_env(monkeypatch, tmp_path: Path):
    env = _fake_env(monkeypatch, tmp_path)
    projects = env["projects_dir"]
    return env, projects / migrate.encode_path(SRC), projects / migrate.encode_path(TGT)


def test_move_carries_sidecar_and_trashes_original(monkeypatch, tmp_path: Path):
    env, src_dir, tgt_dir = _moved_env(monkeypatch, tmp_path)
    _write_session(src_dir, "s1", SRC)
    sub = src_dir / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(json.dumps({"type": "user", "cwd": SRC + "/x"}) + "\n")
    (src_dir / "s1" / "tool-results").mkdir()
    (src_dir / "s1" / "tool-results" / "r.txt").write_text("big output")
    (src_dir / "sessions-index.json").write_text(json.dumps(
        {"entries": [{"sessionId": "s1", "projectPath": SRC}, {"sessionId": "other", "projectPath": SRC}]}
    ))

    result = migrate.migrate_sessions(SRC, TGT, session_ids=["s1"])

    assert result.success, result.message
    assert _cwd_of(tgt_dir / "s1" / "subagents" / "agent-a.jsonl") == TGT + "/x"
    assert (tgt_dir / "s1" / "tool-results" / "r.txt").read_text() == "big output"
    assert not (src_dir / "s1.jsonl").exists() and not (src_dir / "s1").exists()
    assert [e["sessionId"] for e in json.loads((src_dir / "sessions-index.json").read_text())["entries"]] == ["other"]
    tgt_index = json.loads((tgt_dir / "sessions-index.json").read_text())["entries"]
    assert tgt_index == [{"sessionId": "s1", "projectPath": TGT}]
    snapshots = list((env["home"] / ".asm" / "recovery").iterdir())
    assert len(snapshots) == 2  # original jsonl + sidecar dir


def test_append_never_rewrites_existing_target_files(monkeypatch, tmp_path: Path):
    _env, src_dir, tgt_dir = _moved_env(monkeypatch, tmp_path)
    _write_session(src_dir, "dup", SRC)
    _write_session(src_dir, "new", SRC)
    tgt_dir.mkdir(parents=True)
    live = '{"type": "user", "cwd": "%s", "message": {"content": "latest turn"}}\n' % TGT
    (tgt_dir / "dup.jsonl").write_text(live)
    (tgt_dir / "native.jsonl").write_text(live)

    result = migrate.migrate_sessions(SRC, TGT)

    assert result.success and result.sessions_copied == 1 and result.sessions_skipped == 1
    assert (tgt_dir / "dup.jsonl").read_text() == live
    assert (tgt_dir / "native.jsonl").read_text() == live
    assert (src_dir / "dup.jsonl").exists()  # skipped → original kept
    assert not (src_dir / "new.jsonl").exists()


def test_overwrite_replaces_only_conflicting_sessions(monkeypatch, tmp_path: Path):
    _env, src_dir, tgt_dir = _moved_env(monkeypatch, tmp_path)
    _write_session(src_dir, "dup", SRC)
    _write_session(tgt_dir, "dup", "/old")
    _write_session(tgt_dir, "native", TGT)

    result = migrate.migrate_sessions(SRC, TGT, mode="overwrite")

    assert result.success, result.message
    assert _cwd_of(tgt_dir / "dup.jsonl") == TGT
    assert _cwd_of(tgt_dir / "native.jsonl") == TGT


def test_trailing_slash_targets_the_same_project(monkeypatch, tmp_path: Path):
    _env, src_dir, tgt_dir = _moved_env(monkeypatch, tmp_path)
    _write_session(src_dir, "s1", SRC)

    assert migrate.migrate_sessions(SRC + "/", TGT + "/").success
    assert _cwd_of(tgt_dir / "s1.jsonl") == TGT


def test_line_separator_inside_json_string_survives(monkeypatch, tmp_path: Path):
    _env, src_dir, tgt_dir = _moved_env(monkeypatch, tmp_path)
    src_dir.mkdir(parents=True)
    record = {"type": "user", "cwd": SRC, "message": {"content": "a\u2028b"}}
    (src_dir / "s1.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    assert migrate.migrate_sessions(SRC, TGT).success
    moved = json.loads((tgt_dir / "s1.jsonl").read_text(encoding="utf-8").split("\n")[0])
    assert moved["message"]["content"] == "a\u2028b" and moved["cwd"] == TGT


def test_migrate_leaves_native_target_sessions_untouched(monkeypatch, tmp_path: Path):
    _env, src_dir, tgt_dir = _moved_env(monkeypatch, tmp_path)
    _write_session(src_dir, "incoming", SRC)
    # A session that was born in the target project (its cwd contains the
    # source path as a prefix — the old rewrite corrupted exactly these).
    _write_session(tgt_dir, "native", TGT)

    result = migrate.migrate_sessions(SRC, TGT)
    assert result.success
    assert _cwd_of(tgt_dir / "native.jsonl") == TGT
    assert _cwd_of(tgt_dir / "incoming.jsonl") == TGT


def test_migrate_pane_defaults_to_no_selection(monkeypatch, tmp_path: Path):
    from asm.app import CCTuiApp
    from tests.test_feature_smoke import _setup_fake_claude

    _setup_fake_claude(monkeypatch, tmp_path)

    async def run():
        app = CCTuiApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            pane = app.query_one("MigratePane")
            pane._source_hint = "/x"
            pane._populate_session_table("/x", [
                ("sid-1", "07/24 10:00", "first", "1KB"),
                ("sid-2", "07/24 11:00", "second", "2KB"),
            ])
            assert pane._selected_sessions == set()  # nothing pre-checked
            pane.action_toggle_all()
            assert pane._selected_sessions == {"sid-1", "sid-2"}
            pane.action_toggle_all()
            assert pane._selected_sessions == set()
            # No selection → migrate refuses before showing any dialog.
            pane._source_encoded = "-x"
            pane._target_encoded = "-y"
            pane._target_hint = "/y"
            pane._start_migrate()
            await pilot.pause()
            assert len(app.screen_stack) == 1  # no confirm screen pushed

    asyncio.run(run())


def test_copy_gives_new_ids_and_keeps_originals(monkeypatch, tmp_path: Path):
    _env, src_dir, tgt_dir = _moved_env(monkeypatch, tmp_path)
    src_dir.mkdir(parents=True)
    (src_dir / "s1.jsonl").write_text(
        json.dumps({"type": "user", "cwd": SRC, "sessionId": "s1", "message": {"content": "hi"}}) + "\n"
    )
    sub = src_dir / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(json.dumps({"type": "user", "cwd": SRC, "sessionId": "s1"}) + "\n")
    (src_dir / "sessions-index.json").write_text(json.dumps({"entries": [
        {"sessionId": "s1", "projectPath": SRC, "fullPath": str(src_dir / "s1.jsonl")},
    ]}))
    before = (src_dir / "s1.jsonl").read_text()

    result = migrate.migrate_sessions(SRC, TGT, copy=True)

    assert result.success, result.message
    new_id = result.new_ids["s1"]
    assert new_id != "s1"
    assert (src_dir / "s1.jsonl").read_text() == before and (src_dir / "s1").is_dir()  # untouched
    copied = json.loads((tgt_dir / f"{new_id}.jsonl").read_text())
    assert copied["sessionId"] == new_id and copied["cwd"] == TGT
    sub_rec = json.loads((tgt_dir / new_id / "subagents" / "agent-a.jsonl").read_text())
    assert sub_rec["sessionId"] == new_id and sub_rec["cwd"] == TGT
    entry = json.loads((tgt_dir / "sessions-index.json").read_text())["entries"][0]
    assert entry == {"sessionId": new_id, "projectPath": TGT, "fullPath": str(tgt_dir / f"{new_id}.jsonl")}

    # Copying again makes another independent copy rather than colliding.
    again = migrate.migrate_sessions(SRC, TGT, copy=True)
    assert again.success and again.new_ids["s1"] not in (new_id, "s1")


def test_move_points_index_full_path_at_target(monkeypatch, tmp_path: Path):
    _env, src_dir, tgt_dir = _moved_env(monkeypatch, tmp_path)
    _write_session(src_dir, "s1", SRC)
    (src_dir / "sessions-index.json").write_text(json.dumps({"entries": [
        {"sessionId": "s1", "fullPath": str(src_dir / "s1.jsonl")},
    ]}))
    assert migrate.migrate_sessions(SRC, TGT).success
    entry = json.loads((tgt_dir / "sessions-index.json").read_text())["entries"][0]
    assert entry["fullPath"] == str(tgt_dir / "s1.jsonl")
