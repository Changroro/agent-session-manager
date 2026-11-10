"""Tests for full-text session body search."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import asm.models as models
from asm.services import search


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    projects = tmp_path / "projects" / "-home-bch-test"
    projects.mkdir(parents=True)
    # 'warp 한글' appears only mid-conversation, never in the first message,
    # so a title-only search would miss it but a body search must find it.
    (projects / "aaaa-1111.jsonl").write_text(
        '{"type":"user","message":{"content":"첫 질문입니다"}}\n'
        '{"type":"assistant","message":{"content":"warp 한글 패치 얘기"}}\n',
        encoding="utf-8",
    )
    (projects / "bbbb-2222.jsonl").write_text(
        '{"type":"user","message":{"content":"전혀 관련 없는 세션"}}\n',
        encoding="utf-8",
    )
    codex = tmp_path / "codex-sessions" / "2026" / "06" / "26"
    codex.mkdir(parents=True)
    (codex / "rollout-2026-06-26T10-00-00-cccc.jsonl").write_text(
        '{"type":"session_meta","payload":{"id":"cccc"}}\n'
        '{"payload":{"content":[{"type":"text","text":"warp terminal 한글"}]}}\n',
        encoding="utf-8",
    )
    return tmp_path / "projects", tmp_path / "codex-sessions"


def test_claude_body_search_finds_mid_conversation(tmp_path):
    projects_dir, codex_dir = _setup(tmp_path)
    with patch.object(models, "PROJECTS_DIR", projects_dir), \
         patch.object(models, "CODEX_SESSIONS_DIR", codex_dir):
        importlib.reload(search)
        assert search.search_claude_session_ids("warp") == {"aaaa-1111"}
        # Korean term living only in the body is still matched.
        assert search.search_claude_session_ids("한글 패치") == {"aaaa-1111"}
        # Non-matching query returns nothing.
        assert search.search_claude_session_ids("존재하지않는단어") == set()
    importlib.reload(search)


def test_search_ignores_whitespace(tmp_path):
    """Body has 'warp 한글'; both spaced and unspaced queries find it."""
    projects_dir, codex_dir = _setup(tmp_path)
    with patch.object(models, "PROJECTS_DIR", projects_dir), \
         patch.object(models, "CODEX_SESSIONS_DIR", codex_dir):
        importlib.reload(search)
        spaced = search.search_claude_session_ids("warp 한글")
        unspaced = search.search_claude_session_ids("warp한글")
        assert spaced == unspaced == {"aaaa-1111"}
        # Same for the Python fallback (ripgrep forced off).
        with patch("asm.services.search.shutil.which", return_value=None):
            assert search.search_claude_session_ids("warp한글") == {"aaaa-1111"}
    importlib.reload(search)


def test_normalize():
    assert search.normalize("Warp 한글") == search.normalize("warp한글")
    assert search.normalize("  A  B ") == "ab"


def test_codex_body_search_returns_rollout_path(tmp_path):
    projects_dir, codex_dir = _setup(tmp_path)
    with patch.object(models, "PROJECTS_DIR", projects_dir), \
         patch.object(models, "CODEX_SESSIONS_DIR", codex_dir):
        importlib.reload(search)
        paths = search.search_codex_rollout_paths("한글")
        assert len(paths) == 1
        assert Path(next(iter(paths))).name == "rollout-2026-06-26T10-00-00-cccc.jsonl"
    importlib.reload(search)


def test_blank_query_is_empty(tmp_path):
    projects_dir, codex_dir = _setup(tmp_path)
    with patch.object(models, "PROJECTS_DIR", projects_dir), \
         patch.object(models, "CODEX_SESSIONS_DIR", codex_dir):
        importlib.reload(search)
        assert search.search_claude_session_ids("   ") == set()
        assert search.search_codex_rollout_paths("") == set()
    importlib.reload(search)


def test_python_fallback_matches_ripgrep(tmp_path):
    """With ripgrep forced unavailable, the Python scan finds the same hits."""
    projects_dir, codex_dir = _setup(tmp_path)
    with patch.object(models, "PROJECTS_DIR", projects_dir), \
         patch.object(models, "CODEX_SESSIONS_DIR", codex_dir), \
         patch("asm.services.search.shutil.which", return_value=None):
        importlib.reload(search)
        assert search.search_claude_session_ids("warp") == {"aaaa-1111"}
        assert len(search.search_codex_rollout_paths("한글")) == 1
    importlib.reload(search)


def test_subagent_hit_maps_to_parent_session(tmp_path):
    projects_dir, codex_dir = _setup(tmp_path)
    sub = projects_dir / "-home-bch-test" / "bbbb-2222" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text('{"type":"assistant","message":{"content":"needle only here"}}\n')
    with patch.object(models, "PROJECTS_DIR", projects_dir), \
         patch.object(models, "CODEX_SESSIONS_DIR", codex_dir):
        importlib.reload(search)
        assert search.search_claude_session_ids("needle") == {"bbbb-2222"}
    importlib.reload(search)


def test_cancelled_scan_stops_and_kills_ripgrep(tmp_path):
    projects_dir, codex_dir = _setup(tmp_path)
    for i in range(50):
        (projects_dir / "-home-bch-test" / f"s{i}.jsonl").write_text('{"x":"hit"}\n')
    with patch.object(models, "PROJECTS_DIR", projects_dir), \
         patch.object(models, "CODEX_SESSIONS_DIR", codex_dir):
        importlib.reload(search)
        got = []
        for sid in search.iter_claude_session_ids("hit", cancelled=lambda: len(got) >= 3):
            got.append(sid)
        assert len(got) == 3
    importlib.reload(search)


def test_unregistered_project_dir_is_listed_and_not_orphaned(monkeypatch, tmp_path):
    from asm.services import claude_data
    from tests.test_feature_smoke import _setup_fake_claude

    env = _setup_fake_claude(monkeypatch, tmp_path)
    moved_to = Path(env["home"]) / "work" / "new-home"
    moved_to.mkdir(parents=True)
    d = Path(env["projects_dir"]) / claude_data.encode_path(str(moved_to))
    d.mkdir()
    (d / "s9.jsonl").write_text(
        '{"type":"user","cwd":"%s","message":{"content":"moved session"}}\n' % moved_to
    )

    assert str(moved_to) in {p.path for p in claude_data.get_projects()}
    assert [s.summary for s in claude_data.get_project_sessions(str(moved_to))] == ["moved session"]
    orphaned = {s.dir_name for s in claude_data.get_sessions() if s.is_orphaned}
    assert d.name not in orphaned


def test_session_titles_are_reused_until_the_file_changes(monkeypatch, tmp_path):
    from asm.services import claude_data
    from tests.test_feature_smoke import _setup_fake_claude

    env = _setup_fake_claude(monkeypatch, tmp_path)
    monkeypatch.setattr(claude_data, "_title_cache", None)
    calls = []
    real = claude_data._scan_jsonl_summary
    monkeypatch.setattr(claude_data, "_scan_jsonl_summary", lambda f: calls.append(f) or real(f))

    claude_data.get_project_sessions(env["project_b"])
    monkeypatch.setattr(claude_data, "_title_cache", None)  # as in a fresh process: reload from the ledger
    claude_data.get_project_sessions(env["project_b"])
    assert len(calls) == 1

    jsonl = Path(env["projects_dir"]) / env["encoded_b"] / f"{env['other_session_id']}.jsonl"
    jsonl.write_text('{"type":"user","message":{"content":"edited prompt here"}}\n')
    assert claude_data.get_project_sessions(env["project_b"])[0].summary == "edited prompt here"
