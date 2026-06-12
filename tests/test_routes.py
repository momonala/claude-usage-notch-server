"""Tests for the records API: health, upsert idempotency, and time filtering."""

from datetime import date

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
    "&monthly_since=2026-05-13T00:00:00.000Z"
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
    # Daily series spans monthly_since -> today, capped at 30 days.
    monthly_since = date(2026, 5, 13)
    expected_days = min((date.today() - monthly_since).days + 1, 30)
    assert len(body["daily_cost"]) == expected_days
    assert len(body["daily_sessions"]) == expected_days

    assert isinstance(body["session_buckets"], list)
    assert isinstance(body["weekly_buckets"], list)
    assert len(body["session_buckets"]) == 5 * 60
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


def test_analytics_missing_params_returns_400(client):
    assert client.get("/api/analytics").status_code == 400
    assert client.get("/api/analytics?session_since=2026-06-11T07:00:00.000Z").status_code == 400


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
