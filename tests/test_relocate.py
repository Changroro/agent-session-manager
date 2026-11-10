"""Live-session refusal and whole-project relocation (all on sandboxed tmp data)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from asm.services import claude_data, codex_data, migrate, relocate
from asm.services.live import LiveSessionError
from tests.test_codex_data import _write_rollout_real_layout
from tests.test_feature_smoke import _setup_fake_claude

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="uses /proc")


def _register_live_claude(claude_dir: Path, session_id: str, cwd: str) -> None:
    """Pretend this test process is a running Claude Code holding ``session_id``."""
    start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    sessions = claude_dir / "sessions"
    sessions.mkdir(exist_ok=True)
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": session_id, "cwd": cwd, "procStart": start}
    ))


def _fake_codex(monkeypatch, tmp_path: Path, cwd: str, sid: str = "019ddddd-da44-7a72-8b78-912063531fae") -> Path:
    sessions = tmp_path / "codex" / "sessions"
    rollout = sessions / "2026" / "06" / "01" / f"rollout-2026-06-01T09-00-00-{sid}.jsonl"
    _write_rollout_real_layout(rollout, sid, cwd, ["gpt-5.5"], None)
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", sessions)
    codex_data.refresh()
    return rollout


# ── live-session refusal ────────────────────────────────────────────────


def test_move_refuses_session_of_a_running_claude(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    _register_live_claude(Path(env["claude_dir"]), env["session_id"], env["project_a"])
    src = Path(env["projects_dir"]) / env["encoded_a"] / f"{env['session_id']}.jsonl"

    result = migrate.migrate_sessions(env["project_a"], "/elsewhere")

    assert not result.success and "running Claude Code" in result.message
    assert src.exists()
    assert migrate.migrate_sessions(env["project_a"], "/elsewhere", copy=True).success  # copy is fine


def test_dead_claude_registration_does_not_block(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    sessions = Path(env["claude_dir"]) / "sessions"
    sessions.mkdir()
    (sessions / "999999999.json").write_text(json.dumps(
        {"pid": 999999999, "sessionId": env["session_id"], "procStart": "1"}
    ))
    assert migrate.migrate_sessions(env["project_a"], "/elsewhere").success


def test_codex_move_refuses_rollout_held_open_for_writing(monkeypatch, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)
    rollout = _fake_codex(monkeypatch, tmp_path, "/work/a")
    before = rollout.read_text()
    with open(rollout, "a"):  # what Codex's app server does with loaded threads
        with pytest.raises(LiveSessionError):
            codex_data.move_session(str(rollout), "/work/b")
        assert codex_data.copy_session(str(rollout), "/work/b")  # copy leaves the original alone
    assert rollout.read_text() == before


# ── relocate ────────────────────────────────────────────────────────────


def _relocate_env(monkeypatch, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    old = Path(env["project_a"])
    (old / "src").mkdir(parents=True)
    (old / "src" / "run.sh").write_text(f"cd {old}/src && ./go\n")
    (old / "plain.txt").write_text("nothing to see\n")
    claude_json = Path(env["claude_json"])
    data = json.loads(claude_json.read_text())
    data["githubRepoPaths"] = {"me/alpha": [str(old)]}
    claude_json.write_text(json.dumps(data))
    rollout = _fake_codex(monkeypatch, tmp_path, str(old / "src"))
    config = rollout.parents[4] / "config.toml"
    config.write_text(f'model = "gpt-5.5"\n\n[projects."{old}"]\ntrust_level = "trusted"\n')
    return env, old, old.parent / "project-renamed", rollout, config


def test_relocate_moves_folder_sessions_and_config(monkeypatch, tmp_path: Path):
    env, old, new, rollout, config = _relocate_env(monkeypatch, tmp_path)

    p = relocate.plan(str(old), str(new) + "/")
    assert [ids for _o, _n, ids in p.claude_projects] == [[env["session_id"]]]
    assert len(p.codex_sessions) == 1 and p.claude_config_keys == [str(old)]
    r = relocate.apply(p)

    assert r.failures == []
    assert not old.exists() and (new / "src" / "run.sh").exists()
    projects = Path(env["projects_dir"])
    assert (projects / claude_data.encode_path(str(new)) / f"{env['session_id']}.jsonl").exists()
    assert not (projects / env["encoded_a"] / f"{env['session_id']}.jsonl").exists()
    assert (projects / env["encoded_a"] / "sessions-index.json").exists()  # non-empty old dir is kept
    data = json.loads(Path(env["claude_json"]).read_text())
    assert str(new) in data["projects"] and str(old) not in data["projects"]
    assert data["projects"][str(new)]["lastCost"] == 1.25  # project config carried over
    assert data["githubRepoPaths"] == {"me/alpha": [str(new)]}
    assert f'[projects."{new}"]' in config.read_text()
    codex_data.refresh()
    assert [s.cwd for s in codex_data.get_project_sessions(str(new / "src"))] == [str(new / "src")]
    assert r.references == [str(new / "src" / "run.sh")]  # listed, not edited
    assert str(old) in (new / "src" / "run.sh").read_text()


def test_relocate_refuses_bad_destinations(monkeypatch, tmp_path: Path):
    _env, old, new, _rollout, _config = _relocate_env(monkeypatch, tmp_path)
    new.mkdir()
    with pytest.raises(relocate.RelocateError, match="already exists"):
        relocate.plan(str(old), str(new))
    with pytest.raises(relocate.RelocateError, match="inside the source"):
        relocate.plan(str(old), str(old / "sub"))
    with pytest.raises(relocate.RelocateError, match="not a project folder"):
        relocate.plan(str(old / "missing"), str(tmp_path / "x"))


def test_relocate_refuses_while_claude_runs_inside(monkeypatch, tmp_path: Path):
    env, old, new, _rollout, _config = _relocate_env(monkeypatch, tmp_path)
    _register_live_claude(Path(env["claude_dir"]), "some-other-session", str(old / "src"))

    with pytest.raises(LiveSessionError, match="inside the folder"):
        relocate.apply(relocate.plan(str(old), str(new)))
    assert old.exists() and not new.exists()


def test_cli_relocate_dry_run_changes_nothing(monkeypatch, capsys, tmp_path: Path):
    from tests.test_cli import _run

    _env, old, new, _rollout, _config = _relocate_env(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "relocate", str(old), str(new), "--dry-run", "--json")
    assert code == 0
    plan = json.loads(out)
    assert plan["claude_sessions"] == 1 and plan["codex_sessions"] == 1 and plan["dry_run"]
    assert old.exists() and not new.exists()


def test_relocate_removes_emptied_claude_project_dir(monkeypatch, tmp_path: Path):
    env, old, new, _rollout, _config = _relocate_env(monkeypatch, tmp_path)
    old_dir = Path(env["projects_dir"]) / env["encoded_a"]
    (old_dir / "sessions-index.json").unlink()
    for f in (old_dir / "memory").iterdir():
        f.unlink()  # leave memory/ empty, like a fresh Claude project
    assert relocate.apply(relocate.plan(str(old), str(new))).failures == []
    assert not old_dir.exists()
