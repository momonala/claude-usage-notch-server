"""Tests for the records API: health, upsert idempotency, and time filtering."""

from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from src.app import create_app


def _record(uuid: str, timestamp: str, **overrides) -> dict:
    base = {
        "uuid": uuid,
        "request_id": "req_011C",
        "session_id": "d8e25c22",
        "parent_uuid": None,
        "timestamp": timestamp,
        "cwd": "/Users/mnalavadi/code/projects/ClaudeUsageNotch",
        "project": "ClaudeUsageNotch",
        "git_branch": "main",
        "model": "claude-sonnet-4-6",
        "version": "2.1.169",
        "entrypoint": "cli",
        "attribution_skill": "swift",
        "is_sidechain": False,
        "stop_reason": "end_turn",
        "service_tier": "standard",
        "input_tokens": 2,
        "output_tokens": 183,
        "cache_creation_tokens": 7684,
        "cache_read_tokens": 12081,
        "ephemeral_1h_tokens": 7684,
        "ephemeral_5m_tokens": 0,
        "web_searches": 0,
        "web_fetches": 0,
    }
    base.update(overrides)
    return base


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


def test_post_inserts_and_get_returns(client):
    records = [
        _record("uuid-a", "2026-06-09T12:00:00.000Z"),
        _record("uuid-b", "2026-06-09T13:00:00.000Z"),
    ]
    resp = client.post("/api/records", json=records)
    assert resp.status_code == 200
    assert resp.get_json() == {"inserted": 2, "skipped": 0}

    resp = client.get("/api/records")
    body = resp.get_json()
    assert len(body) == 2
    assert {r["uuid"] for r in body} == {"uuid-a", "uuid-b"}
    # Round-trips a representative field and the timestamp format.
    assert body[0]["timestamp"] == "2026-06-09T12:00:00.000Z"
    assert body[0]["cache_creation_tokens"] == 7684


def test_post_is_idempotent_by_uuid(client):
    rec = _record("uuid-dup", "2026-06-09T12:00:00.000Z")
    assert client.post("/api/records", json=[rec]).get_json() == {"inserted": 1, "skipped": 0}
    # Re-posting the same uuid is a no-op.
    assert client.post("/api/records", json=[rec]).get_json() == {"inserted": 0, "skipped": 1}
    assert len(client.get("/api/records").get_json()) == 1


def test_get_since_filters_by_timestamp(client):
    client.post(
        "/api/records",
        json=[
            _record("old", "2026-06-01T00:00:00.000Z"),
            _record("new", "2026-06-10T00:00:00.000Z"),
        ],
    )
    body = client.get("/api/records?since=2026-06-05T00:00:00Z").get_json()
    assert [r["uuid"] for r in body] == ["new"]


def test_post_rejects_non_array(client):
    assert client.post("/api/records", json={"uuid": "x"}).status_code == 400


def test_get_rejects_invalid_since(client):
    assert client.get("/api/records?since=not-a-timestamp").status_code == 400


def test_post_large_batch_stays_under_variable_limit(client):
    # A first sync can push the whole history at once; this must not exceed
    # SQLite's bound-variable ceiling (regression test for the chunked upsert).
    records = [_record(f"uuid-{i}", "2026-06-09T12:00:00.000Z") for i in range(1500)]
    resp = client.post("/api/records", json=records)
    assert resp.status_code == 200
    assert resp.get_json() == {"inserted": 1500, "skipped": 0}
    assert len(client.get("/api/records").get_json()) == 1500


# ---------------------------------------------------------------------------
# Analytics endpoint
# ---------------------------------------------------------------------------

_ANALYTICS_PARAMS = (
    "session_since=2026-06-11T07:00:00.000Z"
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
        "total_web_searches",
        "total_web_fetches",
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
    expected_days = min((datetime.now(timezone.utc).date() - lookback_since).days + 1, 30)
    assert len(body["daily_cost"]) == expected_days
    assert len(body["daily_sessions"]) == expected_days

    assert isinstance(body["session_buckets"], list)
    assert isinstance(body["weekly_buckets"], list)
    assert len(body["session_buckets"]) == 24 * 60
    assert len(body["weekly_buckets"]) == 7 * 24


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
                project="ProjWeekly",
                input_tokens=100,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            ),
            _record(
                "monthly-only",
                "2026-05-20T00:00:00.000Z",  # older than weekly, inside monthly
                project="ProjMonthly",
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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    params = f"session_since={ts}&weekly_since={ts}&month_since={ts}" f"&lookback_since={ts}&granularity=hour"
    body = client.get(f"/api/analytics?{params}").get_json()

    assert len(body["daily_cost"]) == 24
    assert len(body["daily_sessions"]) == 24
    # The current hour's bucket holds the one record's spend.
    assert abs(body["daily_cost"][-1]["value"] - 3.0) < 0.001
    assert body["daily_sessions"][-1]["value"] == 1


def test_analytics_month_granularity_spans_record_months(client):
    # "All" view: spend/sessions roll up per calendar month from the first record.
    now = datetime.now(timezone.utc)
    two_months_ago = (now.replace(day=1) - timedelta(days=40)).strftime("%Y-%m-%dT12:00:00.000Z")
    client.post(
        "/api/records",
        json=[_record("m1", two_months_ago, session_id="s-old")],
    )
    params = (
        f"session_since={now:%Y-%m-%dT%H:%M:%S.000Z}"
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
