"""Migration 004 — inline_images, inline_image_occurrences, sender_signature_index.

Adapted to the existing schema.py pattern (Python migrate_* functions
registered in run_migrations + CURRENT_SCHEMA_VERSION bump).
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


def test_migration_004_creates_three_tables(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    conn = create_database(str(db))
    run_migrations(conn)
    assert _table_exists(conn, "inline_images")
    assert _table_exists(conn, "inline_image_occurrences")
    assert _table_exists(conn, "sender_signature_index")
    conn.close()


def test_migration_004_creates_occurrences_index(tmp_path: Path) -> None:
    db = tmp_path / "idx.db"
    conn = create_database(str(db))
    run_migrations(conn)
    assert _index_exists(conn, "idx_occurrences_sender_sha")
    conn.close()


def test_migration_004_columns_match_spec(tmp_path: Path) -> None:
    db = tmp_path / "cols.db"
    conn = create_database(str(db))
    run_migrations(conn)

    img_cols = {row[1] for row in conn.execute("PRAGMA table_info(inline_images)")}
    assert {
        "sha256",
        "width",
        "height",
        "bytes",
        "classification",
        "classification_method",
        "classified_at",
        "vision_description",
        "user_overridden",
    }.issubset(img_cols)

    occ_cols = {row[1] for row in conn.execute("PRAGMA table_info(inline_image_occurrences)")}
    assert {"sha256", "message_id", "sender_email", "position_in_body"}.issubset(occ_cols)

    sig_cols = {row[1] for row in conn.execute("PRAGMA table_info(sender_signature_index)")}
    assert {
        "sender_email",
        "sha256",
        "occurrence_count",
        "sender_total",
        "frequency",
    }.issubset(sig_cols)
    conn.close()


def test_migration_004_classification_check_constraint(tmp_path: Path) -> None:
    """The classification CHECK constraint must reject invalid values."""
    db = tmp_path / "check.db"
    conn = create_database(str(db))
    run_migrations(conn)

    # Valid value works
    conn.execute(
        """
        INSERT INTO inline_images
        (sha256, width, height, bytes, classification, classification_method, classified_at)
        VALUES ('abc', 100, 50, 1234, 'noise', 'cascade', '2026-04-22T10:00:00')
        """
    )
    conn.commit()

    # Invalid value rejected
    try:
        conn.execute(
            """
            INSERT INTO inline_images
            (sha256, width, height, bytes, classification, classification_method, classified_at)
            VALUES ('def', 100, 50, 1234, 'bogus', 'cascade', '2026-04-22T10:00:00')
            """
        )
        conn.commit()
        raise AssertionError("Expected CHECK constraint failure")
    except sqlite3.IntegrityError:
        pass
    conn.close()


def test_migration_004_occurrence_cascade_delete(tmp_path: Path) -> None:
    """Deleting an inline_image must cascade to occurrences."""
    db = tmp_path / "cascade.db"
    conn = create_database(str(db))
    run_migrations(conn)

    conn.execute(
        """
        INSERT INTO inline_images
        (sha256, width, height, bytes, classification, classification_method, classified_at)
        VALUES ('cascade-sha', 200, 100, 9999, 'signature', 'cascade', '2026-04-22T10:00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO inline_image_occurrences (sha256, message_id, sender_email, position_in_body)
        VALUES ('cascade-sha', 'msg-1', 'a@b.com', 0.5)
        """
    )
    conn.commit()

    conn.execute("DELETE FROM inline_images WHERE sha256 = 'cascade-sha'")
    conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) FROM inline_image_occurrences WHERE sha256 = 'cascade-sha'"
    ).fetchone()[0]
    assert remaining == 0
    conn.close()


def test_migration_004_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "twice.db"
    conn = create_database(str(db))
    run_migrations(conn)
    run_migrations(conn)
    assert _table_exists(conn, "inline_images")
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == CURRENT_SCHEMA_VERSION
    conn.close()
