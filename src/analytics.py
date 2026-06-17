"""Analytics aggregation — all chart computation that previously ran in Swift.

Called by the /api/analytics route; kept separate so it can be unit-tested
without HTTP overhead.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from src.models import QuotaSnapshot
from src.models import UsageRecord
from src.models import format_timestamp

# ---------------------------------------------------------------------------
# Model pricing (mirrors Swift's ModelPricing)
# ---------------------------------------------------------------------------

_MODEL_RATES: dict[str, tuple[float, float]] = {
    "fable": (10.0, 50.0),
    "mythos": (10.0, 50.0),
    "opus": (5.0, 25.0),
    "haiku": (1.0, 5.0),
    "sonnet": (3.0, 15.0),
}
_DEFAULT_RATES = (3.0, 15.0)


def _model_rates(model: str) -> tuple[float, float]:
    for key, rates in _MODEL_RATES.items():
        if key in model:
            return rates
    return _DEFAULT_RATES


def estimated_cost_fields(
    model: str,
    input_tokens: int,
    cache_creation_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
) -> float:
    """Estimated USD cost from raw token counts.

    Kept field-based (not record-based) so callers can sum lifetime cost from a
    column-limited query without hydrating full ORM rows for all of history.
    """
    input_rate, output_rate = _model_rates(model)
    return (
        input_tokens * input_rate / 1_000_000
        + cache_creation_tokens * input_rate * 1.25 / 1_000_000
        + output_tokens * output_rate / 1_000_000
        + cache_read_tokens * input_rate * 0.1 / 1_000_000
    )


def estimated_cost(r: UsageRecord) -> float:
    return estimated_cost_fields(
        r.model, r.input_tokens, r.cache_creation_tokens, r.output_tokens, r.cache_read_tokens
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _total_tokens(r: UsageRecord) -> int:
    return r.input_tokens + r.output_tokens + r.cache_creation_tokens


def _to_ranked(grouped: dict[str, int], top: int) -> list[dict]:
    total = sum(grouped.values())
    if total == 0:
        return []
    return [
        {"label": k, "tokens": v, "fraction": v / total}
        for k, v in sorted(grouped.items(), key=lambda x: x[1], reverse=True)[:top]
    ]


def _add_months(start: datetime, n: int) -> datetime:
    """First-of-month `n` months after `start` (a month-aligned datetime)."""
    idx = start.year * 12 + (start.month - 1) + n
    return datetime(idx // 12, idx % 12 + 1, 1)


def _build_hourly_activity(
    records: list[UsageRecord],
    lookback_cutoff: datetime,
    now: datetime,
) -> list[dict]:
    """Average API requests per hour-of-day across the lookback period."""
    now_naive = now.replace(tzinfo=None)
    cutoff_naive = lookback_cutoff.replace(tzinfo=None)
    days = max(1, (now_naive.date() - cutoff_naive.date()).days + 1)

    counts_by_hour: dict[int, int] = defaultdict(int)
    for r in records:
        counts_by_hour[r.timestamp.hour] += 1

    return [{"hour": h, "value": counts_by_hour.get(h, 0) / days} for h in range(24)]


def _build_series(
    records: list[UsageRecord],
    costs: list[float],
    granularity: str,
    lookback_cutoff: datetime,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    """Bucket records into a spend series and a distinct-session-count series.

    granularity selects both the bucket width and the span:
      - "hour":  24 hourly buckets ending at the current hour (the 1D view)
      - "month": monthly buckets from the earliest record's month to this one (All)
      - "day":   daily buckets from the lookback cutoff to today, capped at 30 days

    Bucket starts are emitted as ISO8601 timestamps (naive UTC, as stored).
    """
    now = now.replace(tzinfo=None)

    if granularity == "hour":

        def key(ts: datetime) -> datetime:
            return ts.replace(minute=0, second=0, microsecond=0)

        end = key(now)
        slots = [end - timedelta(hours=i) for i in range(23, -1, -1)]
    elif granularity == "month":

        def key(ts: datetime) -> datetime:
            return datetime(ts.year, ts.month, 1)

        start = key(min((r.timestamp for r in records), default=now))
        end = key(now)
        slots = []
        slot = start
        while slot <= end:
            slots.append(slot)
            slot = _add_months(slot, 1)
    else:  # "day"

        def key(ts: datetime) -> datetime:
            return datetime(ts.year, ts.month, ts.day)

        end = key(now)
        start = max(key(lookback_cutoff), end - timedelta(days=29))
        slots = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    cost_by: dict[datetime, float] = defaultdict(float)
    sessions_by: dict[datetime, set] = defaultdict(set)
    for r, cost in zip(records, costs):
        slot = key(r.timestamp)
        cost_by[slot] += cost
        if r.session_id:
            sessions_by[slot].add(r.session_id)

    cost_series = [{"date": format_timestamp(s), "value": cost_by.get(s, 0.0)} for s in slots]
    sessions_series = [{"date": format_timestamp(s), "value": len(sessions_by.get(s, set()))} for s in slots]
    return cost_series, sessions_series


def _make_buckets(
    records: list[UsageRecord],
    cutoff: datetime,
    unit: str,
    count: int,
) -> list[dict]:
    """Group records into equal-width time buckets, returning [{timestamp, tokens}].

    unit is "minute" or "hour". count is the number of buckets to emit starting
    from the aligned cutoff. Buckets with no records get tokens=0.
    cutoff is naive UTC (as stored by SQLAlchemy/SQLite).
    """
    if unit == "minute":

        def truncate(ts: datetime) -> datetime:
            return ts.replace(second=0, microsecond=0)

        delta = timedelta(minutes=1)
    else:

        def truncate(ts: datetime) -> datetime:
            return ts.replace(minute=0, second=0, microsecond=0)

        delta = timedelta(hours=1)

    start = truncate(cutoff)
    grouped: dict[datetime, int] = defaultdict(int)
    for r in records:
        grouped[truncate(r.timestamp)] += _total_tokens(r)

    return [
        {"timestamp": format_timestamp(slot := start + delta * i), "tokens": grouped.get(slot, 0)}
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Main aggregation
# ---------------------------------------------------------------------------


def _quota_history(records: list[QuotaSnapshot]) -> list[dict]:
    """Real polled quota readings as [{timestamp, percent_used, resets_at?}], oldest first."""
    result = []
    for q in records:
        entry: dict = {"timestamp": format_timestamp(q.timestamp), "percent_used": q.percent_used}
        if q.resets_at:
            entry["resets_at"] = format_timestamp(q.resets_at)
        result.append(entry)
    return result


def compute_analytics(
    session_records: list[UsageRecord],
    weekly_records: list[UsageRecord],
    month_records: list[UsageRecord],
    lookback_records: list[UsageRecord],
    lifetime_cost: float,
    session_cutoff: datetime,
    weekly_cutoff: datetime,
    lookback_cutoff: datetime,
    granularity: str,
    session_cost_records: list[UsageRecord],
    session_quota_records: list[QuotaSnapshot] | None = None,
    weekly_quota_records: list[QuotaSnapshot] | None = None,
) -> dict:
    now = datetime.now(timezone.utc)

    # `session_records` spans a wide 24h window so the session_buckets chart can
    # show the 5h rolling window resetting multiple times. The "Session" cost
    # figure must instead reflect only the *current* session — narrower and
    # passed separately as `session_cost_records` — otherwise it double-counts
    # spend from outside the actual rolling window (and can exceed "Today").
    session_cost = sum(estimated_cost(r) for r in session_cost_records)
    weekly_cost = sum(estimated_cost(r) for r in weekly_records)
    month_cost = sum(estimated_cost(r) for r in month_records)

    # Token breakdowns, cache, model/project/skill mix, and web counts are all
    # labeled with the selected lookback period in the UI, so they aggregate over
    # the lookback window — not a fixed 7-day or 30-day window.
    total_input = total_output = total_cache_create = total_cache_read = 0
    total_web_searches = total_web_fetches = 0
    model_tokens: dict[str, int] = defaultdict(int)
    project_tokens: dict[str, int] = defaultdict(int)
    skill_tokens: dict[str, int] = defaultdict(int)

    for r in lookback_records:
        total_input += r.input_tokens
        total_output += r.output_tokens
        total_cache_create += r.cache_creation_tokens
        total_cache_read += r.cache_read_tokens
        total_web_searches += r.web_searches
        total_web_fetches += r.web_fetches

        tok = _total_tokens(r)
        model_tokens[r.model] += tok
        project_tokens[r.project] += tok
        if r.attribution_skill:
            skill_tokens[r.attribution_skill] += tok

    cacheable_denom = total_input + total_cache_read + total_cache_create
    cache_hit_rate = total_cache_read / cacheable_denom if cacheable_denom > 0 else 0.0
    # Rough blended $/Mtok over the lookback window (total cost spread across
    # cacheable tokens). Only used to estimate cache savings below — not a precise
    # per-token input rate, hence "blended".
    lookback_costs = [estimated_cost(r) for r in lookback_records]
    lookback_cost = sum(lookback_costs)
    blended_rate = (lookback_cost / cacheable_denom * 1_000_000) if cacheable_denom > 0 else 3.0
    cache_savings = total_cache_read * blended_rate * 0.9 / 1_000_000

    all_tokens = total_input + total_output + total_cache_create + total_cache_read

    # Spend/sessions series; bucket width follows the lookback granularity
    # (hourly for 1D, daily for 7D/30D, monthly for All).
    daily_cost, daily_sessions = _build_series(
        lookback_records, lookback_costs, granularity, lookback_cutoff, now
    )

    # "Today" is a calendar-day figure independent of the series granularity.
    today = now.date()
    today_cost = sum(cost for r, cost in zip(lookback_records, lookback_costs) if r.timestamp.date() == today)

    return {
        "session_cost": session_cost,
        "today_cost": today_cost,
        "weekly_cost": weekly_cost,
        "month_cost": month_cost,
        "lifetime_cost": lifetime_cost,
        "cache_hit_rate": cache_hit_rate,
        "cache_savings_usd": cache_savings,
        "token_types": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_creation_tokens": total_cache_create,
            "cache_read_tokens": total_cache_read,
            "input_fraction": total_input / all_tokens if all_tokens > 0 else 0.0,
            "output_fraction": total_output / all_tokens if all_tokens > 0 else 0.0,
            "cache_creation_fraction": total_cache_create / all_tokens if all_tokens > 0 else 0.0,
            "cache_read_fraction": total_cache_read / all_tokens if all_tokens > 0 else 0.0,
        },
        "model_breakdown": _to_ranked(model_tokens, top=3),
        "project_breakdown": _to_ranked(project_tokens, top=5),
        "skill_breakdown": _to_ranked(skill_tokens, top=5),
        "daily_cost": daily_cost,
        "daily_sessions": daily_sessions,
        "session_quota_history": _quota_history(session_quota_records or []),
        "weekly_quota_history": _quota_history(weekly_quota_records or []),
        "hourly_activity": _build_hourly_activity(lookback_records, lookback_cutoff, now),
        "total_web_searches": total_web_searches,
        "total_web_fetches": total_web_fetches,
        # 24h of minute buckets: the session window is 5h, but the app charts the
        # last 24h so the window can be seen resetting several times across the span.
        "session_buckets": _make_buckets(session_records, session_cutoff, "minute", 24 * 60),
        "weekly_buckets": _make_buckets(weekly_records, weekly_cutoff, "hour", 7 * 24),
    }
