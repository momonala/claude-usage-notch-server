"""SQLAlchemy engine and session setup.

SQLite store at `DB_PATH` (see src.env). Schema is created on demand via
`init_db()` — the model is a single table with no migration history to worry
about, so `create_all` is sufficient.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from src.config import DATABASE_URL
from src.models import Base

engine = create_engine(DATABASE_URL, future=True, connect_args={"check_same_thread": False})


@sqlalchemy.event.listens_for(engine, "connect")
def _set_pragmas(dbapi_conn, _connection_record):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables if they don't exist."""
    Base.metadata.create_all(engine)


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
