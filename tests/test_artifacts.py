"""Tests for the artifacts service (published-page discovery in session JSONLs)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from asm.services import artifacts


def _artifact_lines(tool_id: str, url: str, inp: dict, ts: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {"content": [
                {"type": "tool_use", "id": tool_id, "name": "Artifact", "input": inp},
            ]},
        },
        {
            "type": "user",
            "timestamp": ts,
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": tool_id,
                 "content": f"Published {inp.get('file_path', '?')} at {url}\n\nTo update: republish."},
            ]},
        },
    ]


def _write_session(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_list_artifacts_finds_and_dedupes(tmp_path: Path):
    projects = tmp_path / "projects"
    rows = _artifact_lines(
        "t1", "https://claude.ai/code/artifact/aaa-111",
        {"file_path": "/tmp/report.html", "title": "Audit Report", "favicon": "🧭"},
        "2026-07-01T10:00:00Z",
    )
    # Same URL republished later with a new description — latest wins.
    rows += _artifact_lines(
        "t2", "https://claude.ai/code/artifact/aaa-111",
        {"file_path": "/tmp/report.html", "description": "v2 of the report"},
        "2026-07-02T10:00:00Z",
    )
    # A list call (no publish) produces no URL-bearing result and is skipped.
    rows += [
        {
            "type": "assistant", "timestamp": "2026-07-03T10:00:00Z",
            "message": {"content": [
                {"type": "tool_use", "id": "t3", "name": "Artifact", "input": {"action": "list"}},
            ]},
        },
        {
            "type": "user", "timestamp": "2026-07-03T10:00:00Z",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t3", "content": "2 artifacts: ..."},
            ]},
        },
    ]
    _write_session(projects / "-home-me-proj" / "sess-1.jsonl", rows)
    _write_session(
        projects / "-home-me-other" / "sess-2.jsonl",
        _artifact_lines(
            "t9", "https://claude.ai/code/artifact/bbb-222",
            {"file_path": "/tmp/dash.html", "label": "cost-dash"},
            "2026-07-04T09:00:00Z",
        ),
    )

    with patch.object(artifacts, "PROJECTS_DIR", projects):
        items = artifacts.list_artifacts()

    assert [a.url for a in items] == [
        "https://claude.ai/code/artifact/bbb-222",
        "https://claude.ai/code/artifact/aaa-111",
    ]
    dash, report = items
    assert dash.title == "cost-dash"
    assert dash.project_dir == "-home-me-other"
    assert dash.session_id == "sess-2"
    # Redeploy of the same URL keeps one entry with the latest metadata.
    assert report.description == "v2 of the report"
    assert report.published > 0


def test_list_artifacts_empty_tree(tmp_path: Path):
    with patch.object(artifacts, "PROJECTS_DIR", tmp_path / "none"):
        assert artifacts.list_artifacts() == []


def test_replayed_request_blob_does_not_duplicate_or_crash(tmp_path: Path):
    """Claude Code's api-request-blob records repeat earlier turns verbatim."""
    projects = tmp_path / "projects"
    rows = _artifact_lines(
        "t1", "https://claude.ai/code/artifact/aaa-111", {"title": "Report"}, "2026-07-01T10:00:00Z"
    )
    blob = {"type": "api-request-blob", "message": rows[0]["message"]}
    _write_session(projects / "-home-me-proj" / "sess-1.jsonl", [rows[0], blob, rows[1]])

    with patch.object(artifacts, "PROJECTS_DIR", projects):
        assert [a.title for a in artifacts.list_artifacts()] == ["Report"]
