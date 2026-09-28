"""Schema v26: WhatsApp chats, messages and threads, searchable with Greek folding."""

import sqlite3

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.query import _sanitize_fts5_query
from src.store.schema import (
    create_database,
    get_schema_version,
    run_migrations,
    set_schema_version,
)


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")}


def test_schema_version_is_26():
    assert CURRENT_SCHEMA_VERSION == 26


def test_a_fresh_store_has_the_whatsapp_tables(db):
    for table in ("whatsapp_chats", "whatsapp_messages", "whatsapp_threads"):
        assert _columns(db, table), f"{table} missing"


def test_a_message_is_unique_per_chat_and_id(db):
    db.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at) "
        "VALUES ('c@g.us', 'Chat A', 'group', '2026-09-01T00:00:00Z')"
    )
    row = (1, "c@g.us", "m1", "2026-09-01T10:00:00Z", "hello")
    sql = (
        "INSERT INTO whatsapp_messages (chat_id, chat_jid, message_id, sent_at, content) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    db.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, row)


def test_the_extracted_rows_can_point_at_a_whatsapp_thread(db):
    for table in ("decisions", "action_items", "key_facts"):
        assert "whatsapp_thread_id" in _columns(db, table), table


def test_message_search_ignores_greek_accents(db):
    db.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at) "
        "VALUES ('c@g.us', 'Chat A', 'group', '2026-09-01T00:00:00Z')"
    )
    db.execute(
        "INSERT INTO whatsapp_messages (chat_id, chat_jid, message_id, sent_at, content) "
        "VALUES (1, 'c@g.us', 'm1', '2026-09-01T10:00:00Z', 'η παρουσίαση είναι έτοιμη')"
    )
    hits = db.execute(
        "SELECT count(*) FROM whatsapp_messages_fts WHERE whatsapp_messages_fts MATCH ?",
        (_sanitize_fts5_query("παρουσιαση"),),
    ).fetchone()[0]
    assert hits == 1


def test_thread_summaries_are_searchable(db):
    db.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at) "
        "VALUES ('c@g.us', 'Chat A', 'group', '2026-09-01T00:00:00Z')"
    )
    db.execute(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at, "
        "summary) VALUES (1, 'm1', '2026-09-01T10:00:00Z', '2026-09-01T11:00:00Z', "
        "'Planned the sailing trip')"
    )
    hits = db.execute(
        "SELECT count(*) FROM whatsapp_threads_fts WHERE whatsapp_threads_fts MATCH ?",
        (_sanitize_fts5_query("sailing"),),
    ).fetchone()[0]
    assert hits == 1


def test_a_v25_store_migrates_to_v26(tmp_path):
    conn = create_database(str(tmp_path / "old.db"))
    for table in ("whatsapp_messages_fts", "whatsapp_threads_fts"):
        conn.execute(f"DROP TABLE {table}")
    for table in ("whatsapp_messages", "whatsapp_threads", "whatsapp_chats"):
        conn.execute(f"DROP TABLE {table}")
    set_schema_version(conn, 25)
    run_migrations(conn)
    assert get_schema_version(conn) == 26
    assert _columns(conn, "whatsapp_messages")
    conn.close()


def test_running_the_migrations_twice_is_a_no_op(db):
    run_migrations(db)
    set_schema_version(db, 25)
    run_migrations(db)
    assert get_schema_version(db) == 26
