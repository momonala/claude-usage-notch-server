"""HTTP routes.

GET  /status                                          liveness check
POST /api/records                                     upsert a batch of records (idempotent by uuid)
GET  /api/records?since=ISO                           records with timestamp >= since
POST /api/quota_snapshots                             upsert a batch of polled quota readings
                                                      (idempotent by window_type+timestamp)
GET  /api/quota_snapshots?window_type=&since=ISO       quota readings with timestamp >= since
GET  /api/analytics?session_since=&session_cost_since=&weekly_since=&month_since=&lookback_since=&granularity=
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

from src.analytics import AnalyticsRequest
from src.analytics import compute_analytics
from src.analytics import estimated_cost_fields
from src.database import session_scope
from src.models import QuotaSnapshot
from src.models import UsageRecord
from src.models import UsageStats
from src.models import parse_timestamp
from src.telemetry import metrics

logger = logging.getLogger(__name__)

bp = Blueprint("api", __name__)


def _filter_since(stmt, column, since_raw: str | None):
    """Narrow `stmt` to rows at or after `since_raw`. Raises ValueError if unparseable."""
    if not since_raw:
        return stmt
    return stmt.where(column >= parse_timestamp(since_raw))


def _json_array(endpoint: str):
    """The POST body as a list, or None if it isn't a JSON array."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        logger.warning("%s: rejected non-array body (type=%s)", endpoint, type(payload).__name__)
        return None
    return payload


def _insert_counts(result, submitted: int) -> dict:
    """SQLite reports rows actually inserted, so skipped rows are the remainder."""
    inserted = result.rowcount if result.rowcount >= 0 else submitted
    return {"inserted": inserted, "skipped": submitted - inserted}


@bp.get("/status")
def health():
    return jsonify({"status": "ok"})


@bp.post("/api/records")
def post_records():
    payload = _json_array("POST /api/records")
    if payload is None:
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

    counts = _insert_counts(result, len(uuids))
    logger.info("POST /api/records: inserted=%d skipped=%d", counts["inserted"], counts["skipped"])
    metrics.increment("records_inserted", counts["inserted"])
    return jsonify(counts)


@bp.get("/api/records")
def get_records():
    since_raw = request.args.get("since")
    try:
        stmt = _filter_since(
            select(UsageRecord).order_by(UsageRecord.timestamp), UsageRecord.timestamp, since_raw
        )
    except ValueError:
        return jsonify({"error": f"invalid 'since' timestamp: {since_raw}"}), 400

    with session_scope() as session:
        records = session.scalars(stmt).all()
        logger.info("GET /api/records: since=%s returned=%d", since_raw, len(records))
        return jsonify([r.to_json() for r in records])


@bp.post("/api/quota_snapshots")
def post_quota_snapshots():
    payload = _json_array("POST /api/quota_snapshots")
    if payload is None:
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

    counts = _insert_counts(result, len(rows))
    logger.debug("POST /api/quota_snapshots: inserted=%d skipped=%d", counts["inserted"], counts["skipped"])
    metrics.increment("quota_snapshots_inserted", counts["inserted"])
    return jsonify(counts)


@bp.get("/api/quota_snapshots")
def get_quota_snapshots():
    since_raw = request.args.get("since")
    window_type = request.args.get("window_type")
    stmt = select(QuotaSnapshot).order_by(QuotaSnapshot.timestamp)
    if window_type:
        stmt = stmt.where(QuotaSnapshot.window_type == window_type)
    try:
        stmt = _filter_since(stmt, QuotaSnapshot.timestamp, since_raw)
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
        session_since      — start of the wide 24h fetch range backing the session_buckets
                              chart (so the 5h rolling window can be seen resetting
                              several times across the span); not the "Session" cost figure
        session_cost_since — start of the *actual* current 5-hour session; drives the
                              "Session" cost figure exclusively
        weekly_since        — start of the 7-day weekly window
        month_since         — start of the trailing 30-day window (the "Month" figure)
        lookback_since      — start of the user-selected lookback; drives the breakdowns
                              and spend/sessions series, independent of the fixed windows above
        granularity         — spend/sessions bucket width: "hour" (1D), "day" (7D/30D),
                              or "month" (All). Optional; defaults to "day".
    """
    keys = ("session_since", "session_cost_since", "weekly_since", "month_since", "lookback_since")
    raw = [request.args.get(k) for k in keys]
    missing = [k for k, v in zip(keys, raw) if not v]
    if missing:
        return jsonify({"error": f"missing required params: {', '.join(missing)}"}), 400
    granularity = request.args.get("granularity", "day")
    if granularity not in ("hour", "day", "month"):
        return jsonify({"error": f"invalid granularity: {granularity}"}), 400
    try:
        # Strip tzinfo: SQLite/SQLAlchemy stores naive UTC datetimes.
        session_cutoff, session_cost_cutoff, weekly_cutoff, month_cutoff, lookback_cutoff = [
            parse_timestamp(v).replace(tzinfo=None) for v in raw
        ]
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    # Fetch enough history to cover both the lookback window and the fixed 30-day
    # month — the lookback can be shorter (7D) or longer (All) than a month.
    fetch_cutoff = min(lookback_cutoff, month_cutoff)

    with metrics.timed("duration"):
        with session_scope() as db:
            windowed = db.scalars(
                select(UsageRecord)
                .where(UsageRecord.timestamp >= fetch_cutoff)
                .order_by(UsageRecord.timestamp)
            ).all()
            stats = db.get(UsageStats, 1)
            lifetime_cost = stats.lifetime_cost if stats is not None else 0.0

            # Real polled quota readings (ground truth) over the same spans as
            # the session/weekly token buckets. Empty until a client has pushed
            # at least one reading — the app estimates from tokens until then.
            def quota_since(window_type: str, cutoff: datetime) -> list[QuotaSnapshot]:
                return list(
                    db.scalars(
                        select(QuotaSnapshot)
                        .where(QuotaSnapshot.window_type == window_type, QuotaSnapshot.timestamp >= cutoff)
                        .order_by(QuotaSnapshot.timestamp)
                    ).all()
                )

            session_quota = quota_since("session", session_cutoff)
            weekly_quota = quota_since("weekly", weekly_cutoff)
            month_quota = quota_since("monthly", month_cutoff)

        # `windowed` is ordered by timestamp, so each window is a suffix of it;
        # bisect finds the boundary without a linear scan per window.
        timestamps = [r.timestamp for r in windowed]

        def records_since(cutoff: datetime) -> list[UsageRecord]:
            return list(windowed[bisect.bisect_left(timestamps, cutoff) :])

        request_data = AnalyticsRequest(
            granularity=granularity,
            lifetime_cost=lifetime_cost,
            session_records=records_since(session_cutoff),
            session_cost_records=records_since(session_cost_cutoff),
            weekly_records=records_since(weekly_cutoff),
            month_records=records_since(month_cutoff),
            lookback_records=records_since(lookback_cutoff),
            session_cutoff=session_cutoff,
            weekly_cutoff=weekly_cutoff,
            lookback_cutoff=lookback_cutoff,
            session_quota=session_quota,
            weekly_quota=weekly_quota,
            month_quota=month_quota,
        )
        result = compute_analytics(request_data)

    logger.info(
        "GET /api/analytics: session=%d session_cost=%d weekly=%d month=%d lookback=%d records",
        len(request_data.session_records),
        len(request_data.session_cost_records),
        len(request_data.weekly_records),
        len(request_data.month_records),
        len(request_data.lookback_records),
    )
    return jsonify(result)
