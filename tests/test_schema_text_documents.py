"""v28: documents that keep no file, session notes, and facts that know their attachment.

A SharePoint file or a session note is stored as text only, so sharepoint_links names the
document a link became and session_notes maps a note's path to its current document. An
attachment's own key facts, decisions and action items carry its id, so a new summary of the
attachment replaces only them.
"""

import sqlite3

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import create_database, get_schema_version, migrate_add_text_documents


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_a_fresh_store_has_the_v28_shape(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    assert CURRENT_SCHEMA_VERSION >= 28
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert "document_message_id" in _cols(conn, "sharepoint_links")
    assert _cols(conn, "session_notes") == {
        "path",
        "message_id",
        "session_id",
        "written_at",
        "sha256",
    }
    for table in ("key_facts", "decisions", "action_items"):
        assert "attachment_id" in _cols(conn, table)
        indexes = {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}
        assert f"idx_{table}_attachment_id" in indexes
    conn.close()


def test_the_migration_is_idempotent(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    migrate_add_text_documents(conn)
    migrate_add_text_documents(conn)
    assert "attachment_id" in _cols(conn, "key_facts")
    conn.close()


def test_the_migration_tolerates_a_store_without_the_parent_tables(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "p.db"))
    migrate_add_text_documents(conn)
    assert _cols(conn, "session_notes")
    conn.close()
