"""Shared fixtures for teams ingest tests."""

import json
import sqlite3
from pathlib import Path

import pytest

from src.store.schema import (
    create_database,
    migrate_add_teams,
    migrate_add_teams_last_pulled_at,
    run_migrations,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "teams"


@pytest.fixture
def db(tmp_path):
    """In-memory-ish DB at a tmp path with all migrations applied."""
    db_path = str(tmp_path / "test.db")
    conn = create_database(db_path)
    conn.close()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    # create_database stamps schema_version to CURRENT, so run_migrations is a
    # no-op for fresh DBs. Apply the teams migration directly so the teams_*
    # tables exist for tests. (The migration itself is idempotent.)
    migrate_add_teams(conn)
    # v18 adds teams_chats.last_pulled_at. Applied here for the same reason as
    # migrate_add_teams: run_migrations above is a no-op on a freshly stamped DB.
    migrate_add_teams_last_pulled_at(conn)
    yield conn
    conn.close()


@pytest.fixture
def fixture_loader():
    """Return a function that loads JSON fixture files by name."""

    def _load(name: str) -> dict | list:
        path = FIXTURES_DIR / name
        with open(path) as f:
            return json.load(f)

    return _load
