"""Tests for the records, quota and analytics API."""

from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from src import routes
from src.analytics import estimated_cost_fields
from src.app import create_app
from src.database import session_scope
from src.models import QuotaSnapshot
from src.models import UsageRecord
from src.models import UsageStats
from src.models import format_timestamp


def _record(request_id: str, timestamp: str, **overrides) -> dict:
    base = {
        "request_id": request_id,
        "session_id": "d8e25c22",
        "timestamp": timestamp,
        "cwd": "/Users/mnalavadi/code/projects/ClaudeUsageNotch",
        "git_branch": "main",
        "model": "claude-sonnet-4-6",
        "attribution_skill": "swift",
        "is_sidechain": False,
        "input_tokens": 2,
        "output_tokens": 183,
        "cache_creation_tokens": 7684,
        "cache_read_tokens": 12081,
        "ephemeral_1h_tokens": 7684,
    }
    base.update(overrides)
    return base


def _usage_rows() -> list:
    with session_scope() as session:
        return session.execute(select(UsageRecord).order_by(UsageRecord.timestamp)).scalars().all()


def _quota_rows() -> list[tuple[str, str]]:
    with session_scope() as session:
        rows = session.execute(
            select(QuotaSnapshot.window_type, QuotaSnapshot.timestamp).order_by(QuotaSnapshot.timestamp)
        ).all()
        return [(w, format_timestamp(t)) for w, t in rows]


def _lifetime_cost() -> float:
    with session_scope() as session:
        return session.get(UsageStats, 1).lifetime_cost


def _cost(record: dict) -> float:
    return estimated_cost_fields(
        record["model"],
        record["input_tokens"],
        record["cache_creation_tokens"],
        record["output_tokens"],
        record["cache_read_tokens"],
    )


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def test_health(client):
    resp = client.get("/status")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_oversized_body_is_rejected(client):
    resp = client.post("/api/records", data=b"x" * (5 * 1024 * 1024 + 1), content_type="application/json")
    assert resp.status_code == 413


def test_post_inserts_one_row_per_request(client):
    records = [
        _record("req-a", "2026-06-09T12:00:00.000Z"),
        _record("req-b", "2026-06-09T13:00:00.000Z"),
    ]
    resp = client.post("/api/records", json=records)
    assert resp.status_code == 200
    assert resp.get_json() == {"inserted": 2, "updated": 0, "skipped": 0}
    assert [r.request_id for r in _usage_rows()] == ["req-a", "req-b"]
    assert _lifetime_cost() == pytest.approx(_cost(records[0]) + _cost(records[1]))


def test_post_collapses_lines_of_one_request_keeping_most_output_and_first_time(client):
    partial = _record("req-a", "2026-06-09T12:00:00.000Z", output_tokens=3)
    full = _record("req-a", "2026-06-09T12:00:40.000Z", output_tokens=153)
    assert client.post("/api/records", json=[partial, full]).get_json() == {
        "inserted": 1,
        "updated": 0,
        "skipped": 0,
    }

    [row] = _usage_rows()
    assert row.output_tokens == 153
    assert format_timestamp(row.timestamp) == "2026-06-09T12:00:00.000Z"
    assert _lifetime_cost() == pytest.approx(_cost(full))


def test_later_post_with_more_output_updates_the_request_and_moves_cost_by_the_difference(client):
    partial = _record("req-a", "2026-06-09T12:00:00.000Z", output_tokens=3)
    full = _record("req-a", "2026-06-09T12:00:40.000Z", output_tokens=153)
    client.post("/api/records", json=[partial])

    assert client.post("/api/records", json=[full]).get_json() == {"inserted": 0, "updated": 1, "skipped": 0}
    [row] = _usage_rows()
    assert row.output_tokens == 153
    assert format_timestamp(row.timestamp) == "2026-06-09T12:00:00.000Z"
    assert _lifetime_cost() == pytest.approx(_cost(full))

    # A retry or another machine re-sending the partial line changes nothing.
    assert client.post("/api/records", json=[partial]).get_json() == {
        "inserted": 0,
        "updated": 0,
        "skipped": 1,
    }
    assert _usage_rows()[0].output_tokens == 153
    assert _lifetime_cost() == pytest.approx(_cost(full))


def test_post_skips_records_without_request_id(client):
    resp = client.post("/api/records", json=[_record("", "2026-06-09T12:00:00.000Z"), {"no": "request_id"}])
    assert resp.get_json() == {"inserted": 0, "updated": 0, "skipped": 0}
    assert _usage_rows() == []


def test_post_rejects_non_array(client):
    assert client.post("/api/records", json={"request_id": "x"}).status_code == 400


def test_post_large_batch_stays_under_variable_limit(client):
    # A first sync can push the whole history at once; this must not exceed
    # SQLite's bound-variable ceiling.
    records = [_record(f"req-{i}", "2026-06-09T12:00:00.000Z") for i in range(1500)]
    resp = client.post("/api/records", json=records)
    assert resp.status_code == 200
    assert resp.get_json() == {"inserted": 1500, "updated": 0, "skipped": 0}
    assert len(_usage_rows()) == 1500


# ---------------------------------------------------------------------------
# Quota snapshots endpoint
# ---------------------------------------------------------------------------


def _quota(window_type: str, timestamp: str, **overrides) -> dict:
    base = {
        "window_type": window_type,
        "timestamp": timestamp,
        "percent_used": 0.42,
        "resets_at": "2026-06-16T17:00:00.000Z",
    }
    base.update(overrides)
    return base


def test_quota_post_inserts(client):
    snapshots = [
        _quota("session", "2026-06-16T12:00:00.000Z"),
        _quota("weekly", "2026-06-16T12:00:00.000Z", percent_used=0.1),
    ]
    resp = client.post("/api/quota_snapshots", json=snapshots)
    assert resp.status_code == 200
    assert resp.get_json() == {"inserted": 2, "skipped": 0}
    assert sorted(_quota_rows()) == [
        ("session", "2026-06-16T12:00:00.000Z"),
        ("weekly", "2026-06-16T12:00:00.000Z"),
    ]


def test_quota_post_is_idempotent_by_window_type_and_timestamp(client):
    snap = _quota("session", "2026-06-16T12:00:00.000Z")
    assert client.post("/api/quota_snapshots", json=[snap]).get_json() == {"inserted": 1, "skipped": 0}
    assert client.post("/api/quota_snapshots", json=[snap]).get_json() == {"inserted": 0, "skipped": 1}
    assert len(_quota_rows()) == 1


def test_quota_post_stores_only_changes_and_a_periodic_heartbeat(client):
    """Unchanged readings are dropped until the heartbeat interval keeps the step's right end recent."""
    posts = [
        _quota("session", "2026-06-16T12:00:00.000Z"),
        _quota("session", "2026-06-16T12:01:00.000Z"),
        _quota("session", "2026-06-16T12:02:00.000Z", percent_used=0.5),
        _quota("session", "2026-06-16T12:09:00.000Z", percent_used=0.5),
        _quota("session", "2026-06-16T12:12:00.000Z", percent_used=0.5),
        _quota("weekly", "2026-06-16T12:01:00.000Z"),
    ]
    for snapshot in posts:
        client.post("/api/quota_snapshots", json=[snapshot])

    assert _quota_rows() == [
        ("session", "2026-06-16T12:00:00.000Z"),
        ("weekly", "2026-06-16T12:01:00.000Z"),
        ("session", "2026-06-16T12:02:00.000Z"),
        ("session", "2026-06-16T12:12:00.000Z"),
    ]


def test_quota_post_rejects_non_array(client):
    assert client.post("/api/quota_snapshots", json={"window_type": "session"}).status_code == 400


def test_quota_post_skips_missing_fields(client):
    resp = client.post("/api/quota_snapshots", json=[{"window_type": "session"}])
    assert resp.get_json() == {"inserted": 0, "skipped": 0}


# ---------------------------------------------------------------------------
# Analytics endpoint
# ---------------------------------------------------------------------------

_ANALYTICS_PARAMS = (
    "session_since=2026-06-11T07:00:00.000Z"
    "&session_cost_since=2026-06-11T07:00:00.000Z"
    "&weekly_since=2026-06-05T00:00:00.000Z"
    "&month_since=2026-05-13T00:00:00.000Z"
    "&lookback_since=2026-05-13T00:00:00.000Z"
)


def test_analytics_returns_expected_shape_on_empty_db(client):
    resp = client.get(f"/api/analytics?{_ANALYTICS_PARAMS}")
    assert resp.status_code == 200
    body = resp.get_json()

    for key in (
        "session_cost",
        "today_cost",
        "weekly_cost",
        "month_cost",
        "lifetime_cost",
        "cache_hit_rate",
        "cache_savings_usd",
    ):
        assert key in body

    assert "token_types" in body
    for sub in ("input_tokens", "output_tokens", "input_fraction", "output_fraction"):
        assert sub in body["token_types"]

    assert isinstance(body["model_breakdown"], list)
    assert isinstance(body["project_breakdown"], list)
    assert isinstance(body["skill_breakdown"], list)
    assert isinstance(body["daily_cost"], list)
    assert isinstance(body["daily_sessions"], list)
    # Daily series spans lookback_since -> today (UTC), capped at 30 days.
    lookback_since = date(2026, 5, 13)
    expected_days = min((datetime.now(UTC).date() - lookback_since).days + 1, 30)
    assert len(body["daily_cost"]) == expected_days
    assert len(body["daily_sessions"]) == expected_days

    assert isinstance(body["session_buckets"], list)
    assert isinstance(body["weekly_buckets"], list)
    assert len(body["session_buckets"]) == 24 * 60
    assert len(body["weekly_buckets"]) == 7 * 24
    # No client has pushed a quota reading yet — empty, not absent.
    assert body["session_quota_history"] == []
    assert body["weekly_quota_history"] == []
    assert body["credit_quota_history"] == []


def test_analytics_includes_real_quota_history_within_window(client):
    client.post(
        "/api/quota_snapshots",
        json=[
            _quota("session", "2026-06-11T08:00:00.000Z", percent_used=0.5),  # inside session window
            _quota("session", "2026-06-01T00:00:00.000Z", percent_used=0.9),  # outside session window
            _quota("weekly", "2026-06-06T00:00:00.000Z", percent_used=0.3),  # inside weekly window
            _quota("monthly", "2026-06-01T00:00:00.000Z", percent_used=0.01),  # inside month window
        ],
    )
    body = client.get(f"/api/analytics?{_ANALYTICS_PARAMS}").get_json()

    assert body["session_quota_history"] == [
        {
            "timestamp": "2026-06-11T08:00:00.000Z",
            "percent_used": 0.5,
            "resets_at": "2026-06-16T17:00:00.000Z",
        }
    ]
    assert body["weekly_quota_history"] == [
        {
            "timestamp": "2026-06-06T00:00:00.000Z",
            "percent_used": 0.3,
            "resets_at": "2026-06-16T17:00:00.000Z",
        }
    ]
    assert body["credit_quota_history"] == [
        {
            "timestamp": "2026-06-01T00:00:00.000Z",
            "percent_used": 0.01,
            "resets_at": "2026-06-16T17:00:00.000Z",
        }
    ]


def test_analytics_quota_history_collapses_unchanged_readings(client):
    # The poller samples far faster than the quota moves. A run of identical
    # readings renders identically to its first point (the client holds each value
    # forward), so only transitions plus the final anchor should cross the wire.
    client.post(
        "/api/quota_snapshots",
        json=[
            _quota("session", "2026-06-11T08:00:00.000Z", percent_used=0.5),
            _quota("session", "2026-06-11T08:10:00.000Z", percent_used=0.5),
            _quota("session", "2026-06-11T08:20:00.000Z", percent_used=0.5),
            _quota("session", "2026-06-11T08:30:00.000Z", percent_used=0.7),
            _quota("session", "2026-06-11T08:40:00.000Z", percent_used=0.7),
        ],
    )
    history = client.get(f"/api/analytics?{_ANALYTICS_PARAMS}").get_json()["session_quota_history"]

    # 0.5 keeps its run-START timestamp (the step begins there, not where it ends),
    # 0.7 is the transition, and 08:40 is the final anchor holding the step to the edge.
    assert [(p["timestamp"], p["percent_used"]) for p in history] == [
        ("2026-06-11T08:00:00.000Z", 0.5),
        ("2026-06-11T08:30:00.000Z", 0.7),
        ("2026-06-11T08:40:00.000Z", 0.7),
    ]


def test_analytics_quota_history_preserves_reset_drops(client):
    # A reset is a genuine discontinuity: high -> low with a new resets_at. Both
    # sides of the drop must survive so the sawtooth stays visible.
    client.post(
        "/api/quota_snapshots",
        json=[
            _quota("session", "2026-06-11T08:00:00.000Z", percent_used=0.98),
            _quota("session", "2026-06-11T08:01:00.000Z", percent_used=0.98),
            _quota(
                "session", "2026-06-11T08:02:00.000Z", percent_used=0.03, resets_at="2026-06-16T22:00:00.000Z"
            ),
        ],
    )
    history = client.get(f"/api/analytics?{_ANALYTICS_PARAMS}").get_json()["session_quota_history"]

    assert [p["percent_used"] for p in history] == [0.98, 0.03]
    assert history[0]["resets_at"] == "2026-06-16T17:00:00.000Z"
    assert history[1]["resets_at"] == "2026-06-16T22:00:00.000Z"


def test_analytics_quota_history_quantizes_reset_jitter(client):
    # The provider derives resets_at from poll time, so the same reset arrives with
    # millisecond-level drift on every poll. Those must collapse to ONE reset value,
    # or the client's extractResetTimes (a Set) draws a marker per poll.
    client.post(
        "/api/quota_snapshots",
        json=[
            _quota(
                "session",
                f"2026-06-11T08:0{i}:00.000Z",
                percent_used=0.5,
                resets_at=f"2026-06-16T17:00:00.{i:03d}Z",
            )
            for i in range(5)
        ],
    )
    history = client.get(f"/api/analytics?{_ANALYTICS_PARAMS}").get_json()["session_quota_history"]

    assert len({p["resets_at"] for p in history}) == 1
    assert history[0]["resets_at"] == "2026-06-16T17:00:00.000Z"


def test_analytics_aggregates_costs_correctly(client):
    # Two records: one in session window, one older (weekly only).
    client.post(
        "/api/records",
        json=[
            _record(
                "s1",
                "2026-06-11T08:00:00.000Z",  # within session window
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
            _record(
                "w1",
                "2026-06-09T00:00:00.000Z",  # weekly only
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
        ],
    )

    resp = client.get(f"/api/analytics?{_ANALYTICS_PARAMS}")
    assert resp.status_code == 200
    body = resp.get_json()

    sonnet_input_rate = 3.0  # $3 / 1M tokens
    assert abs(body["session_cost"] - sonnet_input_rate) < 0.001
    assert abs(body["weekly_cost"] - sonnet_input_rate * 2) < 0.001
    assert abs(body["month_cost"] - sonnet_input_rate * 2) < 0.001
    assert abs(body["lifetime_cost"] - sonnet_input_rate * 2) < 0.001


def test_analytics_session_cost_uses_narrower_cutoff_than_session_buckets(client):
    # Regression: "Session" cost must reflect only the actual current 5h session
    # (session_cost_since), not the wide 24h range fetched for session_buckets
    # (session_since) — otherwise Session can exceed Today.
    client.post(
        "/api/records",
        json=[
            _record(
                "in-actual-session",
                "2026-06-11T10:00:00.000Z",  # inside both windows
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
            _record(
                "outside-actual-session",
                "2026-06-11T02:00:00.000Z",  # inside the 24h session_buckets range, but
                model="claude-sonnet-4-6",  # before the real 5h session started
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
        ],
    )
    params = (
        "session_since=2026-06-10T08:00:00.000Z"  # 24h back — covers both records
        "&session_cost_since=2026-06-11T09:00:00.000Z"  # actual session start — covers only the first
        "&weekly_since=2026-06-05T00:00:00.000Z"
        "&month_since=2026-05-13T00:00:00.000Z"
        "&lookback_since=2026-05-13T00:00:00.000Z"
    )
    body = client.get(f"/api/analytics?{params}").get_json()

    sonnet_input_rate = 3.0  # $3 / 1M tokens
    assert abs(body["session_cost"] - sonnet_input_rate) < 0.001  # only the in-session record
    assert len(body["session_buckets"]) == 24 * 60  # buckets still span the full 24h


def test_analytics_breakdowns_span_monthly_window(client):
    # Token/model/project/skill breakdowns are labeled with the lookback period
    # in the UI, so they must aggregate over the monthly window — not a fixed
    # 7-day window. Regression for 7D and 30D reporting identical breakdowns.
    client.post(
        "/api/records",
        json=[
            _record(
                "weekly",
                "2026-06-09T00:00:00.000Z",  # inside the 7-day window
                cwd="/code/ProjWeekly",
                input_tokens=100,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
            _record(
                "monthly-only",
                "2026-05-20T00:00:00.000Z",  # older than weekly, inside monthly
                cwd="/code/ProjMonthly",
                input_tokens=900,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
        ],
    )

    body = client.get(f"/api/analytics?{_ANALYTICS_PARAMS}").get_json()

    # Both records' input tokens are counted, not just the weekly one.
    assert body["token_types"]["input_tokens"] == 1000
    projects = {p["label"] for p in body["project_breakdown"]}
    assert projects == {"ProjWeekly", "ProjMonthly"}


def test_analytics_month_cost_independent_of_lookback(client):
    # "Month" is a fixed trailing-30-day figure; switching the lookback selector
    # (7D / 30D / All) must not change it. Regression for the Month pill tracking
    # the selector instead of a real 30-day window.
    client.post(
        "/api/records",
        json=[
            _record(
                "recent",
                "2026-06-11T00:00:00.000Z",  # inside 7 days
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
            _record(
                "midmonth",
                "2026-05-25T00:00:00.000Z",  # outside 7 days, inside 30
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
        ],
    )
    base = (
        "session_since=2026-06-11T07:00:00.000Z"
        "&session_cost_since=2026-06-11T07:00:00.000Z"
        "&weekly_since=2026-06-05T00:00:00.000Z"
        "&month_since=2026-05-13T00:00:00.000Z"
    )

    def month_cost(lookback_since: str) -> float:
        body = client.get(f"/api/analytics?{base}&lookback_since={lookback_since}").get_json()
        return body["month_cost"]

    week = month_cost("2026-06-05T00:00:00.000Z")
    month = month_cost("2026-05-13T00:00:00.000Z")
    all_time = month_cost("1970-01-01T00:00:00.000Z")

    # Both records fall in the trailing-30-day window → $6 regardless of lookback.
    assert abs(week - 6.0) < 0.001
    assert week == month == all_time


def test_analytics_today_cost_anchored_to_current_date(client):
    # "All" period cuts off at the epoch. today_cost and the day columns must
    # still resolve to the current date, not days starting in 1970. Regression
    # for the Today pill changing across lookback periods.
    now = datetime.now(UTC)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    client.post(
        "/api/records",
        json=[
            _record(
                "today",
                ts,
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            )
        ],
    )

    params = (
        f"session_since={ts}"
        f"&session_cost_since={ts}"
        f"&weekly_since={ts}"
        "&month_since=1970-01-01T00:00:00.000Z"
        "&lookback_since=1970-01-01T00:00:00.000Z"
    )
    body = client.get(f"/api/analytics?{params}").get_json()

    assert abs(body["today_cost"] - 3.0) < 0.001  # 1M sonnet input @ $3/M
    # Default ("day") granularity: last 30 daily buckets, last one is today.
    assert len(body["daily_cost"]) == 30  # last 30 days, capped
    assert body["daily_cost"][-1]["date"].startswith(str(now.date()))


def test_analytics_hour_granularity_returns_24_buckets(client):
    # 1D view: spend/sessions roll up per hour over the last 24 hours.
    now = datetime.now(UTC)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    client.post(
        "/api/records",
        json=[
            _record(
                "h1",
                ts,
                model="claude-sonnet-4-6",
                input_tokens=1_000_000,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            )
        ],
    )
    params = (
        f"session_since={ts}&session_cost_since={ts}&weekly_since={ts}&month_since={ts}"
        f"&lookback_since={ts}&granularity=hour"
    )
    body = client.get(f"/api/analytics?{params}").get_json()

    assert len(body["daily_cost"]) == 24
    assert len(body["daily_sessions"]) == 24
    # The current hour's bucket holds the one record's spend.
    assert abs(body["daily_cost"][-1]["value"] - 3.0) < 0.001
    assert body["daily_sessions"][-1]["value"] == 1


def test_analytics_month_granularity_spans_record_months(client):
    # "All" view: spend/sessions roll up per calendar month from the first record.
    now = datetime.now(UTC)
    two_months_ago = (now.replace(day=1) - timedelta(days=40)).strftime("%Y-%m-%dT12:00:00.000Z")
    client.post(
        "/api/records",
        json=[_record("m1", two_months_ago, session_id="s-old")],
    )
    params = (
        f"session_since={now:%Y-%m-%dT%H:%M:%S.000Z}"
        f"&session_cost_since={now:%Y-%m-%dT%H:%M:%S.000Z}"
        f"&weekly_since={now:%Y-%m-%dT%H:%M:%S.000Z}"
        "&month_since=1970-01-01T00:00:00.000Z"
        "&lookback_since=1970-01-01T00:00:00.000Z"
        "&granularity=month"
    )
    body = client.get(f"/api/analytics?{params}").get_json()

    # Monthly buckets from the record's month through the current month: >= 3.
    assert len(body["daily_cost"]) >= 3
    assert all("T00:00:00" in d["date"] for d in body["daily_cost"])


def test_analytics_rejects_invalid_granularity(client):
    params = f"{_ANALYTICS_PARAMS}&granularity=week"
    assert client.get(f"/api/analytics?{params}").status_code == 400


def test_analytics_missing_params_returns_400(client):
    assert client.get("/api/analytics").status_code == 400
    assert client.get("/api/analytics?session_since=2026-06-11T07:00:00.000Z").status_code == 400


def test_analytics_rejects_invalid_timestamp(client):
    bad = (
        "session_since=not-a-timestamp"
        "&session_cost_since=2026-06-11T07:00:00.000Z"
        "&weekly_since=2026-06-05T00:00:00.000Z"
        "&month_since=2026-05-13T00:00:00.000Z"
        "&lookback_since=2026-05-13T00:00:00.000Z"
    )
    assert client.get(f"/api/analytics?{bad}").status_code == 400


def test_analytics_bucket_tokens_sum_to_total(client):
    client.post(
        "/api/records",
        json=[
            _record(
                "b1", "2026-06-11T08:30:00.000Z", input_tokens=100, output_tokens=50, cache_creation_tokens=25
            ),
            _record(
                "b2", "2026-06-11T08:45:00.000Z", input_tokens=200, output_tokens=100, cache_creation_tokens=0
            ),
        ],
    )

    resp = client.get(f"/api/analytics?{_ANALYTICS_PARAMS}")
    body = resp.get_json()
    bucket_total = sum(b["tokens"] for b in body["session_buckets"])
    # totalTokens = input + output + cacheCreate (matches Swift's totalTokens)
    expected = (100 + 50 + 25) + (200 + 100 + 0)
    assert bucket_total == expected


def test_post_records_tags_metrics_with_device_and_counts_rejected(client, monkeypatch):
    metrics = Mock()
    monkeypatch.setattr(routes, "metrics", metrics)
    headers = {"X-Device-Name": "Mohit%E2%80%99s%20Air"}

    client.post(
        "/api/records",
        json=[_record("req-a", "2026-06-09T12:00:00.000Z"), {"no": "request_id"}],
        headers=headers,
    )
    client.post("/api/records", json={"not": "an array"}, headers=headers)

    metrics.increment.assert_any_call("records_inserted", 1, tags={"device": "Mohit’s Air"})
    metrics.increment.assert_any_call(
        "payload_rejected", 1, tags={"device": "Mohit’s Air", "reason": "missing_request_id"}
    )
    metrics.increment.assert_any_call(
        "payload_rejected", tags={"device": "Mohit’s Air", "reason": "not_array"}
    )
