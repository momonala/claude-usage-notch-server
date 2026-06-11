"""HTTP routes.

GET  /status                                          liveness check
POST /api/records                                     upsert a batch of records (idempotent by uuid)
GET  /api/records?since=ISO                           records with timestamp >= since
GET  /api/analytics?session_since=&weekly_since=&monthly_since=
                                                      pre-aggregated chart data
"""

import bisect
import logging

from flask import Blueprint
from flask import jsonify
from flask import request
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.analytics import compute_analytics
from src.database import session_scope
from src.models import UsageRecord
from src.models import parse_timestamp

logger = logging.getLogger(__name__)

bp = Blueprint("api", __name__)

# Keep `IN (...)` lookups under SQLite's bound-variable ceiling on big first syncs.
_LOOKUP_CHUNK = 500


def _chunked(seq: list, size: int):
    for start in range(0, len(seq), size):
        yield seq[start : start + size]


@bp.get("/status")
def health():
    return jsonify({"status": "ok"})


@bp.post("/api/records")
def post_records():
    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        logger.warning("POST /api/records: rejected non-array body (type=%s)", type(payload).__name__)
        return jsonify({"error": "expected a JSON array of records"}), 400

    logger.debug("POST /api/records: received batch of %d", len(payload))

    # Deduplicate within the batch; last write wins for a repeated uuid.
    rows = {item["uuid"]: UsageRecord.row_from_json(item) for item in payload}
    if not rows:
        return jsonify({"inserted": 0, "skipped": 0})

    uuids = list(rows.keys())
    with session_scope() as session:
        # `existing` is the subset of this batch already stored, so it is exactly
        # the rows the upsert will skip. Chunk the lookup to stay under SQLite's
        # bound-variable limit on large first syncs.
        existing = set()
        for chunk in _chunked(uuids, _LOOKUP_CHUNK):
            existing.update(
                session.scalars(select(UsageRecord.uuid).where(UsageRecord.uuid.in_(chunk))).all()
            )

        # executemany form (params as a list) — one statement run per row, so the
        # whole batch never becomes a single oversized INSERT.
        stmt = sqlite_insert(UsageRecord).on_conflict_do_nothing(index_elements=["uuid"])
        session.execute(stmt, list(rows.values()))

    skipped = len(existing)
    inserted = len(uuids) - skipped
    logger.info("POST /api/records: inserted=%d skipped=%d", inserted, skipped)
    return jsonify({"inserted": inserted, "skipped": skipped})


@bp.get("/api/records")
def get_records():
    since_raw = request.args.get("since")
    stmt = select(UsageRecord).order_by(UsageRecord.timestamp)
    if since_raw:
        try:
            stmt = stmt.where(UsageRecord.timestamp >= parse_timestamp(since_raw))
        except ValueError:
            return jsonify({"error": f"invalid 'since' timestamp: {since_raw}"}), 400

    with session_scope() as session:
        records = session.scalars(stmt).all()
        logger.info("GET /api/records: since=%s returned=%d", since_raw, len(records))
        return jsonify([r.to_json() for r in records])


@bp.get("/api/analytics")
def get_analytics():
    """Return pre-aggregated chart data covering session, weekly, and monthly windows.

    Query params (all ISO8601):
        session_since   — start of the 5-hour session window
        weekly_since    — start of the 7-day weekly window
        monthly_since   — start of the 30-day monthly window
    """
    keys = ("session_since", "weekly_since", "monthly_since")
    raw = [request.args.get(k) for k in keys]
    if not all(raw):
        return jsonify({"error": "session_since, weekly_since, and monthly_since are required"}), 400
    try:
        # Strip tzinfo: SQLite/SQLAlchemy stores naive UTC datetimes.
        session_cutoff, weekly_cutoff, monthly_cutoff = [parse_timestamp(v).replace(tzinfo=None) for v in raw]
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with session_scope() as db:
        all_records = db.scalars(
            select(UsageRecord).order_by(UsageRecord.timestamp)
        ).all()

    # Records are sorted by timestamp; bisect avoids multiple O(n) linear scans.
    timestamps = [r.timestamp for r in all_records]
    monthly_records = all_records[bisect.bisect_left(timestamps, monthly_cutoff) :]
    weekly_records  = all_records[bisect.bisect_left(timestamps, weekly_cutoff) :]
    session_records = all_records[bisect.bisect_left(timestamps, session_cutoff) :]

    logger.info(
        "GET /api/analytics: session=%d weekly=%d monthly=%d all=%d records",
        len(session_records),
        len(weekly_records),
        len(monthly_records),
        len(all_records),
    )
    return jsonify(
        compute_analytics(session_records, weekly_records, monthly_records, all_records, session_cutoff, weekly_cutoff)
    )
