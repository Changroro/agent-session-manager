"""E2E: remaining CLI subcommand paths, each exercised at least once."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cli import _run
from tests.test_codex_data import _write_rollout_real_layout
from tests.test_feature_smoke import _fake_send2trash, _setup_fake_claude


def test_cli_backup_restore_and_delete(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    cfg = Path(env["claude_json"])
    original = cfg.read_text()

    code, out = _run(monkeypatch, capsys, "backup", "create", "--type", "config")
    assert code == 0
    backup_path = out.strip()

    cfg.write_text('{"projects": {}}')  # damage the live config
    code, _ = _run(monkeypatch, capsys, "backup", "restore", backup_path, "--yes")
    assert code == 0
    assert json.loads(cfg.read_text()) == json.loads(original)

    code, _ = _run(monkeypatch, capsys, "backup", "delete", backup_path, "--yes")
    assert code == 0
    assert not Path(backup_path).exists()


def test_cli_backup_export_import_roundtrip(monkeypatch, capsys, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "backup", "create", "--type", "config")
    assert code == 0
    backup_path = out.strip()

    dest = tmp_path / "exports"
    code, out = _run(monkeypatch, capsys, "backup", "export", backup_path, "--dest", str(dest))
    assert code == 0
    archive = out.strip()
    assert archive.endswith(".tar.gz") and Path(archive).exists()

    code, out = _run(monkeypatch, capsys, "backup", "import", archive)
    assert code == 0
    assert Path(out.strip()).exists()


def test_cli_recovery_list_restore_delete(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    from asm.services import recovery
    monkeypatch.setattr(recovery, "CLAUDE_DIR", Path(env["claude_dir"]))
    from asm.services import codex_data as _codex_data

    monkeypatch.setattr(_codex_data, "CODEX_SESSIONS_DIR", tmp_path / "no-codex" / "sessions")
    monkeypatch.setattr(recovery, "RECOVERY_BASE_DIR", tmp_path / "recovery")
    monkeypatch.setattr(recovery, "send2trash", _fake_send2trash)

    jsonl = Path(env["projects_dir"]) / env["encoded_a"] / f"{env['session_id']}.jsonl"
    code, _ = _run(monkeypatch, capsys, "trash", env["session_id"], "--yes")
    assert code == 0 and not jsonl.exists()

    code, out = _run(monkeypatch, capsys, "recovery", "list", "--json")
    assert code == 0
    items = json.loads(out)
    assert len(items) == 1
    rid = items[0]["id"]

    code, _ = _run(monkeypatch, capsys, "recovery", "restore", rid, "--yes")
    assert code == 0 and jsonl.exists()

    code, _ = _run(monkeypatch, capsys, "recovery", "delete", rid, "--yes")
    assert code == 0
    code, out = _run(monkeypatch, capsys, "recovery", "list", "--json")
    assert json.loads(out) == []


def test_cli_cost_source_and_period_filters(monkeypatch, capsys, tmp_path: Path):
    _setup_fake_claude(monkeypatch, tmp_path)
    from asm.services import codex_data
    codex_root = tmp_path / "codex-sessions"
    _write_rollout_real_layout(
        codex_root / "2026" / "07" / "01" / "rollout-2026-07-01T09-00-00-cst1.jsonl",
        "cst1", "/work/cst", ["gpt-5.5"],
        {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 1_000_000},
    )
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", codex_root)
    codex_data.refresh()

    code, out = _run(monkeypatch, capsys, "cost", "--source", "codex", "--period", "monthly", "--json")
    assert code == 0
    data = json.loads(out)
    assert "codex" in data and "claude" not in data
    assert data["codex"]["monthly"], "monthly rows expected"

    code, out = _run(monkeypatch, capsys, "cost", "--source", "claude", "--json")
    assert code == 0
    data = json.loads(out)
    assert "claude" in data and "codex" not in data


def test_cli_sessions_project_filter_and_preview_json(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    code, out = _run(monkeypatch, capsys, "find", "--project", env["project_a"], "--json")
    assert code == 0
    rows = json.loads(out)
    assert {r["session_id"] for r in rows} == {env["session_id"]}

    code, out = _run(monkeypatch, capsys, "preview", env["session_id"], "--json")
    assert code == 0
    msgs = json.loads(out)
    assert any(m["type"] == "user" for m in msgs)
    assert any("alpha" in m.get("content", "") for m in msgs)


def test_cli_clean_orphaned(monkeypatch, capsys, tmp_path: Path):
    env = _setup_fake_claude(monkeypatch, tmp_path)
    orphan_dir = Path(env["projects_dir"]) / "-home-me-orphan"
    orphan_dir.mkdir()
    (orphan_dir / "dead.jsonl").write_text("{}\n")

    code, out = _run(monkeypatch, capsys, "clean", "orphaned", "--dry-run")
    assert code == 0
    assert "-home-me-orphan" in out and orphan_dir.exists()

    code, _ = _run(monkeypatch, capsys, "clean", "orphaned", "--yes")
    assert code == 0
    assert not orphan_dir.exists()


def test_legacy_data_dir_migration(monkeypatch, tmp_path: Path):
    from asm import models
    legacy = tmp_path / ".cc-tui"
    (legacy / "backups").mkdir(parents=True)
    (legacy / "trash-log.jsonl").write_text('{"old": true}\n')
    new_dir = tmp_path / "new-asm"
    monkeypatch.setattr(models, "APP_DATA_DIR", new_dir)
    monkeypatch.setattr(models, "LEGACY_APP_DATA_DIRS", [legacy])

    assert models.migrate_legacy_data_dir() is True
    assert (new_dir / "trash-log.jsonl").read_text() == '{"old": true}\n'
    assert (new_dir / "backups").is_dir()
    assert not legacy.exists()
    # Idempotent: nothing left to migrate on the next startup.
    assert models.migrate_legacy_data_dir() is False
