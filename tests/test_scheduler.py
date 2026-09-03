"""Tests for scheduler.py parsing logic."""

import pytest

from src.scheduler import _SUBSCRIPTION_ONLY_MARKER
from src.scheduler import _USAGE_RE
from src.scheduler import _WINDOW_MAP
from src.scheduler import _is_subscription_only_output
from src.scheduler import _parse_resets_at

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

_OUTPUT_SUBSCRIPTION_ONLY = f"{_SUBSCRIPTION_ONLY_MARKER}\n"


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


@pytest.mark.parametrize("output", [_OUTPUT_COMMA, _OUTPUT_AT], ids=["comma", "at"])
class TestRegex:
    """Both real `claude -p /usage` date formats, and only the quota lines."""

    def test_finds_exactly_the_two_quota_lines(self, output):
        assert len(_parse_output(output)) == 2

    def test_reads_percent_and_reset_for_each_window(self, output):
        by_window = {r["window_type"]: r for r in _parse_output(output)}
        assert by_window["five_hour"]["percent_used"] == 8.0
        assert by_window["five_hour"]["resets_raw"].startswith("Jun 17")
        assert by_window["seven_day"]["percent_used"] == 81.0
        assert by_window["seven_day"]["resets_raw"].startswith("Jun 19")


class TestWindowMap:
    def test_unrecognised_label_maps_to_none(self):
        assert _WINDOW_MAP.get("month (opus only)") is None


class TestSubscriptionOnlyOutput:
    def test_detects_subscription_only_banner(self):
        assert _is_subscription_only_output(_OUTPUT_SUBSCRIPTION_ONLY)

    def test_full_output_is_not_subscription_only(self):
        assert not _is_subscription_only_output(_OUTPUT_COMMA)

    def test_subscription_only_produces_no_records(self):
        assert _parse_output(_OUTPUT_SUBSCRIPTION_ONLY) == []


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
