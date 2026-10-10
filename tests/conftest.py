"""Shared database fixtures for persistence unit tests."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from opsmind_storage.models import Base
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    """Provide an isolated SQLite session with foreign key constraints enabled."""
    engine = create_engine(f"sqlite:///{tmp_path / 'opsmind.db'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: sqlite3.Connection, _: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()
