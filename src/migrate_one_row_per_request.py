"""One-time migration: one usage_records row per request, slimmer quota_snapshots.

Run once on the Pi with the service stopped, after copying the DB off the Pi:
    uv run python -m src.migrate_one_row_per_request [db_path]
"""

import sqlite3
import sys
from datetime import UTC
from datetime import datetime

from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex
from sqlalchemy.schema import CreateTable

from src.analytics import estimated_cost_fields
from src.analytics import quantize_reset
from src.config import DB_PATH
from src.models import QuotaSnapshot
from src.models import UsageRecord
from src.routes import QUOTA_HEARTBEAT_INTERVAL

_USAGE_COLUMNS = [c.name for c in UsageRecord.__table__.columns]


def _create_statements(table) -> list[str]:
    dialect = sqlite_dialect.dialect()
    return [str(CreateTable(table).compile(dialect=dialect))] + [
        str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes
    ]


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _thinned_quota(rows: list[tuple]) -> list[tuple]:
    """Apply the live store-on-change rule (see routes._claim_quota_row) to history."""
    last: dict[str, tuple[tuple, datetime]] = {}
    kept = []
    for window_type, timestamp, percent_used, resets_at in rows:
        at = _parse(timestamp)
        reset = _parse(resets_at)
        signature = (percent_used, quantize_reset(reset) if reset else None)
        previous = last.get(window_type)
        if previous is None or previous[0] != signature or at >= previous[1] + QUOTA_HEARTBEAT_INTERVAL:
            last[window_type] = (signature, at)
            kept.append((window_type, timestamp, percent_used, resets_at))
    return kept


def migrate(db_path: str) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    old_columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_records)")}
    if "uuid" not in old_columns:
        print(f"{db_path}: already migrated")
        return

    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DROP INDEX IF EXISTS idx_usage_records_timestamp")
    conn.execute("DROP INDEX IF EXISTS idx_quota_snapshots_timestamp")
    conn.execute("ALTER TABLE usage_records RENAME TO usage_records_old")
    conn.execute("ALTER TABLE quota_snapshots RENAME TO quota_snapshots_old")
    for statement in _create_statements(UsageRecord.__table__) + _create_statements(QuotaSnapshot.__table__):
        conn.execute(statement)

    selected = ", ".join("first_seen" if c == "timestamp" else c for c in _USAGE_COLUMNS)
    conn.execute(
        f"""
        INSERT INTO usage_records ({", ".join(_USAGE_COLUMNS)})
        SELECT {selected} FROM (
            SELECT *,
                   min(timestamp) OVER (PARTITION BY request_id) AS first_seen,
                   row_number() OVER (
                       PARTITION BY request_id ORDER BY output_tokens DESC, timestamp DESC
                   ) AS rn
            FROM usage_records_old
            WHERE request_id IS NOT NULL AND request_id != ''
        )
        WHERE rn = 1
        ORDER BY first_seen
        """
    )

    quota_old = conn.execute(
        "SELECT window_type, timestamp, percent_used, resets_at FROM quota_snapshots_old ORDER BY timestamp"
    ).fetchall()
    quota_new = _thinned_quota(quota_old)
    conn.executemany(
        "INSERT INTO quota_snapshots (window_type, timestamp, percent_used, resets_at) VALUES (?, ?, ?, ?)",
        quota_new,
    )

    expected_requests, expected_output = conn.execute(
        """
        SELECT count(*), coalesce(sum(max_output), 0) FROM (
            SELECT max(output_tokens) AS max_output FROM usage_records_old
            WHERE request_id IS NOT NULL AND request_id != '' GROUP BY request_id
        )
        """
    ).fetchone()
    actual_requests, actual_output = conn.execute(
        "SELECT count(*), coalesce(sum(output_tokens), 0) FROM usage_records"
    ).fetchone()
    old_windows = {row[0] for row in quota_old}
    new_windows = {row[0] for row in conn.execute("SELECT DISTINCT window_type FROM quota_snapshots")}
    if (actual_requests, actual_output) != (expected_requests, expected_output) or old_windows != new_windows:
        conn.execute("ROLLBACK")
        raise SystemExit(
            f"check failed, rolled back: requests {actual_requests}/{expected_requests}, "
            f"output {actual_output}/{expected_output}, windows {sorted(new_windows)}/{sorted(old_windows)}"
        )

    old_cost = conn.execute("SELECT lifetime_cost FROM usage_stats WHERE id = 1").fetchone()
    lifetime_cost = sum(
        estimated_cost_fields(*row)
        for row in conn.execute(
            "SELECT model, input_tokens, cache_creation_tokens, output_tokens, cache_read_tokens FROM usage_records"
        )
    )
    conn.execute(
        "INSERT OR REPLACE INTO usage_stats (id, lifetime_cost, last_updated) VALUES (1, ?, ?)",
        (lifetime_cost, str(datetime.now(UTC).replace(tzinfo=None))),
    )
    old_rows = conn.execute("SELECT count(*) FROM usage_records_old").fetchone()[0]
    conn.execute("DROP TABLE usage_records_old")
    conn.execute("DROP TABLE quota_snapshots_old")
    conn.execute("COMMIT")
    conn.execute("VACUUM")
    conn.close()

    print(f"usage_records: {old_rows} lines -> {actual_requests} requests")
    print(f"quota_snapshots: {len(quota_old)} -> {len(quota_new)} readings")
    print(f"lifetime_cost: {old_cost[0] if old_cost else None} -> {lifetime_cost:.2f}")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
