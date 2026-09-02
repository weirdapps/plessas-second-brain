"""Migration 003 — sharepoint_links table.

Plan referenced apply_migrations(db, target_version=N) and SQL files in
src/store/migrations/. The actual schema.py uses Python migrate_*
functions registered in run_migrations(conn) plus a CURRENT_SCHEMA_VERSION
bump in src/config.py. Tests are adapted to that pattern.
"""

import sqlite3
from pathlib import Path

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import create_database, run_migrations


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


def test_migration_003_creates_sharepoint_links_table(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = create_database(str(db))
    run_migrations(conn)
    assert _table_exists(conn, "sharepoint_links")
    assert _index_exists(conn, "idx_sharepoint_message")
    assert _index_exists(conn, "idx_sharepoint_status")
    conn.close()


def test_migration_003_columns_match_spec(tmp_path: Path) -> None:
    db = tmp_path / "cols.db"
    conn = create_database(str(db))
    run_migrations(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sharepoint_links)")}
    expected = {
        "url",
        "message_id",
        "fetched_at",
        "fetched_path",
        "last_status",
        "last_attempt_at",
        "file_name",
        "file_size",
    }
    assert expected.issubset(cols)
    conn.close()


def test_migration_003_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "twice.db"
    conn = create_database(str(db))
    run_migrations(conn)
    run_migrations(conn)
    assert _table_exists(conn, "sharepoint_links")
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    conn.close()


def test_migration_003_inserts_and_queries(tmp_path: Path) -> None:
    db = tmp_path / "insert.db"
    conn = create_database(str(db))
    run_migrations(conn)

    conn.execute(
        """
        INSERT INTO sharepoint_links
        (url, message_id, fetched_at, fetched_path, last_status,
         last_attempt_at, file_name, file_size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "https://contoso.sharepoint.com/sites/foo/Doc.docx",
            "AAQk-msg-1",
            "2026-04-22T10:00:00",
            "/data/sharepoint/abc.docx",
            "fetched",
            "2026-04-22T10:00:01",
            "Doc.docx",
            12345,
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT message_id, last_status, file_size FROM sharepoint_links WHERE url = ?",
        ("https://contoso.sharepoint.com/sites/foo/Doc.docx",),
    ).fetchone()
    assert row[0] == "AAQk-msg-1"
    assert row[1] == "fetched"
    assert row[2] == 12345
    conn.close()
