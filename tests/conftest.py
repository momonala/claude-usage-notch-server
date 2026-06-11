"""Point the server at a throwaway SQLite DB for each test.

Config (including DB_PATH) is baked in from pyproject.toml at import time, so tests
swap out the engine/session in `src.database` rather than overriding an env var.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database as database
from src.models import Base


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False, future=True)
    )
    Base.metadata.create_all(engine)
    yield
