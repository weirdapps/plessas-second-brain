"""Verify schema migration v11 adds internet_message_id with partial unique index."""

import sqlite3
from pathlib import Path

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import (
    create_database,
    get_connection,
    run_migrations,
)


def _email_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(emails)").fetchall()}


def _email_indexes(conn: sqlite3.Connection) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='emails'"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def test_fresh_install_includes_internet_message_id(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    conn = create_database(str(db))
    run_migrations(conn)
    assert "internet_message_id" in _email_columns(conn)
    indexes = _email_indexes(conn)
    assert "idx_emails_internet_message_id" in indexes
    sql = indexes["idx_emails_internet_message_id"] or ""
    assert "UNIQUE" in sql.upper()
    assert "WHERE" in sql.upper()  # partial index — only NOT NULL rows
    conn.close()


def test_legacy_db_gets_column_and_partial_unique_index(tmp_path: Path) -> None:
    """A pre-v11 DB with existing rows must gain the column and the index without breaking."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    # Minimal pre-v11 schema (just the emails columns the migration cares about).
    conn.execute(
        """
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY,
            message_id INTEGER UNIQUE NOT NULL,
            date_received TEXT NOT NULL,
            subject TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
        ("legacy-1", "2025-01-01T00:00:00Z", "old"),
    )
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_version (version) VALUES (10)")
    conn.commit()
    conn.close()

    conn = get_connection(str(db))
    run_migrations(conn)

    assert "internet_message_id" in _email_columns(conn)
    # Pre-existing row preserved with NULL imid.
    row = conn.execute(
        "SELECT message_id, internet_message_id FROM emails WHERE message_id='legacy-1'"
    ).fetchone()
    assert row[0] == "legacy-1"
    assert row[1] is None

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    conn.close()


def test_partial_unique_index_blocks_duplicate_imid_but_allows_multiple_nulls(
    tmp_path: Path,
) -> None:
    db = tmp_path / "imid.db"
    conn = create_database(str(db))
    run_migrations(conn)

    # Two rows with NULL imid should be fine — the index is partial.
    conn.execute(
        "INSERT INTO emails (message_id, date_received) VALUES (?, ?)",
        ("a", "2025-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO emails (message_id, date_received) VALUES (?, ?)",
        ("b", "2025-01-01T00:00:00Z"),
    )

    # Two rows with the same NON-NULL imid should fail.
    conn.execute(
        "INSERT INTO emails (message_id, date_received, internet_message_id) VALUES (?, ?, ?)",
        ("c", "2025-01-01T00:00:00Z", "<same@example.com>"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO emails (message_id, date_received, internet_message_id) VALUES (?, ?, ?)",
            ("d", "2025-01-01T00:00:00Z", "<same@example.com>"),
        )
    conn.close()


def test_running_migrations_twice_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "twice.db"
    conn = create_database(str(db))
    run_migrations(conn)
    run_migrations(conn)  # second pass must not error
    assert "internet_message_id" in _email_columns(conn)
    conn.close()
