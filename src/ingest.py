"""Write usage records to the DB; shared by `POST /api/records` and the backfill."""

import logging
from datetime import datetime
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.analytics import estimated_cost_fields
from src.database import session_scope
from src.models import UsageRecord
from src.models import UsageStats

logger = logging.getLogger(__name__)


def insert_usage_records(items: list) -> dict:
    """Insert new records (idempotent by uuid), bump the lifetime cost, and return inserted/skipped/invalid counts."""
    rows = {}
    invalid = 0
    for item in items:
        if not isinstance(item, dict) or not item.get("uuid"):
            logger.warning("insert_usage_records: skipping record with missing uuid")
            invalid += 1
            continue
        rows[item["uuid"]] = UsageRecord.row_from_json(item)
    if not rows:
        return {"inserted": 0, "skipped": 0, "invalid": invalid}

    with session_scope() as session:
        existing_uuids = set(session.scalars(select(UsageRecord.uuid).where(UsageRecord.uuid.in_(rows))))
        new_rows = [data for uuid, data in rows.items() if uuid not in existing_uuids]
        if new_rows:
            session.execute(
                sqlite_insert(UsageRecord).on_conflict_do_nothing(index_elements=["uuid"]), new_rows
            )
            stats = session.get(UsageStats, 1)
            if stats is not None:
                stats.lifetime_cost += sum(
                    estimated_cost_fields(
                        d["model"],
                        d["input_tokens"],
                        d["cache_creation_tokens"],
                        d["output_tokens"],
                        d["cache_read_tokens"],
                    )
                    for d in new_rows
                )
                stats.last_updated = datetime.now(timezone.utc)

    return {"inserted": len(new_rows), "skipped": len(rows) - len(new_rows), "invalid": invalid}
