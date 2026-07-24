"""Tests for the teams schema migration."""

import sqlite3

import pytest

from src.store.schema import create_database, migrate_add_teams, run_migrations


@pytest.fixture
def fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    create_database(db_path).close()
    return db_path


def _open(db_path: str) -> sqlite3.Connection:
    """Open the test DB with row_factory set so columns can be accessed by name."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn, table: str, column: str) -> bool:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def test_migration_creates_all_teams_tables(fresh_db):
    conn = _open(fresh_db)
    migrate_add_teams(conn)

    for table in (
        "teams_chats",
        "teams_messages",
        "teams_threads",
        "teams_mri_resolution",
        "teams_messages_fts",
        "teams_threads_fts",
    ):
        assert _table_exists(conn, table), f"missing table: {table}"
    conn.close()


def test_migration_adds_teams_thread_id_columns(fresh_db):
    conn = _open(fresh_db)
    migrate_add_teams(conn)

    for table in ("decisions", "action_items", "key_facts"):
        assert _column_exists(conn, table, "teams_thread_id"), (
            f"{table} missing teams_thread_id column"
        )
    conn.close()


def test_migration_is_idempotent(fresh_db):
    conn = _open(fresh_db)
    migrate_add_teams(conn)
    # Second run must not raise.
    migrate_add_teams(conn)
    # And run_migrations on top of an already-migrated DB must also be safe.
    run_migrations(conn)
    conn.close()


def test_teams_messages_fts_trigger_keeps_index_in_sync(fresh_db):
    from datetime import datetime

    conn = _open(fresh_db)
    migrate_add_teams(conn)

    # Need a chat row for FK
    conn.execute(
        "INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at) VALUES (?, ?, ?)",
        ("19:test@thread.v2", "channel", datetime.now().isoformat()),
    )
    chat_id = conn.execute("SELECT id FROM teams_chats").fetchone()["id"]

    conn.execute(
        "INSERT INTO teams_messages(teams_message_id, chat_id, composed_at, "
        "content_text, sender_display_name) VALUES (?, ?, ?, ?, ?)",
        ("19:test::1", chat_id, datetime.now().isoformat(), "hello world", "Alice"),
    )
    conn.commit()

    # FTS should find it
    rows = conn.execute(
        "SELECT rowid FROM teams_messages_fts WHERE teams_messages_fts MATCH 'hello'"
    ).fetchall()
    assert len(rows) == 1
    conn.close()
