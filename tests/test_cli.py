"""Tests for the headless CLI subcommands (asm <command>)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from asm.__main__ import main
from tests.test_feature_smoke import _setup_fake_claude


def _run(monkeypatch, capsys, *argv: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["asm", *argv])
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code or 0
    return code, capsys.readouterr().out


def _find(monkeypatch, capsys, *argv) -> dict[str, str]:
    code, out = _run(monkeypatch, capsys, "find", *argv, "--json")
    assert code == 0
    return {r["session_id"]: r["match"] for r in json.loads(out)}


def test_cli_find_title_and_content_scopes(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    sid = env["session_id"]  # title "alpha prompt", body also has "alpha answer"
    assert _find(monkeypatch, capsys, "alpha") == {sid: "title"}
    assert _find(monkeypatch, capsys, "alpha answer") == {sid: "content"}  # body only
    assert _find(monkeypatch, capsys, "alpha answer", "--only-title") == {}
    assert _find(monkeypatch, capsys, "alpha prompt", "--only-content") == {sid: "content"}
    assert _find(monkeypatch, capsys, "beta") == {env["other_session_id"]: "title"}


def test_cli_find_marks_subagent_only_hits(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    sub = Path(env["projects_dir"]) / env["encoded_b"] / env["other_session_id"] / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text('{"type":"assistant","message":{"content":"zebra"}}\n')
    assert _find(monkeypatch, capsys, "zebra") == {env["other_session_id"]: "sub-agent"}


def test_cli_find_without_query_lists_all(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "find", "--json")
    assert code == 0
    rows = json.loads(out)
    ids = {r["session_id"] for r in rows}
    assert {env["session_id"], env["other_session_id"]} <= ids


def test_cli_cost_json(monkeypatch, capsys, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "cost", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["claude"]["total_cost"] > 0


def test_cli_projects(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "projects")
    assert code == 0
    assert env["project_a"] in out


def test_cli_preview(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "preview", env["session_id"])
    assert code == 0
    assert "alpha prompt" in out


def test_cli_preview_missing(monkeypatch, capsys, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)
    code, _ = _run(monkeypatch, capsys, "preview", "no-such-session")
    assert code == 1


def test_cli_clean_empty_dry_run(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    # project_b's session is a single user message -> counted as empty stub.
    code, out = _run(monkeypatch, capsys, "clean", "empty", "--dry-run")
    assert code == 0
    jsonl = Path(env["projects_dir"]) / env["encoded_b"] / f"{env['other_session_id']}.jsonl"
    assert jsonl.exists()  # dry run deletes nothing


def test_cli_trash_session(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    jsonl = Path(env["projects_dir"]) / env["encoded_a"] / f"{env['session_id']}.jsonl"
    assert jsonl.exists()
    code, out = _run(monkeypatch, capsys, "trash", env["session_id"], "--yes")
    assert code == 0
    assert not jsonl.exists()


def test_cli_backup_create_and_list(monkeypatch, capsys, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "backup", "create", "--type", "config")
    assert code == 0
    created = out.strip()
    assert Path(created).exists()
    code, out = _run(monkeypatch, capsys, "backup", "list")
    assert code == 0
    assert created in out


def test_cli_move_selected_session_by_id_prefix(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    target = str(Path(env["home"]) / "work" / "project-gamma")
    code, out = _run(
        monkeypatch, capsys,
        "move", env["project_a"], target + "/", "--sessions", env["session_id"][:8], "--yes",
    )
    assert code == 0, out
    assert "1 sessions moved" in out
    projects = Path(env["projects_dir"])
    assert not (projects / env["encoded_a"] / f"{env['session_id']}.jsonl").exists()
    assert (projects / claude_data_encode(target) / f"{env['session_id']}.jsonl").exists()


def claude_data_encode(path: str) -> str:
    from asm.models import encode_path

    return encode_path(path)


def test_cli_move_all_claude_and_codex_sessions_of_a_project(monkeypatch, capsys, tmp_path: Path):
    from asm.services import codex_data
    from tests.test_codex_data import _write_rollout_real_layout

    env = _setup_fake_claude(monkeypatch, tmp_path)
    sessions = tmp_path / "codex" / "sessions"
    rollout = sessions / "2026" / "06" / "01" / "rollout-2026-06-01T09-00-00-cccc.jsonl"
    _write_rollout_real_layout(rollout, "cccc", env["project_a"], ["gpt-5.5"], None)
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", sessions)
    codex_data.refresh()
    target = str(Path(env["home"]) / "work" / "project-gamma")

    code, out = _run(monkeypatch, capsys, "move", env["project_a"], target, "--yes", "--json")

    assert code == 0, out
    result = json.loads(out)
    assert result["claude"]["moved"] == 1 and result["codex"] == {"moved": 1, "failed": [], "ids": {"cccc": "cccc"}}
    codex_data.refresh()
    assert [s.session_id for s in codex_data.get_project_sessions(target)] == ["cccc"]


def test_cli_copy_keeps_original_and_reports_new_id(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    target = str(Path(env["home"]) / "work" / "project-gamma")
    code, out = _run(
        monkeypatch, capsys, "copy", env["project_a"], target, "--sessions", env["session_id"][:8], "--yes", "--json",
    )
    assert code == 0, out
    new_id = json.loads(out)["claude"]["ids"][env["session_id"]]
    projects = Path(env["projects_dir"])
    assert (projects / env["encoded_a"] / f"{env['session_id']}.jsonl").exists()
    assert (projects / claude_data_encode(target) / f"{new_id}.jsonl").exists()


def test_cli_move_refuses_session_not_in_source(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    code, _ = _run(
        monkeypatch, capsys,
        "move", env["project_a"], "/elsewhere", "--sessions", env["other_session_id"], "--yes",
    )
    assert code == 1
    assert (Path(env["projects_dir"]) / env["encoded_b"] / f"{env['other_session_id']}.jsonl").exists()


def test_cli_id_prefix_resolves_or_reports_ambiguity(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "preview", env["session_id"][:8], "--json")
    assert code == 0 and json.loads(out)

    proj = Path(env["projects_dir"]) / env["encoded_b"]
    for sid in ("abcd1234-0000-0000-0000-000000000000", "abcd5678-0000-0000-0000-000000000000"):
        (proj / f"{sid}.jsonl").write_text('{"type":"user","message":{"content":"x"}}\n')
    code, _ = _run(monkeypatch, capsys, "preview", "abcd")
    assert code == 1
    import pytest

    from asm.cli import AmbiguousSessionError, _resolve_session_id

    with pytest.raises(AmbiguousSessionError, match="matches several sessions"):
        _resolve_session_id("abcd")
    assert _resolve_session_id("abcd1") == "abcd1234-0000-0000-0000-000000000000"


def test_cli_resume_disambiguates_duplicate_with_project(monkeypatch, capsys, tmp_path: Path):
    import shutil

    env = _setup_fake_claude(monkeypatch, tmp_path)
    projects = Path(env["projects_dir"])
    name = f"{env['session_id']}.jsonl"
    shutil.copy(projects / env["encoded_a"] / name, projects / env["encoded_b"] / name)
    Path(env["project_b"]).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)  # resume chdirs into the project; restore afterwards
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    code, _ = _run(monkeypatch, capsys, "resume", env["session_id"], "--dry-run")
    assert code != 0  # ambiguous without --project
    code, _ = _run(monkeypatch, capsys, "resume", env["session_id"], "--project", env["project_b"], "--dry-run")
    assert code == 0
    assert Path.cwd() == Path(env["project_b"])


def test_cli_destructive_requires_confirmation(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    jsonl = Path(env["projects_dir"]) / env["encoded_a"] / f"{env['session_id']}.jsonl"
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    code, _ = _run(monkeypatch, capsys, "trash", env["session_id"])
    assert code == 1
    assert jsonl.exists()


def test_cli_resume_claude_dry_run(monkeypatch, capsys, tmp_path: Path):
    import os
    import shutil

    import asm.models as models

    work = tmp_path / "work" / "proj"
    work.mkdir(parents=True)
    projects = tmp_path / "projects"
    enc = models.encode_path(str(work))
    pdir = projects / enc
    pdir.mkdir(parents=True)
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    (pdir / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": str(work), "message": {"content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "PROJECTS_DIR", projects)
    monkeypatch.setattr(shutil, "which", lambda b: f"/usr/bin/{b}")
    chdirs: list = []
    monkeypatch.setattr(os, "chdir", lambda d: chdirs.append(d))

    monkeypatch.setattr(sys, "argv", ["asm", "resume", sid, "--dry-run"])
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code or 0
    err = capsys.readouterr().err
    assert code == 0
    assert chdirs == [str(work)]  # cd into the recorded cwd
    assert f"claude -r {sid}" in err


def test_cli_resume_codex_dry_run(monkeypatch, capsys, tmp_path: Path):
    import os
    import shutil

    from asm.services import codex_data

    sessions = tmp_path / "sessions" / "2026" / "06" / "01"
    sessions.mkdir(parents=True)
    sid = "019ddddd-da44-7a72-8b78-912063531fae"
    cwd = str(tmp_path / "codexwork")
    (tmp_path / "codexwork").mkdir()
    (sessions / f"rollout-2026-06-01T09-00-00-{sid}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", tmp_path / "sessions")
    codex_data.refresh()
    monkeypatch.setattr(shutil, "which", lambda b: f"/usr/bin/{b}")
    chdirs: list = []
    monkeypatch.setattr(os, "chdir", lambda d: chdirs.append(d))
    # No Claude projects dir so it falls through to Codex.
    import asm.models as models
    monkeypatch.setattr(models, "PROJECTS_DIR", tmp_path / "no-claude")

    monkeypatch.setattr(sys, "argv", ["asm", "resume", sid, "--dry-run"])
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code or 0
    err = capsys.readouterr().err
    assert code == 0
    assert chdirs == [cwd]
    assert f"codex resume {sid}" in err


def test_cli_resume_not_found(monkeypatch, capsys, tmp_path: Path):
    import asm.models as models
    from asm.services import codex_data

    monkeypatch.setattr(models, "PROJECTS_DIR", tmp_path / "none")
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", tmp_path / "none2")
    code, _ = _run(monkeypatch, capsys, "resume", "no-such-id")
    assert code == 1


def test_cli_clean_debug_dry_run_deletes_nothing(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    empty_log = Path(env["debug_dir"]) / "orphaned.log"  # fixture content "[]"
    code, out = _run(monkeypatch, capsys, "clean", "debug", "--dry-run")
    assert code == 0
    assert empty_log.exists()
    assert "dry run" in out


def test_cli_clean_debug_requires_confirmation(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    empty_log = Path(env["debug_dir"]) / "orphaned.log"
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    code, _ = _run(monkeypatch, capsys, "clean", "debug")
    assert code == 1
    assert empty_log.exists()


def test_cli_clean_todos_with_yes(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    empty_todo = Path(env["todos_dir"]) / "orphaned-agent-test.json"  # fixture content "{}"
    full_todo = Path(env["todos_dir"]) / f"{env['session_id']}-agent-test.json"
    code, out = _run(monkeypatch, capsys, "clean", "todos", "--yes")
    assert code == 0
    assert not empty_todo.exists()
    assert full_todo.exists()


def test_cli_trash_codex_session_beyond_scan_window(monkeypatch, capsys, tmp_path: Path):
    from asm.services import codex_data
    from tests.test_codex_data import _write_rollout_real_layout

    _setup_fake_claude(monkeypatch, tmp_path)
    codex_root = tmp_path / "codex-sessions"
    rollout = codex_root / "2026" / "06" / "01" / "rollout-2026-06-01T09-00-00-cccc.jsonl"
    _write_rollout_real_layout(rollout, "cccc", "/work/proj-c", ["gpt-5.5"], None)
    # Trash validates against the scanned session dirs, so repointing
    # codex_data is enough — cleaner no longer holds its own root constant.
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", codex_root)
    codex_data.refresh()
    # Old capped lookup would miss ids outside the recent-N window; the by-id
    # path must not depend on the project scan at all.
    monkeypatch.setattr(codex_data, "get_projects", lambda *a, **k: [])
    code, out = _run(monkeypatch, capsys, "trash", "cccc", "--yes")
    assert code == 0
    assert not rollout.exists()


def test_cli_backup_create_full_confirms_with_size(monkeypatch, capsys, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    code, _ = _run(monkeypatch, capsys, "backup", "create", "--type", "full")
    assert code == 1
    code, out = _run(monkeypatch, capsys, "backup", "create", "--type", "full", "--yes")
    assert code == 0
    assert Path(out.strip()).exists()


def test_cli_artifacts_json(monkeypatch, capsys, tmp_path: Path):
    import json as _json

    env = _setup_fake_claude(monkeypatch, tmp_path)
    from asm.services import artifacts as artifacts_service
    monkeypatch.setattr(artifacts_service, "PROJECTS_DIR", Path(env["projects_dir"]))
    session = Path(env["projects_dir"]) / env["encoded_a"] / "artifact-sess.jsonl"
    rows = [
        {"type": "assistant", "timestamp": "2026-07-05T08:00:00Z",
         "message": {"content": [
             {"type": "tool_use", "id": "a1", "name": "Artifact",
              "input": {"file_path": "/tmp/page.html", "title": "My Page"}}]}},
        {"type": "user", "timestamp": "2026-07-05T08:00:01Z",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "a1",
              "content": "Published /tmp/page.html at https://claude.ai/code/artifact/ccc-333"}]}},
    ]
    session.write_text("".join(_json.dumps(r) + "\n" for r in rows))

    code, out = _run(monkeypatch, capsys, "artifacts", "--json")
    assert code == 0
    items = _json.loads(out)
    assert len(items) == 1
    assert items[0]["url"] == "https://claude.ai/code/artifact/ccc-333"
    assert items[0]["title"] == "My Page"


# ── asm import ────────────────────────────────────────────────────────────


def _codex_rollout(path: Path, cwd: str, first_text: str, extra_user: str | None = None) -> None:
    rows = [
        {
            "timestamp": "2026-08-05T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"session_id": "x", "cwd": cwd, "cli_version": "0.145.0"},
        },
        {
            "timestamp": "2026-08-05T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": first_text}],
            },
        },
    ]
    if extra_user:
        rows.append({
            "timestamp": "2026-08-05T00:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": extra_user}],
            },
        })
    rows.append({
        "timestamp": "2026-08-05T00:00:03.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "답변"}],
        },
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


@pytest.fixture
def import_env(monkeypatch, tmp_path: Path):
    from asm.services import agent_import, codex_data

    sessions = tmp_path / "codex" / "sessions"
    projects = tmp_path / "claude" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setattr(agent_import, "CODEX_SESSIONS_DIR", sessions)
    monkeypatch.setattr(agent_import, "PROJECTS_DIR", projects)
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", sessions)
    codex_data.refresh()
    yield sessions, projects
    codex_data.refresh()


def test_cli_import_list_shows_last_activity_newest_first(import_env, monkeypatch, capsys):
    import os

    sessions, _ = import_env
    old = sessions / "2026" / "08" / "05" / "rollout-2026-08-05T09-00-00-11111111-1111-4111-8111-111111111111.jsonl"
    new = sessions / "2026" / "08" / "01" / "rollout-2026-08-01T09-00-00-22222222-2222-4222-8222-222222222222.jsonl"
    _codex_rollout(old, "/work/a", "오래된 세션")
    _codex_rollout(new, "/work/b", "최근에 이어 쓴 세션")
    # `new` starts earlier by filename but was touched last -> it must sort first.
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))

    code, out = _run(monkeypatch, capsys, "import", "list", "--to", "claude")

    assert code == 0
    assert "newest activity first" in out
    body = [line for line in out.splitlines() if "turns" in line]
    assert "22222222" in body[0] and "11111111" in body[1]
    # utime above puts `new` in 2027 and `old` in 2023 — the printed activity
    # time is what makes the ordering checkable.
    assert "2027-" in body[0]
    assert "2023-" in body[1]


def test_cli_import_list_json_carries_last_active(import_env, monkeypatch, capsys):
    sessions, _ = import_env
    _codex_rollout(
        sessions / "2026" / "08" / "05" / "rollout-2026-08-05T09-00-00-33333333-3333-4333-8333-333333333333.jsonl",
        "/work/a",
        "제이슨 확인",
    )

    code, out = _run(monkeypatch, capsys, "import", "list", "--to", "claude", "--json")

    assert code == 0
    payload = json.loads(out)
    assert payload["new"][0]["title"] == "제이슨 확인"
    assert payload["new"][0]["last_active"]
    assert payload["truncated"] == 0


def test_cli_import_list_title_skips_harness_preamble(import_env, monkeypatch, capsys):
    sessions, _ = import_env
    _codex_rollout(
        sessions / "2026" / "08" / "05" / "rollout-2026-08-05T09-00-00-44444444-4444-4444-8444-444444444444.jsonl",
        "/work/a",
        "<recommended_plugins> here is a list of plugins",
        extra_user="실제로 내가 한 말",
    )

    code, out = _run(monkeypatch, capsys, "import", "list", "--to", "claude")

    assert code == 0
    assert "실제로 내가 한 말" in out
    assert "recommended_plugins" not in out


def test_cli_import_session_dry_run_infers_direction(import_env, monkeypatch, capsys):
    sessions, projects = import_env
    sid = "55555555-5555-4555-8555-555555555555"
    _codex_rollout(
        sessions / "2026" / "08" / "05" / f"rollout-2026-08-05T09-00-00-{sid}.jsonl",
        "/work/target",
        "옮길 대화",
    )

    code, out = _run(monkeypatch, capsys, "import", "session", sid, "--dry-run")

    assert code == 0
    assert "codex -> claude" in out
    assert "/work/target" in out
    assert list(projects.rglob("*.jsonl")) == []


def test_cli_import_session_writes_into_cwd_project(import_env, monkeypatch, capsys):
    from asm.services import backup

    monkeypatch.setattr(backup, "create_sessions_backup", lambda: None)
    monkeypatch.setattr("asm.services.agent_import.claude_cli_version", lambda: "2.1.228")
    sessions, projects = import_env
    sid = "66666666-6666-4666-8666-666666666666"
    _codex_rollout(
        sessions / "2026" / "08" / "05" / f"rollout-2026-08-05T09-00-00-{sid}.jsonl",
        "/work/target",
        "옮길 대화",
    )

    code, out = _run(monkeypatch, capsys, "import", "session", sid, "--yes")

    assert code == 0
    assert "imported as" in out
    written = list(projects.rglob("*.jsonl"))
    assert len(written) == 1
    assert written[0].parent.name == "-work-target"
    assert written[0].stem != sid  # a fresh id, not the Codex one


def test_cli_import_session_requires_id(monkeypatch, capsys):
    code, _out = _run(monkeypatch, capsys, "import", "session")
    assert code == 2


def test_cli_import_session_not_found(import_env, monkeypatch, capsys):
    code, _out = _run(monkeypatch, capsys, "import", "session", "no-such-session")
    assert code == 1


def test_cli_import_home_reports_single_active_home(monkeypatch, capsys, tmp_path: Path):
    from asm import models
    from asm.services import agent_import

    home = tmp_path / ".codex"
    sessions = home / "sessions" / "2026" / "08" / "05"
    sessions.mkdir(parents=True)
    (sessions / "rollout-2026-08-05T09-00-00-77777777-7777-4777-8777-777777777777.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    monkeypatch.setattr(models, "CODEX_DIR", home)
    monkeypatch.setattr(models, "CODEX_SESSIONS_DIR", home / "sessions")
    # cmd_import imports constants at call time from models.
    code, out = _run(monkeypatch, capsys, "import", "home")

    assert code == 0
    assert str(home) in out
    assert "1 sessions" in out


def test_cli_import_home_json(monkeypatch, capsys, tmp_path: Path):
    from asm import models

    home = tmp_path / ".codex"
    home.mkdir()
    monkeypatch.setattr(models, "CODEX_DIR", home)
    monkeypatch.setattr(models, "CODEX_SESSIONS_DIR", home / "sessions")

    code, out = _run(monkeypatch, capsys, "import", "home", "--json")

    assert code == 0
    assert json.loads(out)["codex_home"] == {
        "path": str(home), "exists": True, "sessions": 0
    }
