"""Tests for scheduler.py parsing logic."""

import pytest

from src.scheduler import _USAGE_RE, _WINDOW_MAP, _parse_resets_at

# Two real output variants from `claude -p /usage`
_OUTPUT_COMMA = """\
You are currently using your subscription to power your Claude Code usage

Current session: 8% used · resets Jun 17, 12:39pm (Europe/Berlin)
Current week (all models): 81% used · resets Jun 19, 10:59pm (Europe/Berlin)
"""

_OUTPUT_AT = """\
You are currently using your subscription to power your Claude Code usage

Current session: 8% used · resets Jun 17 at 12:39pm (Europe/Berlin)
Current week (all models): 81% used · resets Jun 19 at 10:59pm (Europe/Berlin)

What's contributing to your limits usage?
Approximate, based on local sessions on this machine — does not include other devices or claude.ai.

Last 24h · 305 requests · 11 sessions
  37% of your usage was at >150k context
"""


def _parse_output(text: str) -> list[dict]:
    matches = list(_USAGE_RE.finditer(text))
    return [
        {
            "window_type": _WINDOW_MAP.get(m.group("label").strip().lower()),
            "percent_used": float(m.group("pct")),
            "resets_raw": m.group("resets").strip(),
        }
        for m in matches
    ]


class TestRegex:
    def test_comma_format_finds_two_lines(self):
        records = _parse_output(_OUTPUT_COMMA)
        assert len(records) == 2

    def test_at_format_finds_two_lines(self):
        records = _parse_output(_OUTPUT_AT)
        assert len(records) == 2

    def test_comma_format_session(self):
        records = _parse_output(_OUTPUT_COMMA)
        session = next(r for r in records if r["window_type"] == "five_hour")
        assert session["percent_used"] == 8.0
        assert session["resets_raw"] == "Jun 17, 12:39pm"

    def test_comma_format_week(self):
        records = _parse_output(_OUTPUT_COMMA)
        week = next(r for r in records if r["window_type"] == "seven_day")
        assert week["percent_used"] == 81.0
        assert week["resets_raw"] == "Jun 19, 10:59pm"

    def test_at_format_session(self):
        records = _parse_output(_OUTPUT_AT)
        session = next(r for r in records if r["window_type"] == "five_hour")
        assert session["percent_used"] == 8.0
        assert session["resets_raw"] == "Jun 17 at 12:39pm"

    def test_at_format_week(self):
        records = _parse_output(_OUTPUT_AT)
        week = next(r for r in records if r["window_type"] == "seven_day")
        assert week["percent_used"] == 81.0
        assert week["resets_raw"] == "Jun 19 at 10:59pm"

    def test_unknown_label_maps_to_none(self):
        records = _parse_output(_OUTPUT_COMMA)
        assert all(r["window_type"] is not None for r in records)

    def test_extra_content_does_not_produce_spurious_matches(self):
        records = _parse_output(_OUTPUT_AT)
        assert len(records) == 2


class TestParseResetsAt:
    @pytest.mark.parametrize(
        "raw,expected_suffix",
        [
            ("Jun 17, 12:39pm", "T12:39:00+00:00"),
            ("Jun 17 at 12:39pm", "T12:39:00+00:00"),
            ("Jun 19, 10:59pm", "T22:59:00+00:00"),
            ("Jun 19 at 10:59pm", "T22:59:00+00:00"),
        ],
    )
    def test_parses_time_correctly(self, raw, expected_suffix):
        result = _parse_resets_at(raw)
        assert result is not None
        assert result.endswith(expected_suffix), f"{raw!r} → {result!r}"

    def test_returns_none_for_garbage(self):
        assert _parse_resets_at("not a date") is None

    def test_strips_whitespace(self):
        assert _parse_resets_at("  Jun 17, 12:39pm  ") is not None
