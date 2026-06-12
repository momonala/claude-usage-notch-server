"""HTTP routes.

GET  /status                                          liveness check
POST /api/records                                     upsert a batch of records (idempotent by uuid)
GET  /api/records?since=ISO                           records with timestamp >= since
GET  /api/analytics?session_since=&weekly_since=&month_since=&lookback_since=&granularity=
                                                      pre-aggregated chart data
"""

import bisect
import logging
from collections.abc import Iterator

from flask import Blueprint
from flask import jsonify
from flask import request
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.analytics import compute_analytics
from src.analytics import estimated_cost_fields
from src.database import session_scope
from src.models import UsageRecord
from src.models import parse_timestamp

logger = logging.getLogger(__name__)

bp = Blueprint("api", __name__)

# Keep `IN (...)` lookups under SQLite's bound-variable ceiling on big first syncs.
_LOOKUP_CHUNK = 500


def _chunked(seq: list[str], size: int) -> Iterator[list[str]]:
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
    # Skip malformed records missing a uuid rather than aborting the whole batch.
    rows = {}
    for item in payload:
        if not isinstance(item, dict) or not item.get("uuid"):
            logger.warning("POST /api/records: skipping record with missing uuid")
            continue
        rows[item["uuid"]] = UsageRecord.row_from_json(item)
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

    # Best-effort counts: with ON CONFLICT DO NOTHING, the pre-existing rows are
    # exactly the skipped ones and the rest insert, so this is derived rather than
    # read back from the statement's rowcount.
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
    """Return pre-aggregated chart data covering session, weekly, month, and lookback windows.

    Query params (all ISO8601):
        session_since   — start of the 5-hour session window
        weekly_since    — start of the 7-day weekly window
        month_since     — start of the trailing 30-day window (the "Month" figure)
        lookback_since  — start of the user-selected lookback; drives the breakdowns
                          and spend/sessions series, independent of the fixed windows above
        granularity     — spend/sessions bucket width: "hour" (1D), "day" (7D/30D),
                          or "month" (All). Optional; defaults to "day".
    """
    keys = ("session_since", "weekly_since", "month_since", "lookback_since")
    raw = [request.args.get(k) for k in keys]
    missing = [k for k, v in zip(keys, raw) if not v]
    if missing:
        return jsonify({"error": f"missing required params: {', '.join(missing)}"}), 400
    granularity = request.args.get("granularity", "day")
    if granularity not in ("hour", "day", "month"):
        return jsonify({"error": f"invalid granularity: {granularity}"}), 400
    try:
        # Strip tzinfo: SQLite/SQLAlchemy stores naive UTC datetimes.
        session_cutoff, weekly_cutoff, month_cutoff, lookback_cutoff = [
            parse_timestamp(v).replace(tzinfo=None) for v in raw
        ]
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Fetch enough history to cover both the lookback window and the fixed 30-day
    # month — the lookback can be shorter (7D) or longer (All) than a month.
    fetch_cutoff = min(lookback_cutoff, month_cutoff)

    with session_scope() as db:
        windowed = db.scalars(
            select(UsageRecord).where(UsageRecord.timestamp >= fetch_cutoff).order_by(UsageRecord.timestamp)
        ).all()
        # Lifetime cost is the only figure needing the full history; pull just the
        # cost columns rather than whole ORM rows.
        lifetime_cost = sum(
            estimated_cost_fields(*row)
            for row in db.execute(
                select(
                    UsageRecord.model,
                    UsageRecord.input_tokens,
                    UsageRecord.cache_creation_tokens,
                    UsageRecord.output_tokens,
                    UsageRecord.cache_read_tokens,
                )
            ).all()
        )

    # Records are sorted by timestamp; bisect avoids multiple O(n) linear scans.
    timestamps = [r.timestamp for r in windowed]
    lookback_records = windowed[bisect.bisect_left(timestamps, lookback_cutoff) :]
    month_records = windowed[bisect.bisect_left(timestamps, month_cutoff) :]
    weekly_records = windowed[bisect.bisect_left(timestamps, weekly_cutoff) :]
    session_records = windowed[bisect.bisect_left(timestamps, session_cutoff) :]

    logger.info(
        "GET /api/analytics: session=%d weekly=%d month=%d lookback=%d records",
        len(session_records),
        len(weekly_records),
        len(month_records),
        len(lookback_records),
    )
    return jsonify(
        compute_analytics(
            session_records,
            weekly_records,
            month_records,
            lookback_records,
            lifetime_cost,
            session_cutoff,
            weekly_cutoff,
            lookback_cutoff,
            granularity,
        )
    )
