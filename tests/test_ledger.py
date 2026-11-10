"""Tests for the persistent usage ledger (incremental, frozen prices, survives deletion)."""

from __future__ import annotations

import json
from pathlib import Path

from asm.services import claude_data, codex_data, ledger, pricing
from tests.test_codex_data import _write_rollout_real_layout


def _write_claude_session(path: Path, msg_id: str, model: str, tokens: int, ts: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"id": msg_id, "model": model,
                    "usage": {"input_tokens": tokens, "output_tokens": 0}},
    }) + "\n")


def _rates(monkeypatch, input_cost: float) -> None:
    monkeypatch.setattr(pricing, "_live_db", {
        "claude-fable-5": {"input_cost_per_token": input_cost, "output_cost_per_token": 1e-4},
    })


def test_incremental_skips_unchanged_files(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_data, "PROJECTS_DIR", projects)
    _write_claude_session(projects / "-p" / "s1.jsonl", "m1", "claude-fable-5", 1_000_000, "2026-07-01T10:00:00Z")
    assert ledger.update_claude() == 1
    assert ledger.update_claude() == 0  # nothing changed → nothing re-parsed
    # Appending to the file (mtime/size change) re-ingests just that file.
    f = projects / "-p" / "s1.jsonl"
    f.write_text(f.read_text() + json.dumps({
        "type": "assistant", "timestamp": "2026-07-01T11:00:00Z",
        "message": {"id": "m2", "model": "claude-fable-5",
                    "usage": {"input_tokens": 500, "output_tokens": 0}},
    }) + "\n")
    assert ledger.update_claude() == 1
    assert set(ledger.claude_records()) == {"m1", "m2"}


def test_costs_frozen_at_scan_time(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_data, "PROJECTS_DIR", projects)
    _rates(monkeypatch, 1e-5)  # $10/M when the old session is scanned
    _write_claude_session(projects / "-p" / "old.jsonl", "m-old", "claude-fable-5", 1_000_000, "2026-07-01T10:00:00Z")
    ledger.update_claude()
    _rates(monkeypatch, 3e-5)  # price triples afterwards
    _write_claude_session(projects / "-p" / "new.jsonl", "m-new", "claude-fable-5", 1_000_000, "2026-07-02T10:00:00Z")
    ledger.update_claude()
    recs = ledger.claude_records()
    assert round(recs["m-old"]["cost"], 2) == 10.0  # old valuation kept
    assert round(recs["m-new"]["cost"], 2) == 30.0  # new session at new rates


def test_records_survive_file_deletion(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_data, "PROJECTS_DIR", projects)
    f = projects / "-p" / "gone.jsonl"
    _write_claude_session(f, "m-gone", "claude-fable-5", 1_000_000, "2026-07-01T10:00:00Z")
    ledger.update_claude()
    f.unlink()
    ledger.update_claude()
    recs = ledger.claude_records()
    assert "m-gone" in recs  # cost history outlives the transcript
    claude_data.refresh_usage_cache()
    assert claude_data.get_usage_data()["total_cost"] > 0


def test_codex_ledger_full_history_and_missing_files(monkeypatch, tmp_path: Path):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(codex_data, "CODEX_SESSIONS_DIR", sessions)
    codex_data.refresh()
    a = sessions / "2026" / "06" / "01" / "rollout-2026-06-01T09-00-00-aaaa.jsonl"
    b = sessions / "2026" / "06" / "02" / "rollout-2026-06-02T09-00-00-bbbb.jsonl"
    u = {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 1_000_000}
    _write_rollout_real_layout(a, "aaaa", "/w/a", ["gpt-5.5"], u)
    _write_rollout_real_layout(b, "bbbb", "/w/b", ["gpt-5.5"], u)
    assert ledger.update_codex() == 2
    a.unlink()
    codex_data.refresh()
    usage = codex_data.get_usage_data()
    # Deleted rollout still counted in cost history…
    assert round(usage["total_cost"], 2) == 10.0
    assert usage["total_sessions_ever"] == 2
    # …but no longer listed as a browsable session.
    assert codex_data.get_project_sessions("/w/a") == []
    assert len(codex_data.get_project_sessions("/w/b")) == 1


def test_unchanged_files_are_not_rehashed(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_data, "PROJECTS_DIR", projects)
    _write_claude_session(projects / "-p" / "s1.jsonl", "m1", "claude-fable-5", 10, "2026-07-01T10:00:00Z")
    ledger.update_claude()
    hashed = []
    real = ledger._fingerprint
    monkeypatch.setattr(ledger, "_fingerprint", lambda p: hashed.append(p) or real(p))
    assert ledger.update_claude() == 0
    assert hashed == []  # a refresh must not re-read every transcript


def test_non_anthropic_model_is_kept_without_crashing(monkeypatch, tmp_path: Path):
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_data, "PROJECTS_DIR", projects)
    monkeypatch.setattr(pricing, "_live_db", {})
    _write_claude_session(projects / "-p" / "s1.jsonl", "m1", "zai-glm-5-3-flash", 100, "2026-07-01T10:00:00Z")
    assert ledger.update_claude() == 1
    rec = ledger.claude_records()["m1"]
    assert rec["model"] == "zai-glm-5-3-flash" and rec["cost"] == 0.0
