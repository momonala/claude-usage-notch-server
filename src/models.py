"""SQLAlchemy model for a single Claude Code usage record.

One row per `assistant`-type JSONL line from ~/.claude/projects/**/*.jsonl. The
notch app extracts these and POSTs them here; the chart view reads them back.
The server aggregates records on demand via /api/analytics.
"""

from datetime import datetime
from datetime import timezone
from typing import ClassVar

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
        value = value.replace(tzinfo=timezone.utc)
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


class UsageStats(Base):
    """Single-row table caching lifetime_cost to avoid full table scans."""

    __tablename__ = "usage_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    lifetime_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )


class UsageRecord(Base):
    __tablename__ = "usage_records"

    uuid: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    parent_uuid: Mapped[str | None] = mapped_column(String)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cwd: Mapped[str] = mapped_column(String, nullable=False)
    project: Mapped[str] = mapped_column(String, nullable=False)
    git_branch: Mapped[str | None] = mapped_column(String)
    model: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str | None] = mapped_column(String)
    entrypoint: Mapped[str | None] = mapped_column(String)
    attribution_skill: Mapped[str | None] = mapped_column(String)
    is_sidechain: Mapped[bool] = mapped_column(default=False, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String)
    service_tier: Mapped[str | None] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ephemeral_1h_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ephemeral_5m_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    web_searches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    web_fetches: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        Index("idx_usage_records_timestamp", "timestamp"),
        Index("idx_usage_records_session_id", "session_id"),
        Index("idx_usage_records_project", "project"),
        Index("idx_usage_records_request_id", "request_id"),
    )

    # Fields the client sends and the API returns. `ingested_at` is server-owned
    # and excluded from both directions.
    _CLIENT_FIELDS: ClassVar[tuple[str, ...]] = (
        "request_id",
        "session_id",
        "parent_uuid",
        "cwd",
        "project",
        "git_branch",
        "model",
        "version",
        "entrypoint",
        "attribution_skill",
        "is_sidechain",
        "stop_reason",
        "service_tier",
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "ephemeral_1h_tokens",
        "ephemeral_5m_tokens",
        "web_searches",
        "web_fetches",
    )

    @classmethod
    def row_from_json(cls, data: dict) -> dict:
        """Map an API record to an insert-ready column dict.

        Excludes the server-owned `ingested_at` so its default fires on insert.
        """
        row = {field: data.get(field) for field in cls._CLIENT_FIELDS}
        row["uuid"] = data["uuid"]
        row["timestamp"] = parse_timestamp(data["timestamp"])
        return row

    def to_json(self) -> dict:
        result = {field: getattr(self, field) for field in self._CLIENT_FIELDS}
        result["uuid"] = self.uuid
        result["timestamp"] = format_timestamp(self.timestamp)
        return result
