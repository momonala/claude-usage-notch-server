"""Tests for the in-process backfill."""

import json

import src.backfill as backfill_module
from src.backfill import backfill


def _assistant_line(uuid: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": "2026-10-19T09:00:00Z",
            "sessionId": "s1",
            "cwd": "/home/me/proj",
            "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 5}},
        }
    )


def test_inserts_new_records_once(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    lines = [_assistant_line("a"), _assistant_line("b"), _assistant_line("a"), '{"type": "user"}', "not json"]
    (project / "session.jsonl").write_text("\n".join(lines))
    monkeypatch.setattr(backfill_module, "_CLAUDE_DIR", tmp_path)

    assert backfill() == 2
    assert backfill() == 0
