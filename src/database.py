"""SQLAlchemy engine and session setup.

SQLite store at `DB_PATH` (see src.env). Schema is created on demand via
`init_db()` — the model is a single table with no migration history to worry
about, so `create_all` is sufficient.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from src.config import DATABASE_URL
from src.models import Base
from src.models import UsageRecord
from src.models import UsageStats

engine = create_engine(DATABASE_URL, future=True, connect_args={"check_same_thread": False})


@sqlalchemy.event.listens_for(engine, "connect")
def _set_pragmas(dbapi_conn, _connection_record):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables if they don't exist."""
    Base.metadata.create_all(engine)
    _ensure_usage_stats()


def _ensure_usage_stats() -> None:
    from src.analytics import estimated_cost_fields

    with session_scope() as session:
        if session.get(UsageStats, 1) is not None:
            return
        rows = session.execute(
            select(
                UsageRecord.model,
                UsageRecord.input_tokens,
                UsageRecord.cache_creation_tokens,
                UsageRecord.output_tokens,
                UsageRecord.cache_read_tokens,
            )
        ).all()
        lifetime_cost = sum(estimated_cost_fields(*row) for row in rows)
        session.add(UsageStats(id=1, lifetime_cost=lifetime_cost))


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
