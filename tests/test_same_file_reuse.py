"""The same bytes arriving twice are extracted and summarised once.

A deck forwarded around, or a re-download of a moved message, reaches the store many times.
Its text and summary are the same every time, so the second copy takes the first copy's
finished row: no extraction and no model call. Its key facts stay with the first email.
"""

import pytest

from src.extract import attachment_pipeline
from src.extract.attachment_pipeline import run_phase1
from src.store.schema import create_database

SHA = "cd" * 32


def _attachment(conn, tmp_path, n, sha=SHA):
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, '2026-09-01', ?)",
        (1000 + n, f"mail {n}"),
    )
    email_id = conn.execute("SELECT id FROM emails WHERE message_id = ?", (1000 + n,)).fetchone()[0]
    f = tmp_path / "att" / str(1000 + n) / "deck.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"%PDF the same bytes")
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, sha256)"
        " VALUES (?, ?, 'deck.pdf', 'application/pdf', 19, ?, '2026-09-01', ?)",
        (email_id, 1000 + n, str(f), sha),
    )
    return conn.execute("SELECT id FROM attachments WHERE email_id = ?", (email_id,)).fetchone()[0]


def _content(conn, att_id, status="extracted", llm="extracted", error=None):
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error, extracted_at, summary, language, llm_status)"
        " VALUES (?, 'the deck text', 'pdfplumber', ?, ?, '2026-09-01', 'a deck', 'en', ?)",
        (att_id, status, error, llm),
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    yield path, conn
    conn.close()


def _record_reads(monkeypatch):
    reads = []

    def fake(path, mime):
        reads.append(path)
        return {
            "text": "text read from the file itself, long enough to count",
            "method": "pdfplumber",
            "status": "extracted",
            "error": None,
        }

    monkeypatch.setattr(attachment_pipeline, "extract_text_from_file", fake)
    return reads


def test_a_second_copy_takes_the_first_copys_finished_row(db, tmp_path, monkeypatch):
    path, conn = db
    _content(conn, _attachment(conn, tmp_path, 1))
    second = _attachment(conn, tmp_path, 2)
    conn.commit()
    reads = _record_reads(monkeypatch)

    stats = run_phase1(str(path))

    assert reads == []
    assert stats["reused"] == 1
    row = conn.execute(
        "SELECT extracted_text, summary, llm_status FROM attachment_content"
        " WHERE attachment_id = ?",
        (second,),
    ).fetchone()
    assert tuple(row) == ("the deck text", "a deck", "extracted")


def test_a_copy_whose_bytes_were_never_read_is_not_reused(db, tmp_path, monkeypatch):
    path, conn = db
    _content(
        conn,
        _attachment(conn, tmp_path, 1),
        status="failed",
        llm="pending",
        error="File not found: /elsewhere/deck.pdf",
    )
    _attachment(conn, tmp_path, 2)
    conn.commit()
    reads = _record_reads(monkeypatch)

    stats = run_phase1(str(path))

    assert len(reads) == 1
    assert stats.get("reused", 0) == 0


def test_a_copy_still_waiting_for_its_summary_is_not_reused(db, tmp_path, monkeypatch):
    path, conn = db
    _content(conn, _attachment(conn, tmp_path, 1), llm="pending")
    _attachment(conn, tmp_path, 2)
    conn.commit()
    reads = _record_reads(monkeypatch)

    run_phase1(str(path))

    assert len(reads) == 1


def test_an_attachment_without_a_hash_is_extracted(db, tmp_path, monkeypatch):
    path, conn = db
    _content(conn, _attachment(conn, tmp_path, 1))
    _attachment(conn, tmp_path, 2, sha=None)
    conn.commit()
    reads = _record_reads(monkeypatch)

    run_phase1(str(path))

    assert len(reads) == 1
