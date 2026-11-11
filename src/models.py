"""SQLAlchemy models for the stored usage history.

`usage_records` is one row per Claude API request, collapsed from the
`assistant`-type JSONL lines in ~/.claude/projects/**/*.jsonl (Claude Code writes
one line per content block, each repeating the request's usage);
`quota_snapshots` holds polled quota readings,
stored only when the value changes plus a periodic heartbeat (see `routes.py`).
The notch app POSTs both and reads aggregates back from /api/analytics.
"""

from datetime import UTC
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column


class Base(DeclarativeBase):
    pass


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO8601 timestamp, normalising a trailing Z to +00:00."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_timestamp(value: datetime) -> str:
    """Serialise to ISO8601 (UTC) with exactly 3 fractional digits and a trailing Z.

    Millisecond precision is the format Swift's `ISO8601DateFormatter` parses
    reliably; `datetime.isoformat()` would emit 6-digit microseconds, which it does
    not round-trip.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    utc = value.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


class UsageStats(Base):
    """Single-row table caching lifetime_cost to avoid full table scans."""

    __tablename__ = "usage_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    lifetime_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), nullable=False
    )


class QuotaSnapshot(Base):
    """One polled reading of a provider's quota utilization (e.g. Claude's 5h/7d windows).

    Real ground truth for the chart's "% Quota" view, recorded straight from the
    notch app's poll of the provider's usage endpoint. Claude's local JSONL doesn't
    carry quota percentages, so there is no historical source to backfill from.

    Keyed by (window_type, timestamp) so a repeated POST is a no-op.
    """

    __tablename__ = "quota_snapshots"
    __table_args__ = {"sqlite_with_rowid": False}

    window_type: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    percent_used: Mapped[float] = mapped_column(Float, nullable=False)
    resets_at: Mapped[datetime | None] = mapped_column(DateTime)

    @classmethod
    def row_from_json(cls, data: dict) -> dict:
        return {
            "window_type": data["window_type"],
            "timestamp": parse_timestamp(data["timestamp"]),
            "percent_used": data.get("percent_used"),
            "resets_at": parse_timestamp(data["resets_at"]) if data.get("resets_at") else None,
        }


class UsageRecord(Base):
    """One Claude API request. `timestamp` is its first JSONL line's time and never changes."""

    __tablename__ = "usage_records"

    request_id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    cwd: Mapped[str] = mapped_column(String, nullable=False)
    git_branch: Mapped[str | None] = mapped_column(String)
    model: Mapped[str] = mapped_column(String, nullable=False)
    attribution_skill: Mapped[str | None] = mapped_column(String)
    is_sidechain: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    ephemeral_1h_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("idx_usage_records_timestamp", "timestamp"),)

    @classmethod
    def row_from_json(cls, data: dict) -> dict:
        """Map one API/JSONL-derived record to an insert-ready column dict."""
        return {
            "request_id": data["request_id"],
            "timestamp": parse_timestamp(data["timestamp"]),
            "session_id": data.get("session_id") or "",
            "cwd": data.get("cwd") or "",
            "git_branch": data.get("git_branch"),
            "model": data.get("model") or "unknown",
            "attribution_skill": data.get("attribution_skill"),
            "is_sidechain": bool(data.get("is_sidechain", False)),
            "input_tokens": data.get("input_tokens") or 0,
            "output_tokens": data.get("output_tokens") or 0,
            "cache_creation_tokens": data.get("cache_creation_tokens") or 0,
            "cache_read_tokens": data.get("cache_read_tokens") or 0,
            "ephemeral_1h_tokens": data.get("ephemeral_1h_tokens") or 0,
        }


def project_from_cwd(cwd: str) -> str:
    return PurePosixPath(cwd).name or "unknown"
