"""Tests for the one-time migration to one usage_records row per request."""

import sqlite3

import pytest

from src.analytics import estimated_cost_fields
from src.migrate_one_row_per_request import migrate

_OLD_SCHEMA = """
CREATE TABLE usage_records (
    uuid VARCHAR NOT NULL, request_id VARCHAR, session_id VARCHAR NOT NULL, parent_uuid VARCHAR,
    timestamp DATETIME NOT NULL, cwd VARCHAR NOT NULL, project VARCHAR NOT NULL, git_branch VARCHAR,
    model VARCHAR NOT NULL, version VARCHAR, entrypoint VARCHAR, attribution_skill VARCHAR,
    is_sidechain BOOLEAN NOT NULL, stop_reason VARCHAR, service_tier VARCHAR,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cache_creation_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL, ephemeral_1h_tokens INTEGER NOT NULL,
    ephemeral_5m_tokens INTEGER NOT NULL, web_searches INTEGER NOT NULL, web_fetches INTEGER NOT NULL,
    ingested_at DATETIME NOT NULL, PRIMARY KEY (uuid)
);
CREATE INDEX idx_usage_records_timestamp ON usage_records (timestamp);
CREATE TABLE usage_stats (
    id INTEGER NOT NULL, lifetime_cost FLOAT NOT NULL, last_updated DATETIME NOT NULL, PRIMARY KEY (id)
);
CREATE TABLE quota_snapshots (
    window_type VARCHAR NOT NULL, timestamp DATETIME NOT NULL, percent_used FLOAT NOT NULL,
    resets_at DATETIME, source VARCHAR, ingested_at DATETIME NOT NULL, PRIMARY KEY (window_type, timestamp)
);
CREATE INDEX idx_quota_snapshots_timestamp ON quota_snapshots (timestamp);
"""


def _line(uuid, request_id, timestamp, output_tokens):
    return (
        uuid, request_id, "s1", None, timestamp, "/code/proj", "proj", "main", "claude-opus-5-5", "2.1",
        "cli", None, 0, "tool_use", "standard", 10, output_tokens, 100, 1000, 100, 0, 0, 0, timestamp,
    )  # fmt: skip


def _quota(timestamp, percent_used, resets_at="2026-06-16 17:00:00.000000"):
    return ("session", timestamp, percent_used, resets_at, "laptop", timestamp)


@pytest.fixture
def old_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.executemany(
        f"INSERT INTO usage_records VALUES ({','.join('?' * 24)})",
        [
            _line("u1", "req_a", "2026-06-09 12:00:00.000000", 3),
            _line("u2", "req_a", "2026-06-09 12:00:40.000000", 153),
            _line("u3", "req_b", "2026-06-09 13:00:00.000000", 50),
            _line("u4", None, "2026-06-09 14:00:00.000000", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO quota_snapshots VALUES (?, ?, ?, ?, ?, ?)",
        [
            _quota("2026-06-16 12:00:00.000000", 0.4),
            _quota("2026-06-16 12:01:00.000000", 0.4),
            _quota("2026-06-16 12:02:00.000000", 0.4, resets_at="2026-06-16 17:00:20.000000"),
            _quota("2026-06-16 12:03:00.000000", 0.5),
            _quota("2026-06-16 12:13:00.000000", 0.5),
        ],
    )
    conn.execute("INSERT INTO usage_stats VALUES (1, 999.0, '2026-06-09 14:00:00.000000')")
    conn.commit()
    conn.close()
    return path


def test_migration_collapses_requests_thins_quota_and_recomputes_cost(old_db):
    migrate(str(old_db))

    conn = sqlite3.connect(old_db)
    usage = conn.execute(
        "SELECT request_id, timestamp, output_tokens FROM usage_records ORDER BY timestamp"
    ).fetchall()
    assert usage == [
        ("req_a", "2026-06-09 12:00:00.000000", 153),
        ("req_b", "2026-06-09 13:00:00.000000", 50),
    ]
    assert conn.execute("SELECT timestamp FROM quota_snapshots ORDER BY timestamp").fetchall() == [
        ("2026-06-16 12:00:00.000000",),
        ("2026-06-16 12:03:00.000000",),
        ("2026-06-16 12:13:00.000000",),
    ]
    expected_cost = sum(estimated_cost_fields("claude-opus-5-5", 10, 100, out, 1000) for out in (153, 50))
    assert conn.execute("SELECT lifetime_cost FROM usage_stats").fetchone()[0] == pytest.approx(expected_cost)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"usage_records", "quota_snapshots", "usage_stats"}


def test_migration_is_a_no_op_on_a_migrated_db(old_db, capsys):
    migrate(str(old_db))
    migrate(str(old_db))
    assert "already migrated" in capsys.readouterr().out
