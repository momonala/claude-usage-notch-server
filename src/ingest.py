"""Write usage records to the DB; shared by `POST /api/records` and the backfill."""

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.analytics import estimated_cost_fields
from src.database import session_scope
from src.models import UsageRecord
from src.models import UsageStats

logger = logging.getLogger(__name__)

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "ephemeral_1h_tokens",
)


def _cost(row) -> float:
    get = row.get if isinstance(row, dict) else lambda field: getattr(row, field)
    return estimated_cost_fields(
        get("model"),
        get("input_tokens"),
        get("cache_creation_tokens"),
        get("output_tokens"),
        get("cache_read_tokens"),
    )


def _collapse_by_request(items: list) -> tuple[dict[str, dict], int]:
    """One row per request_id, keeping the line with the most output and the earliest timestamp.

    Claude Code writes one JSONL line per content block, each repeating the request's
    usage; early lines carry a partial output_tokens count.
    """
    rows: dict[str, dict] = {}
    invalid = 0
    for item in items:
        if not isinstance(item, dict) or not item.get("request_id"):
            invalid += 1
            continue
        row = UsageRecord.row_from_json(item)
        kept = rows.get(row["request_id"])
        if kept is None:
            rows[row["request_id"]] = row
            continue
        first_seen = min(kept["timestamp"], row["timestamp"])
        if row["output_tokens"] > kept["output_tokens"]:
            kept = rows[row["request_id"]] = row
        kept["timestamp"] = first_seen
    return rows, invalid


def insert_usage_records(items: list) -> dict:
    """Upsert one row per request, move the lifetime cost by the change, and return counts."""
    rows, invalid = _collapse_by_request(items)
    if invalid:
        logger.warning("insert_usage_records: skipped %d records without a request_id", invalid)
    counts = {"inserted": 0, "updated": 0, "skipped": 0, "invalid": invalid}
    if not rows:
        return counts

    with session_scope() as session:
        existing = {
            r.request_id: r
            for r in session.execute(
                select(
                    UsageRecord.request_id,
                    UsageRecord.model,
                    *(getattr(UsageRecord, f) for f in _USAGE_FIELDS),
                ).where(UsageRecord.request_id.in_(rows))
            )
        }
        changed = [
            row
            for request_id, row in rows.items()
            if request_id not in existing or row["output_tokens"] > existing[request_id].output_tokens
        ]
        counts["inserted"] = sum(1 for row in changed if row["request_id"] not in existing)
        counts["updated"] = len(changed) - counts["inserted"]
        counts["skipped"] = len(rows) - len(changed)
        if not changed:
            return counts

        stmt = sqlite_insert(UsageRecord)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["request_id"],
                set_={field: stmt.excluded[field] for field in _USAGE_FIELDS},
                where=stmt.excluded.output_tokens > UsageRecord.output_tokens,
            ),
            changed,
        )
        stats = session.get(UsageStats, 1)
        if stats is not None:
            stats.lifetime_cost += sum(
                _cost(row) - (_cost(existing[row["request_id"]]) if row["request_id"] in existing else 0.0)
                for row in changed
            )
            stats.last_updated = datetime.now(UTC)

    return counts
