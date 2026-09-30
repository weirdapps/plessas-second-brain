"""v27: attachments carry a content hash and a removal stamp; images count vision attempts.

The VPS deletes attachment files once their content is stored (src/store/file_sweep.py), so
the orphan reaper can no longer prove a re-download is a duplicate by comparing it with a copy
on disk. The hash has to live in the table.
"""

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import create_database, get_schema_version, migrate_add_file_hashes


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_a_fresh_store_has_the_new_columns(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    assert CURRENT_SCHEMA_VERSION >= 27
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert {"sha256", "file_removed_at"} <= _cols(conn, "attachments")
    assert "vision_attempts" in _cols(conn, "inline_images")
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(attachments)")}
    assert "idx_attachments_sha256" in indexes
    conn.close()


def test_the_migration_is_idempotent(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    migrate_add_file_hashes(conn)
    migrate_add_file_hashes(conn)
    assert {"sha256", "file_removed_at"} <= _cols(conn, "attachments")
    conn.close()


def test_it_upgrades_a_store_that_predates_it(tmp_path):
    conn = create_database(str(tmp_path / "old.db"))
    conn.execute("DROP INDEX idx_attachments_sha256")
    conn.execute("ALTER TABLE attachments DROP COLUMN sha256")
    conn.execute("ALTER TABLE attachments DROP COLUMN file_removed_at")
    conn.execute("ALTER TABLE inline_images DROP COLUMN vision_attempts")
    conn.commit()

    migrate_add_file_hashes(conn)

    assert {"sha256", "file_removed_at"} <= _cols(conn, "attachments")
    assert "vision_attempts" in _cols(conn, "inline_images")
    conn.close()


def test_an_existing_image_starts_with_no_attempts(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.execute(
        "INSERT INTO inline_images (sha256, width, height, bytes, classification,"
        " classification_method, classified_at) VALUES ('ab', 1, 1, 1, 'content', 't', 'now')"
    )
    assert conn.execute("SELECT vision_attempts FROM inline_images").fetchone()[0] == 0
    conn.close()


def test_v27_corrects_images_recorded_as_octet_stream(tmp_path):
    """ingest_document recorded every image as octet-stream, so vision never saw them."""
    conn = create_database(str(tmp_path / "b.db"))
    for name, mime in (("a.PNG", "application/octet-stream"), ("b.jpg", None), ("c.pdf", None)):
        conn.execute(
            "INSERT INTO attachments (message_id, filename, mime_type, file_path, exported_at)"
            " VALUES (-1, ?, ?, ?, 'now')",
            (name, mime, f"/att/1/{name}"),
        )
    conn.commit()

    migrate_add_file_hashes(conn)

    got = dict(conn.execute("SELECT filename, mime_type FROM attachments").fetchall())
    assert got == {"a.PNG": "image/png", "b.jpg": "image/jpeg", "c.pdf": None}
    conn.close()
