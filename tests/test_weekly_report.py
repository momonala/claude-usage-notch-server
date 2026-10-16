"""Tests for the weekly report: one row per request, pricing, work split, gaps, and weekly limit pairing."""

import sqlite3

import pytest

from src.app import create_app
from src.weekly_report import build_report
from src.weekly_report import load
from src.weekly_report import model_label
from src.weekly_report import rates_for
from tests.test_routes import _quota
from tests.test_routes import _record

WORK_PATHS = ["/code/rev-ops"]
MILLION_EACH = dict(
    model="claude-opus-5-5",
    input_tokens=0,
    output_tokens=1_000_000,
    cache_creation_tokens=1_000_000,
    ephemeral_1h_tokens=1_000_000,
    cache_read_tokens=1_000_000,
)
COST_PER_REQUEST = 20.0 + 8.0 + 0.20


@pytest.fixture
def post():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c.post


@pytest.fixture
def report(tmp_path):
    def build() -> dict:
        requests, quota = load(str(tmp_path / "test.db"), None, WORK_PATHS)
        return build_report(requests, quota)

    return build


def test_weeks_count_each_request_once_and_split_work_from_personal(post, report):
    post(
        "/api/records",
        json=[
            _record(
                "a",
                "2026-09-23T10:00:00Z",
                request_id="req_1",
                cwd="/u/code/rev-ops/ingestion",
                **MILLION_EACH,
            ),
            _record(
                "b",
                "2026-09-23T10:00:01Z",
                request_id="req_1",
                cwd="/u/code/rev-ops/ingestion",
                **MILLION_EACH,
            ),
            _record(
                "c", "2026-10-06T10:00:00Z", request_id="req_2", cwd="/u/code/projects/notch", **MILLION_EACH
            ),
        ],
    )

    weeks = report()["weeks"]

    assert [w["week"] for w in weeks] == ["2026-09-21", "2026-09-28", "2026-10-05"]
    assert weeks[1]["all"]["reqs"] == 0
    first = weeks[0]
    assert first["all"]["reqs"] == 1
    assert first["all"]["tokens"]["output"] == 1_000_000
    assert first["all"]["usd"]["output"] == pytest.approx(20.0)
    assert first["all"]["usd"]["cache_write"] == pytest.approx(8.0)
    assert first["all"]["usd"]["cache_read"] == pytest.approx(0.20)
    assert first["work"]["reqs"] == 1 and first["personal"]["reqs"] == 0
    assert weeks[2]["personal"]["reqs"] == 1


def test_weekly_limit_counts_early_resets(post, report):
    post(
        "/api/records",
        json=[
            _record("a", "2026-09-01T10:00:00Z", request_id="req_1", **MILLION_EACH),
            _record("b", "2026-09-03T10:00:00Z", request_id="req_2", **MILLION_EACH),
        ],
    )
    reset = "2026-09-06T02:00:00Z"
    post(
        "/api/quota_snapshots",
        json=[
            _quota("weekly", "2026-09-01T12:00:00Z", percent_used=0.30, resets_at=reset),
            _quota("weekly", "2026-09-02T12:00:00Z", percent_used=0.05, resets_at=reset),
            _quota("weekly", "2026-09-03T12:00:00Z", percent_used=0.25, resets_at="2026-09-06T01:59:59Z"),
        ],
    )

    [window] = report()["limits"]

    assert window["used"] == pytest.approx(0.30 + 0.25)
    assert window["midweek_resets"] == 1
    assert window["usd_per_pct"] == pytest.approx(2 * COST_PER_REQUEST / 55)


def test_rates_and_labels_match_the_most_specific_model_id():
    assert rates_for("claude-opus-5-5").input == 4.0
    assert rates_for("claude-opus-5").input == 5.0
    assert rates_for("claude-fable-5-1").cache_read == 0.25
    assert rates_for("<synthetic>").input == 0.0
    assert model_label("claude-opus-5-5") == "Opus 5.5"
    assert model_label("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert model_label("claude-opus-5") == "Opus 5"


def test_load_rejects_an_empty_range(tmp_path):
    sqlite3.connect(tmp_path / "test.db").close()
    with pytest.raises(Exception, match="No usage records"):
        load(str(tmp_path / "test.db"), "2099-01-01", WORK_PATHS)


def test_weekly_limit_keeps_windows_apart_within_one_hour(post, report):
    post(
        "/api/records",
        json=[
            _record("a", "2026-09-01T10:00:00Z", request_id="req_1", **MILLION_EACH),
            _record("b", "2026-09-07T10:00:00Z", request_id="req_2", **MILLION_EACH),
        ],
    )
    post(
        "/api/quota_snapshots",
        json=[
            _quota("weekly", "2026-09-06T01:30:00Z", percent_used=1.0, resets_at="2026-09-06T01:59:00Z"),
            _quota("weekly", "2026-09-06T01:59:30Z", percent_used=0.0, resets_at="2026-09-13T01:59:00Z"),
            _quota("weekly", "2026-09-07T12:00:00Z", percent_used=0.10, resets_at="2026-09-13T01:59:00Z"),
        ],
    )

    later = report()["limits"][-1]

    assert later["reset"].startswith("2026-09-13")
    assert later["used"] == pytest.approx(0.10)


def test_since_drops_limit_windows_that_start_before_it(post, tmp_path):
    post("/api/records", json=[_record("a", "2026-09-04T10:00:00Z", request_id="req_1", **MILLION_EACH)])
    post(
        "/api/quota_snapshots",
        json=[_quota("weekly", "2026-09-04T12:00:00Z", percent_used=0.30, resets_at="2026-09-06T02:00:00Z")],
    )

    requests, quota = load(str(tmp_path / "test.db"), "2026-09-03", WORK_PATHS)

    assert build_report(requests, quota)["limits"] == []
