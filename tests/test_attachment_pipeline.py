import os
import sqlite3
import tempfile


def _create_test_db(db_path):
    """Create a minimal test database with attachments."""
    from src.store.schema import create_database

    conn = create_database(db_path)

    # Insert a test email
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary) "
        "VALUES (1000, '2026-01-01', 'Test Email', 'Test summary')"
    )

    # Create a real text file for the attachment
    att_dir = os.path.join(os.path.dirname(db_path), "attachments", "1000")
    os.makedirs(att_dir, exist_ok=True)
    att_path = os.path.join(att_dir, "test.txt")
    with open(att_path, "w") as f:
        f.write(
            "This is a test document with enough text content to pass the extraction threshold easily."
        )

    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size, file_path, exported_at) "
        f"VALUES (1, 1000, 'test.txt', 'text/plain', 100, '{att_path}', '2026-01-01')"
    )
    conn.commit()
    return conn


def test_phase1_extracts_text():
    from src.extract.attachment_pipeline import run_phase1

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _create_test_db(db_path)
        conn.close()

        stats = run_phase1(db_path, limit=10)
        assert stats["processed"] == 1
        assert stats["extracted"] >= 1

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT extracted_text, extraction_status, extraction_method "
            "FROM attachment_content WHERE attachment_id = 1"
        ).fetchone()
        assert row is not None
        assert row[1] == "extracted"  # extraction_status
        assert "test document" in row[0]  # extracted_text
        conn.close()


def test_phase1_is_resumable():
    """Running phase1 twice should not reprocess already-extracted files."""
    from src.extract.attachment_pipeline import run_phase1

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = _create_test_db(db_path)
        conn.close()

        stats1 = run_phase1(db_path, limit=10)
        assert stats1["processed"] == 1

        stats2 = run_phase1(db_path, limit=10)
        assert stats2["processed"] == 0  # nothing new to process
