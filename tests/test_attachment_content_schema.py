import os
import sqlite3
import tempfile


def test_migrate_add_attachment_content():
    """Migration should create attachment_content table and FTS index."""
    from src.store.schema import create_database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        conn = create_database(db_path)
        conn.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Verify attachment_content table exists after migration
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        ]
        assert "attachment_content" in tables

        # Verify columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(attachment_content)").fetchall()]
        assert "attachment_id" in cols
        assert "extracted_text" in cols
        assert "extraction_method" in cols
        assert "extraction_status" in cols
        assert "summary" in cols
        assert "llm_status" in cols

        # Verify FTS table
        assert "attachment_content_fts" in tables

        # Verify index
        indexes = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='attachment_content'"
            ).fetchall()
        ]
        assert any("attachment_id" in idx for idx in indexes)

        conn.close()
    finally:
        os.unlink(db_path)
