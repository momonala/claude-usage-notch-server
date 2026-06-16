"""HTTP routes.

GET  /status                                          liveness check
POST /api/records                                     upsert a batch of records (idempotent by uuid)
GET  /api/records?since=ISO                           records with timestamp >= since
POST /api/quota_snapshots                             upsert a batch of polled quota readings
                                                      (idempotent by window_type+timestamp)
GET  /api/quota_snapshots?window_type=&since=ISO       quota readings with timestamp >= since
GET  /api/analytics?session_since=&weekly_since=&month_since=&lookback_since=&granularity=
                                                      pre-aggregated chart data
"""

import bisect
import logging
from datetime import datetime
from datetime import timezone

from flask import Blueprint
from flask import jsonify
from flask import request
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.analytics import compute_analytics
from src.analytics import estimated_cost_fields
from src.database import session_scope
from src.models import QuotaSnapshot
from src.models import UsageRecord
from src.models import UsageStats
from src.models import parse_timestamp

logger = logging.getLogger(__name__)

bp = Blueprint("api", __name__)


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
        existing_uuids = {
            row[0] for row in session.execute(select(UsageRecord.uuid).where(UsageRecord.uuid.in_(uuids)))
        }
        new_row_data = [data for uuid, data in rows.items() if uuid not in existing_uuids]

        stmt = sqlite_insert(UsageRecord).on_conflict_do_nothing(index_elements=["uuid"])
        result = session.connection().execute(stmt, list(rows.values()))

        if new_row_data:
            cost_delta = sum(
                estimated_cost_fields(
                    d["model"],
                    d["input_tokens"],
                    d["cache_creation_tokens"],
                    d["output_tokens"],
                    d["cache_read_tokens"],
                )
                for d in new_row_data
            )
            stats = session.get(UsageStats, 1)
            if stats is not None:
                stats.lifetime_cost += cost_delta
                stats.last_updated = datetime.now(timezone.utc)

    # SQLite sets rowcount to actual rows inserted (skipped rows don't count).
    inserted = result.rowcount if result.rowcount >= 0 else len(uuids)
    skipped = len(uuids) - inserted
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


@bp.post("/api/quota_snapshots")
def post_quota_snapshots():
    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        logger.warning("POST /api/quota_snapshots: rejected non-array body (type=%s)", type(payload).__name__)
        return jsonify({"error": "expected a JSON array of records"}), 400

    # Keyed by (window_type, timestamp) — the table's natural composite key — so a
    # repeated POST (retry, or another laptop polling the same account a moment
    # apart) is a no-op rather than a duplicate row.
    rows: dict[tuple[str, datetime], dict] = {}
    for item in payload:
        if not isinstance(item, dict) or not item.get("window_type") or not item.get("timestamp"):
            logger.warning("POST /api/quota_snapshots: skipping record missing window_type/timestamp")
            continue
        try:
            data = QuotaSnapshot.row_from_json(item)
        except ValueError:
            logger.warning("POST /api/quota_snapshots: skipping record with invalid timestamp")
            continue
        rows[(data["window_type"], data["timestamp"])] = data

    if not rows:
        return jsonify({"inserted": 0, "skipped": 0})

    with session_scope() as session:
        stmt = sqlite_insert(QuotaSnapshot).on_conflict_do_nothing(
            index_elements=["window_type", "timestamp"]
        )
        result = session.connection().execute(stmt, list(rows.values()))

    inserted = result.rowcount if result.rowcount >= 0 else len(rows)
    skipped = len(rows) - inserted
    logger.info("POST /api/quota_snapshots: inserted=%d skipped=%d", inserted, skipped)
    return jsonify({"inserted": inserted, "skipped": skipped})


@bp.get("/api/quota_snapshots")
def get_quota_snapshots():
    since_raw = request.args.get("since")
    window_type = request.args.get("window_type")
    stmt = select(QuotaSnapshot).order_by(QuotaSnapshot.timestamp)
    if window_type:
        stmt = stmt.where(QuotaSnapshot.window_type == window_type)
    if since_raw:
        try:
            stmt = stmt.where(QuotaSnapshot.timestamp >= parse_timestamp(since_raw))
        except ValueError:
            return jsonify({"error": f"invalid 'since' timestamp: {since_raw}"}), 400

    with session_scope() as session:
        records = session.scalars(stmt).all()
        logger.info(
            "GET /api/quota_snapshots: window_type=%s since=%s returned=%d",
            window_type,
            since_raw,
            len(records),
        )
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
        stats = db.get(UsageStats, 1)
        lifetime_cost = stats.lifetime_cost if stats is not None else 0.0

        # Real polled quota readings (ground truth) for the same spans as the
        # session/weekly token buckets below. Empty until a client has pushed at
        # least one reading — the app falls back to its token-based estimate then.
        session_quota_records = db.scalars(
            select(QuotaSnapshot)
            .where(QuotaSnapshot.window_type == "session", QuotaSnapshot.timestamp >= session_cutoff)
            .order_by(QuotaSnapshot.timestamp)
        ).all()
        weekly_quota_records = db.scalars(
            select(QuotaSnapshot)
            .where(QuotaSnapshot.window_type == "weekly", QuotaSnapshot.timestamp >= weekly_cutoff)
            .order_by(QuotaSnapshot.timestamp)
        ).all()

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
            session_quota_records,
            weekly_quota_records,
        )
    )
