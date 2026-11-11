"""Tests for the in-process backfill."""

import json

import src.backfill as backfill_module
from src.backfill import backfill


def _assistant_line(request_id: str, output_tokens: int = 5) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "requestId": request_id,
            "timestamp": "2026-10-19T09:00:00Z",
            "sessionId": "s1",
            "cwd": "/home/me/proj",
            "message": {
                "model": "claude-opus-5-5",
                "usage": {"input_tokens": 10, "output_tokens": output_tokens},
            },
        }
    )


def test_inserts_one_row_per_request_once(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    lines = [
        _assistant_line("a", output_tokens=1),
        _assistant_line("b"),
        _assistant_line("a", output_tokens=40),
        '{"type": "assistant", "timestamp": "2026-10-19T09:00:00Z"}',
        '{"type": "user"}',
        "not json",
    ]
    (project / "session.jsonl").write_text("\n".join(lines))
    monkeypatch.setattr(backfill_module, "_CLAUDE_DIR", tmp_path)

    assert backfill() == 2
    assert backfill() == 0
