"""Test schema migration v13: calendar support."""

import sqlite3
import tempfile
from pathlib import Path

from src.store import schema


def test_migration_v13_creates_calendar_tables():
    """Verify migration v13 creates all calendar tables, columns, and indexes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        # Create minimal schema (v12 state) — decisions and action_items must exist
        # for the ALTER TABLE operations
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

        # Run migration
        schema.migrate_add_calendar(conn)

        # Verify calendar_events table
        cursor = conn.execute("PRAGMA table_info(calendar_events)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        assert "id" in columns
        assert "outlook_event_id" in columns
        assert "subject" in columns
        assert "body" in columns
        assert "body_summary" in columns
        assert "organizer_email" in columns
        assert "organizer_name" in columns
        assert "start_at" in columns
        assert "end_at" in columns
        assert "location" in columns
        assert "is_recurring" in columns
        assert "recurrence_master_id" in columns
        assert "response_status" in columns
        assert "is_self_organized" in columns
        assert "is_cancelled" in columns
        assert "created_at" in columns
        assert "modified_at" in columns
        assert "body_extracted_at" in columns
        assert "ingested_at" in columns

        # Verify event_attendees table
        cursor = conn.execute("PRAGMA table_info(event_attendees)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        assert "id" in columns
        assert "event_id" in columns
        assert "person_id" in columns
        assert "email" in columns
        assert "name" in columns
        assert "response_status" in columns
        assert "is_organizer" in columns
        assert "is_self" in columns

        # Verify ALTER TABLE columns
        cursor = conn.execute("PRAGMA table_info(decisions)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "event_id" in columns

        cursor = conn.execute("PRAGMA table_info(action_items)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "event_id" in columns

        # Verify indexes
        indexes = {
            row[1]
            for row in conn.execute("SELECT * FROM sqlite_master WHERE type='index'").fetchall()
        }

        assert "idx_calendar_start" in indexes
        assert "idx_calendar_master" in indexes
        assert "idx_attendees_event" in indexes
        assert "idx_attendees_person" in indexes
        assert "idx_attendees_email" in indexes
        assert "idx_decisions_event" in indexes
        assert "idx_actions_event" in indexes

        # Verify FTS5 table
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "calendar_events_fts" in tables

        # Verify triggers exist
        triggers = {
            row[1]
            for row in conn.execute("SELECT * FROM sqlite_master WHERE type='trigger'").fetchall()
        }

        assert "calendar_events_ai" in triggers
        assert "calendar_events_ad" in triggers
        assert "calendar_events_au" in triggers

        conn.close()


def test_migration_v13_idempotent():
    """Verify migration v13 can be run multiple times without crashing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")

        # Create minimal schema with decisions/action_items
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

        # Run migration twice
        schema.migrate_add_calendar(conn)
        schema.migrate_add_calendar(conn)  # Should not crash

        # Verify tables still exist
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        assert "calendar_events" in tables
        assert "event_attendees" in tables
        assert "calendar_events_fts" in tables

        conn.close()
