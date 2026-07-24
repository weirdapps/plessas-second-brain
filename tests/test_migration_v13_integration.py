"""Integration test for schema migration v13."""

import sqlite3
import tempfile
from pathlib import Path

from src.store import schema


def test_run_migrations_v12_to_v13():
    """Test migrating from v12 to v13 through run_migrations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

        # Create a v12 database state
        conn.execute("""
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY,
                email_id INTEGER,
                decision TEXT NOT NULL,
                decided_by TEXT,
                decision_date TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE action_items (
                id INTEGER PRIMARY KEY,
                email_id INTEGER,
                task TEXT NOT NULL,
                owner TEXT,
                deadline TEXT,
                status TEXT DEFAULT 'open'
            )
        """)
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (12)")
        conn.commit()

        # Verify current version is 12
        assert schema.get_schema_version(conn) == 12

        # Run migrations
        schema.run_migrations(conn)

        # Verify version bumped to the current schema version (migrations run forward
        # through every pending step, not just v13).
        assert schema.get_schema_version(conn) == schema.CURRENT_SCHEMA_VERSION

        # Verify calendar tables exist
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        assert "calendar_events" in tables
        assert "event_attendees" in tables
        assert "calendar_events_fts" in tables
        # v14 also ran: commitments table exists.
        assert "commitments" in tables

        # Verify event_id column added to decisions and action_items
        decisions_cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        assert "event_id" in decisions_cols

        action_cols = {row[1] for row in conn.execute("PRAGMA table_info(action_items)").fetchall()}
        assert "event_id" in action_cols

        conn.close()
