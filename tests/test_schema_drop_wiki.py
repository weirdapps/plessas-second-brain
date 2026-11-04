"""Verify schema migration v8 drops the wiki tables idempotently."""

import sqlite3
from pathlib import Path

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import (
    create_database,
    get_connection,
    run_migrations,
)


def _wiki_objects(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'wiki_pages%' OR name LIKE 'idx_wiki_pages%'"
    ).fetchall()
    return {r[0] for r in rows}


def test_fresh_install_has_no_wiki_tables(tmp_path: Path) -> None:
    """create_database on an empty file must not create wiki_pages."""
    db = tmp_path / "fresh.db"
    conn = create_database(str(db))
    run_migrations(conn)
    assert _wiki_objects(conn) == set()
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    conn.close()


def test_drop_migration_is_idempotent(tmp_path: Path) -> None:
    """Running migrations a second time on a clean DB must not error."""
    db = tmp_path / "twice.db"
    conn = create_database(str(db))
    run_migrations(conn)
    run_migrations(conn)  # idempotency check
    assert _wiki_objects(conn) == set()
    conn.close()


def test_drop_migration_removes_existing_wiki_tables(tmp_path: Path) -> None:
    """A DB that still has v6/v7 wiki tables must end up wiki-free after migration."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    # Simulate a v7 DB that still has the wiki schema
    conn.execute("CREATE TABLE wiki_pages (id INTEGER PRIMARY KEY, slug TEXT)")
    conn.execute("CREATE INDEX idx_wiki_pages_slug ON wiki_pages(slug)")
    conn.execute(
        "CREATE VIRTUAL TABLE wiki_pages_fts USING fts5(title, content, content='wiki_pages', content_rowid='id')"
    )
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (7)")
    conn.commit()
    conn.close()

    conn = get_connection(str(db))
    run_migrations(conn)
    assert _wiki_objects(conn) == set()
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    conn.close()
