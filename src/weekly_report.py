"""Render a token and cost report from the local SQLite DB as a self-contained HTML page.

Usage:
    bash sync.sh pull && uv run weekly-report
    uv run weekly-report --since 2026-07-20 --out ~/Desktop/claude-usage.html
"""

import json
import re
import sqlite3
import statistics
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import typer

from src.config import DB_PATH
from src.config import REPORT_WORK_PATHS

TEMPLATE_PATH = Path(__file__).with_name("weekly_report.html")

TOP_PROJECTS = 20
TOP_SKILLS = 15
TOP_MODELS = 5
SESSION_BUCKETS_USD = [0.1, 1, 5, 20, 50]


@dataclass(frozen=True)
class Rates:
    input: float
    cache_read: float

    @property
    def output(self) -> float:
        return self.input * 5

    @property
    def cache_write_5m(self) -> float:
        return self.input * 1.25

    @property
    def cache_write_1h(self) -> float:
        return self.input * 2


# USD per million tokens, matched by model-id prefix; most specific prefix first.
_RATES: list[tuple[str, Rates]] = [
    ("claude-opus-5-5", Rates(input=4.0, cache_read=0.20)),
    ("claude-fable-5-1", Rates(input=10.0, cache_read=0.25)),
    ("claude-mythos-5-1", Rates(input=10.0, cache_read=0.25)),
    ("claude-fable", Rates(input=10.0, cache_read=1.0)),
    ("claude-mythos", Rates(input=10.0, cache_read=1.0)),
    ("claude-opus", Rates(input=5.0, cache_read=0.50)),
    ("claude-sonnet-5", Rates(input=2.0, cache_read=0.20)),
    ("claude-sonnet", Rates(input=3.0, cache_read=0.30)),
    ("claude-haiku-5-5", Rates(input=0.10, cache_read=0.01)),
    ("claude-haiku", Rates(input=1.0, cache_read=0.10)),
]
_NO_COST = Rates(input=0.0, cache_read=0.0)


def rates_for(model: str) -> Rates:
    return next((rates for prefix, rates in _RATES if model.startswith(prefix)), _NO_COST)


def model_label(model: str) -> str:
    """'claude-opus-5-5' -> 'Opus 5.5', 'claude-haiku-4-5-20251001' -> 'Haiku 4.5'."""
    match = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?", model)
    if not match:
        return model
    family, major, minor = match.groups()
    return f"{family.title()} {major}{'.' + minor if minor else ''}"


def is_work(cwd: str, work_paths: list[str]) -> bool:
    path = cwd.rstrip("/") + "/"
    return any(marker.rstrip("/") + "/" in path for marker in work_paths)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=timezone.utc)


_REQUESTS_SQL = """
WITH one_row_per_request AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY coalesce(request_id, uuid) ORDER BY output_tokens DESC, timestamp DESC
    ) AS rn
    FROM usage_records
    WHERE :since IS NULL OR timestamp >= :since
)
SELECT timestamp, session_id, cwd, project, model, attribution_skill,
       input_tokens, output_tokens, cache_creation_tokens, ephemeral_1h_tokens, cache_read_tokens
FROM one_row_per_request
WHERE rn = 1
ORDER BY timestamp
"""

_WEEKLY_QUOTA_SQL = """
SELECT strftime('%Y-%m-%d %H:00:00', timestamp) AS hour, resets_at, max(percent_used) AS used
FROM quota_snapshots
WHERE window_type = 'weekly' AND resets_at IS NOT NULL
  AND (:since IS NULL OR datetime(resets_at, '-7 days') >= :since)
GROUP BY hour, resets_at
ORDER BY hour
"""


@dataclass(frozen=True)
class Request:
    at: datetime
    session_id: str
    project: str
    model: str
    skill: str | None
    work: bool
    tokens: dict[str, int]
    usd: dict[str, float]

    @property
    def total_tokens(self) -> int:
        return sum(self.tokens.values())

    @property
    def total_usd(self) -> float:
        return sum(self.usd.values())


CATEGORIES = ("input", "output", "cache_write", "cache_read")


def to_request(row: sqlite3.Row, work_paths: list[str]) -> Request:
    rates = rates_for(row["model"])
    cache_write_1h = row["ephemeral_1h_tokens"]
    cache_write_5m = row["cache_creation_tokens"] - cache_write_1h
    return Request(
        at=parse_ts(row["timestamp"]),
        session_id=row["session_id"],
        project=row["project"],
        model=row["model"],
        skill=row["attribution_skill"],
        work=is_work(row["cwd"], work_paths),
        tokens={
            "input": row["input_tokens"],
            "output": row["output_tokens"],
            "cache_write": row["cache_creation_tokens"],
            "cache_read": row["cache_read_tokens"],
        },
        usd={
            "input": row["input_tokens"] * rates.input / 1e6,
            "output": row["output_tokens"] * rates.output / 1e6,
            "cache_write": (cache_write_5m * rates.cache_write_5m + cache_write_1h * rates.cache_write_1h)
            / 1e6,
            "cache_read": row["cache_read_tokens"] * rates.cache_read / 1e6,
        },
    )


def load(db_path: str, since: str | None, work_paths: list[str]) -> tuple[list[Request], list[sqlite3.Row]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_REQUESTS_SQL, {"since": since}).fetchall()
        quota = conn.execute(_WEEKLY_QUOTA_SQL, {"since": since}).fetchall()
    if not rows:
        raise typer.BadParameter(f"No usage records {f'on or after {since} ' if since else ''}in {db_path}")
    return [to_request(r, work_paths) for r in rows], quota


class Totals:
    """Running token and USD sums, split by category."""

    def __init__(self) -> None:
        self.reqs = 0
        self.tokens = dict.fromkeys(CATEGORIES, 0)
        self.usd = dict.fromkeys(CATEGORIES, 0.0)

    def add(self, r: Request) -> None:
        self.reqs += 1
        for c in CATEGORIES:
            self.tokens[c] += r.tokens[c]
            self.usd[c] += r.usd[c]

    def as_dict(self) -> dict:
        return {"reqs": self.reqs, "tokens": self.tokens, "usd": self.usd}


def weekly(requests: list[Request], models: list[str]) -> list[dict]:
    """Per-week totals with empty weeks filled in, split by category, work/personal and model."""
    weeks: dict[date, dict] = {}

    def week_of(day: date) -> dict:
        return weeks.setdefault(
            day,
            {
                "all": Totals(),
                "work": Totals(),
                "personal": Totals(),
                "models": {m: Totals() for m in models + ["Other"]},
            },
        )

    for r in requests:
        w = week_of(week_start(r.at.date()))
        w["all"].add(r)
        w["work" if r.work else "personal"].add(r)
        label = model_label(r.model)
        w["models"][label if label in models else "Other"].add(r)

    day = min(weeks)
    while day <= max(weeks):
        week_of(day)
        day += timedelta(days=7)

    return [
        {
            "week": day.isoformat(),
            "all": w["all"].as_dict(),
            "work": w["work"].as_dict(),
            "personal": w["personal"].as_dict(),
            "models": {m: t.as_dict() for m, t in w["models"].items()},
        }
        for day, w in sorted(weeks.items())
    ]


def top_models(requests: list[Request]) -> list[str]:
    spend: Counter[str] = Counter()
    for r in requests:
        spend[model_label(r.model)] += r.total_usd
    return [m for m, _ in spend.most_common(TOP_MODELS)]


def grouped(requests: list[Request], key, limit: int) -> list[dict]:
    """Totals per group, biggest spend first, with session count and last activity."""
    groups: dict[str, dict] = defaultdict(
        lambda: {"totals": Totals(), "sessions": set(), "last": None, "work": 0}
    )
    for r in requests:
        name = key(r)
        if name is None:
            continue
        g = groups[name]
        g["totals"].add(r)
        g["sessions"].add(r.session_id)
        g["last"] = r.at
        g["work"] += r.work
    ranked = sorted(groups.items(), key=lambda kv: -sum(kv[1]["totals"].usd.values()))
    return [
        {
            "name": name,
            **g["totals"].as_dict(),
            "sessions": len(g["sessions"]),
            "last": g["last"].date().isoformat(),
            "work": g["work"] * 2 > g["totals"].reqs,
        }
        for name, g in ranked[:limit]
    ]


def heatmap(requests: list[Request]) -> dict:
    """Spend and tokens by local weekday (0 = Monday) and hour."""
    usd = [[0.0] * 24 for _ in range(7)]
    tokens = [[0] * 24 for _ in range(7)]
    for r in requests:
        local = r.at.astimezone()
        usd[local.weekday()][local.hour] += r.total_usd
        tokens[local.weekday()][local.hour] += r.total_tokens
    return {"usd": usd, "tokens": tokens, "tz": datetime.now().astimezone().tzname()}


def sessions(requests: list[Request]) -> dict:
    """Cost-per-session histogram and the most expensive sessions."""
    by_session: dict[str, dict] = {}
    for r in requests:
        s = by_session.setdefault(
            r.session_id,
            {"usd": 0.0, "tokens": 0, "start": r.at, "end": r.at, "project": r.project, "work": r.work},
        )
        s["usd"] += r.total_usd
        s["tokens"] += r.total_tokens
        s["end"] = r.at

    edges = [0, *SESSION_BUCKETS_USD]
    buckets = [
        {"from": lo, "to": hi, "count": 0, "usd": 0.0} for lo, hi in zip(edges, SESSION_BUCKETS_USD + [None])
    ]
    for s in by_session.values():
        bucket = next(b for b in buckets if b["to"] is None or s["usd"] < b["to"])
        bucket["count"] += 1
        bucket["usd"] += s["usd"]

    costs = sorted(s["usd"] for s in by_session.values())
    top = sorted(by_session.values(), key=lambda s: -s["usd"])[:10]
    return {
        "count": len(costs),
        "median_usd": statistics.median(costs),
        "p90_usd": costs[int(len(costs) * 0.9)] if len(costs) > 1 else costs[0],
        "buckets": buckets,
        "top": [
            {
                "project": s["project"],
                "work": s["work"],
                "start": s["start"].isoformat(),
                "minutes": round((s["end"] - s["start"]).total_seconds() / 60),
                "usd": s["usd"],
                "tokens": s["tokens"],
            }
            for s in top
        ],
    }


MIDWEEK_RESET_DROP = 0.05


def limit_windows(requests: list[Request], quota: list[sqlite3.Row]) -> list[dict]:
    """Spend per weekly limit window next to how much of the limit it used.

    Readings are hourly peaks per window across all polling machines. Anthropic sometimes resets the weekly limit
    partway through a window without moving `resets_at`, so "used" adds up the rises between readings
    and treats a drop of MIDWEEK_RESET_DROP or more as a reset. A peak reading alone undercounts
    those weeks. `resets_at` differs by a second or so between machines, hence rounding to the hour.
    """
    windows: dict[datetime, dict] = {}
    for row in quota:
        reset = parse_ts(row["resets_at"])
        reset = (reset + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
        w = windows.setdefault(reset, {"used": 0.0, "level": None, "resets": 0})
        value = row["used"]
        if w["level"] is None or value > w["level"]:
            w["used"] += value - (w["level"] or 0.0)
            w["level"] = value
        elif w["level"] - value >= MIDWEEK_RESET_DROP:
            w["used"] += value
            w["level"] = value
            w["resets"] += 1

    result = []
    for reset, w in sorted(windows.items()):
        start = reset - timedelta(days=7)
        spent = sum(r.total_usd for r in requests if start <= r.at < reset)
        if w["used"] <= 0 or spent <= 0:
            continue
        result.append(
            {
                "reset": reset.isoformat(),
                "used": w["used"],
                "midweek_resets": w["resets"],
                "usd": spent,
                "usd_per_pct": spent / (w["used"] * 100),
            }
        )
    return result


def build_report(requests: list[Request], quota: list[sqlite3.Row]) -> dict:
    models = top_models(requests)
    return {
        "first": requests[0].at.isoformat(),
        "last": requests[-1].at.isoformat(),
        "models": models,
        "weeks": weekly(requests, models),
        "projects": grouped(requests, lambda r: r.project, TOP_PROJECTS),
        "skills": grouped(requests, lambda r: r.skill, TOP_SKILLS),
        "heatmap": heatmap(requests),
        "sessions": sessions(requests),
        "limits": limit_windows(requests, quota),
    }


def render(report: dict) -> str:
    return TEMPLATE_PATH.read_text().replace("__REPORT__", json.dumps(report))


def weekly_report_cli(
    since: str | None = typer.Option(None, help="First day to include (YYYY-MM-DD, UTC). Default: all data."),
    out: Path = typer.Option(Path("tmp/weekly-report.html"), help="Where to write the HTML."),
    db: str = typer.Option(DB_PATH, help="SQLite DB to read."),
) -> None:
    requests, quota = load(db, since, REPORT_WORK_PATHS)
    unpriced = sorted({r.model for r in requests if rates_for(r.model) is _NO_COST and r.total_tokens})
    if unpriced:
        typer.secho(
            f"No price for {', '.join(unpriced)}: counted as $0. Add them to _RATES.", fg=typer.colors.YELLOW
        )
    report = build_report(requests, quota)
    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report))
    total = sum(sum(w["all"]["usd"].values()) for w in report["weeks"])
    typer.secho(f"Wrote {out} ({len(report['weeks'])} weeks, ${total:,.0f})", fg=typer.colors.GREEN)


def main() -> None:
    typer.run(weekly_report_cli)


if __name__ == "__main__":
    main()
