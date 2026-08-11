"""Test schema migration v13: calendar support."""

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

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


def _v16_calendar_db(conn: sqlite3.Connection) -> None:
    """A database that already has calendar tables but not yet llm_status."""
    # get_schema_version reads row["version"], so run_migrations needs a Row factory.
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY, email_id INTEGER, decision TEXT NOT NULL,
            decided_by TEXT, decision_date TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE action_items (
            id INTEGER PRIMARY KEY, email_id INTEGER, task TEXT NOT NULL,
            owner TEXT, deadline TEXT, status TEXT DEFAULT 'open'
        )
    """)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version (version) VALUES (12)")
    conn.commit()
    schema.migrate_add_calendar(conn)
    schema.set_schema_version(conn, 16)


def test_migration_v17_backfills_llm_status_without_re_offering_the_whole_history():
    """v17 adds calendar_events.llm_status, and must not put the archive on retry.

    'pending' is the value that makes cmd_calendar_sync re-offer an unmodified event, so
    what the existing rows get is not bookkeeping: defaulting them all to 'pending' would
    send every event ever synced back through the LLM on the next tick of a unit with a
    90s budget. Rows that plainly produced a result are promoted; the rest are left inert.

    Would this pass with the behaviour removed? No, and each half fails on its own.
    Declaring the column DEFAULT 'pending' — the attachment table's default, so the
    tempting one — makes the extracted row read 'pending'. Dropping the backfill UPDATE
    makes it read 'skipped', which understates a row that really was extracted and would
    hide it from any "what still owes us an extraction" query.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(str(Path(tmpdir) / "test.db"))
        _v16_calendar_db(conn)

        conn.execute(
            "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at, "
            "ingested_at, body_extracted_at, body_summary) "
            "VALUES ('done', 's', 'a', 'b', 'c', '2026-08-01T00:00:00Z', 'a summary')"
        )
        conn.execute(
            "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at, "
            "ingested_at) VALUES ('never-extracted', 's', 'a', 'b', 'c')"
        )
        conn.commit()

        schema.run_migrations(conn)

        statuses = {
            r["outlook_event_id"]: r["llm_status"]
            for r in conn.execute("SELECT outlook_event_id, llm_status FROM calendar_events")
        }
        assert statuses == {"done": "extracted", "never-extracted": "skipped"}
        conn.close()


def test_migration_v17_is_idempotent_and_tolerates_a_calendar_free_database():
    """Both shapes the estate actually has: re-running, and a DB with no calendar tables.

    Would this pass with the behaviour removed? No. Dropping the column-presence check
    raises "duplicate column name: llm_status" on the second call; dropping the table
    check raises "no such table: calendar_events" on a DB that never had the calendar
    migration, which would break run_migrations for every caller of a partial fixture DB.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(str(Path(tmpdir) / "test.db"))
        _v16_calendar_db(conn)
        schema.migrate_add_calendar_llm_status(conn)
        schema.migrate_add_calendar_llm_status(conn)  # must not crash
        cols = {row[1] for row in conn.execute("PRAGMA table_info(calendar_events)").fetchall()}
        assert "llm_status" in cols
        conn.close()

        bare = sqlite3.connect(":memory:")
        schema.migrate_add_calendar_llm_status(bare)  # must not crash
        bare.close()


def test_migration_v17_rolls_the_alter_back_when_the_backfill_fails():
    """The ALTER and the backfill land together or not at all.

    Python's sqlite3 opens no implicit transaction for DDL, so an unwrapped ALTER is
    durable the instant it runs while the UPDATE waits for a commit. A crash in that gap
    is not merely a half-done migration, it is an UNREPAIRABLE one: the column exists, so
    every later run short-circuits the backfill it never performed, and the archive stays
    mislabelled for good.

    The failure is injected by making the backfill raise, which is the only deterministic
    stand-in for a crash between the two statements.

    Would this pass with the behaviour removed? No. Drop the BEGIN IMMEDIATE and the ALTER
    survives the rolled-back UPDATE: 'llm_status' is in the column list and the assertion
    fails. That is exactly the unrepairable state.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = sqlite3.connect(str(Path(tmpdir) / "test.db"))
        _v16_calendar_db(conn)
        conn.execute(
            "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at, "
            "ingested_at, body_extracted_at) VALUES ('done', 's', 'a', 'b', 'c', 'd')"
        )
        conn.commit()

        # A trigger, not a mock: sqlite3.Connection.execute is read-only, and this makes
        # the real UPDATE fail inside the real transaction rather than simulating it.
        conn.execute(
            "CREATE TRIGGER boom BEFORE UPDATE ON calendar_events "
            "BEGIN SELECT RAISE(ABORT, 'simulated crash between the two statements'); END"
        )
        conn.commit()

        with pytest.raises(sqlite3.DatabaseError):
            schema.migrate_add_calendar_llm_status(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(calendar_events)")}
        assert "llm_status" not in cols, "the ALTER must roll back with the backfill"
        conn.close()


def test_migration_v17_loses_a_race_cleanly_instead_of_raising_duplicate_column():
    """Two processes, one first run. The loser must no-op, not crash.

    The realistic vector is sb-auth-watch.sh firing up to six units back to back with
    --no-block, several of which open the same database and call run_migrations.

    DETERMINISTIC, not a hopeful sleep. The writer takes the write lock and performs the
    ALTER before the loser is even started, and holds it open until the loser has had to
    block on it. So the loser is guaranteed to have done its existence check AFTER the
    column was created — which is only true if that check happens inside its own
    transaction, i.e. after BEGIN IMMEDIATE.

    Would this pass with the behaviour removed? No. Hoist the PRAGMA read back above the
    BEGIN, as it was, and the loser reads "no column" while the writer still holds the
    lock, waits, and then issues its own ALTER: sqlite3.OperationalError "duplicate column
    name: llm_status" surfaces on the joined thread and `error` is not None.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = str(Path(tmpdir) / "race.db")
        writer = sqlite3.connect(db_file)
        _v16_calendar_db(writer)

        # The winner: holds the write lock with the ALTER already applied but uncommitted.
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "ALTER TABLE calendar_events ADD COLUMN llm_status TEXT NOT NULL DEFAULT 'skipped'"
        )

        error = []
        started = threading.Event()

        def loser():
            other = sqlite3.connect(db_file, timeout=10)
            other.row_factory = sqlite3.Row
            started.set()
            try:
                schema.migrate_add_calendar_llm_status(other)
            except Exception as e:  # noqa: BLE001 — the assertion is that there is none
                error.append(e)
            finally:
                other.close()

        t = threading.Thread(target=loser)
        t.start()
        started.wait(timeout=5)
        # Give the loser time to reach its BEGIN IMMEDIATE and block on the held lock.
        time.sleep(0.2)
        writer.commit()
        t.join(timeout=15)
        writer.close()

        assert not t.is_alive()
        assert error == [], f"the losing process must no-op, not raise: {error}"

        check = sqlite3.connect(db_file)
        cols = {row[1] for row in check.execute("PRAGMA table_info(calendar_events)")}
        assert "llm_status" in cols
        check.close()
