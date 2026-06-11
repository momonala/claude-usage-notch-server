"""Analytics aggregation — all chart computation that previously ran in Swift.

Called by the /api/analytics route; kept separate so it can be unit-tested
without HTTP overhead.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from src.models import UsageRecord
from src.models import format_timestamp

# ---------------------------------------------------------------------------
# Model pricing (mirrors Swift's ModelPricing)
# ---------------------------------------------------------------------------

_MODEL_RATES: dict[str, tuple[float, float]] = {
    "opus": (15.0, 75.0),
    "haiku": (0.80, 4.0),
    "sonnet": (3.0, 15.0),
}
_DEFAULT_RATES = (3.0, 15.0)


def _model_rates(model: str) -> tuple[float, float]:
    for key, rates in _MODEL_RATES.items():
        if key in model:
            return rates
    return _DEFAULT_RATES


def estimated_cost(r: UsageRecord) -> float:
    input_rate, output_rate = _model_rates(r.model)
    return (
        (r.input_tokens + r.cache_creation_tokens) * input_rate / 1_000_000
        + r.output_tokens * output_rate / 1_000_000
        + r.cache_read_tokens * input_rate * 0.1 / 1_000_000
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
        truncate = lambda ts: ts.replace(second=0, microsecond=0)  # noqa: E731
        delta = timedelta(minutes=1)
    else:
        truncate = lambda ts: ts.replace(minute=0, second=0, microsecond=0)  # noqa: E731
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


def compute_analytics(
    session_records: list[UsageRecord],
    weekly_records: list[UsageRecord],
    monthly_records: list[UsageRecord],
    session_cutoff: datetime,
    weekly_cutoff: datetime,
) -> dict:
    now = datetime.now(timezone.utc)

    session_cost = sum(estimated_cost(r) for r in session_records)
    monthly_cost = sum(estimated_cost(r) for r in monthly_records)

    weekly_cost = 0.0
    total_input = total_output = total_cache_create = total_cache_read = 0
    total_web_searches = total_web_fetches = 0
    cost_by_day: dict = defaultdict(float)
    sessions_by_day: dict[object, set] = defaultdict(set)
    model_tokens: dict[str, int] = defaultdict(int)
    project_tokens: dict[str, int] = defaultdict(int)
    skill_tokens: dict[str, int] = defaultdict(int)

    for r in weekly_records:
        cost = estimated_cost(r)
        weekly_cost += cost
        total_input += r.input_tokens
        total_output += r.output_tokens
        total_cache_create += r.cache_creation_tokens
        total_cache_read += r.cache_read_tokens
        total_web_searches += r.web_searches
        total_web_fetches += r.web_fetches

        day = r.timestamp.date()  # naive UTC from DB
        cost_by_day[day] += cost
        if r.session_id:
            sessions_by_day[day].add(r.session_id)

        tok = _total_tokens(r)
        model_tokens[r.model] += tok
        project_tokens[r.project] += tok
        if r.attribution_skill:
            skill_tokens[r.attribution_skill] += tok

    cacheable_denom = total_input + total_cache_read + total_cache_create
    cache_hit_rate = total_cache_read / cacheable_denom if cacheable_denom > 0 else 0.0
    avg_input_rate = (weekly_cost / max(1, cacheable_denom) * 1_000_000) if weekly_records else 3.0
    cache_savings = total_cache_read * avg_input_rate * 0.9 / 1_000_000

    all_tokens = max(1, total_input + total_output + total_cache_create + total_cache_read)

    days = [(now - timedelta(days=6 - i)).date() for i in range(7)]
    daily_cost = [{"date": str(d), "value": cost_by_day.get(d, 0.0)} for d in days]
    daily_sessions = [{"date": str(d), "value": len(sessions_by_day.get(d, set()))} for d in days]
    today_cost = cost_by_day.get(days[-1], 0.0)

    return {
        "session_cost": session_cost,
        "today_cost": today_cost,
        "weekly_cost": weekly_cost,
        "month_cost": monthly_cost,
        "cache_hit_rate": cache_hit_rate,
        "cache_savings_usd": cache_savings,
        "token_types": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_creation_tokens": total_cache_create,
            "cache_read_tokens": total_cache_read,
            "input_fraction": total_input / all_tokens,
            "output_fraction": total_output / all_tokens,
            "cache_creation_fraction": total_cache_create / all_tokens,
            "cache_read_fraction": total_cache_read / all_tokens,
        },
        "model_breakdown": _to_ranked(model_tokens, top=3),
        "project_breakdown": _to_ranked(project_tokens, top=5),
        "skill_breakdown": _to_ranked(skill_tokens, top=5),
        "daily_cost": daily_cost,
        "daily_sessions": daily_sessions,
        "total_web_searches": total_web_searches,
        "total_web_fetches": total_web_fetches,
        "session_buckets": _make_buckets(session_records, session_cutoff, "minute", 5 * 60),
        "weekly_buckets": _make_buckets(weekly_records, weekly_cutoff, "hour", 7 * 24),
    }
