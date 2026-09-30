"""A document that keeps no file: three rows written at once, then Phase 2 as usual.

Phase 1 selects attachments without a content row, so it never sees these documents; Phase 2
selects extracted rows still pending, so it summarises them.
"""

import sqlite3

import pytest

from src.extract import attachment_pipeline
from src.extract.attachment_pipeline import ingest_text_document, run_phase1, run_phase2
from src.store.schema import create_database

TEXT = "A note about the quarterly plan, long enough to count as real content here."


def _doc(conn, sha="ab" * 32, text=TEXT, status="extracted"):
    return ingest_text_document(
        conn,
        source="session-note",
        key="/notes/plan.md",
        filename="plan.md",
        mime_type="text/markdown",
        text=text,
        sha256=sha,
        method="session-note",
        status=status,
        error=None,
        subject="[Session note] /notes/plan.md",
        sender_name="Claude session",
        date="2026-09-30T10:00:00",
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    yield path, conn
    conn.close()


def test_writes_the_email_the_attachment_and_a_complete_content_row(db):
    _path, conn = db
    out = _doc(conn)

    assert out["skipped"] is False
    email = conn.execute(
        "SELECT subject, mailbox_name, sender_address, message_id FROM emails WHERE id = ?",
        (out["email_id"],),
    ).fetchone()
    assert tuple(email) == (
        "[Session note] /notes/plan.md",
        "External",
        "session-note@documents.local",
        out["message_id"],
    )
    att = conn.execute(
        "SELECT file_path, sha256 FROM attachments WHERE id = ?", (out["attachment_id"],)
    ).fetchone()
    assert tuple(att) == ("text:session-note:/notes/plan.md", "ab" * 32)
    ac = conn.execute(
        "SELECT extraction_status, llm_status, extracted_text FROM attachment_content"
        " WHERE attachment_id = ?",
        (out["attachment_id"],),
    ).fetchone()
    assert (ac[0], ac[1]) == ("extracted", "pending")
    assert "quarterly plan" in ac[2]


def test_phase1_never_selects_it_and_phase2_does(db, monkeypatch):
    path, conn = db
    _doc(conn)
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], {"summary": "s", "language": "en"}, None, False),
    )

    assert run_phase1(str(path))["processed"] == 0
    assert run_phase2(str(path))["extracted"] == 1


def test_the_same_bytes_twice_are_stored_once(db):
    _path, conn = db
    first = _doc(conn)
    again = _doc(conn)

    assert again["skipped"] is True
    assert again["message_id"] == first["message_id"]
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1


def test_secrets_are_redacted_before_they_are_stored(db):
    _path, conn = db
    token = "123456789:AA" + "x" * 33
    out = _doc(conn, text=f"the bot token is {token}, keep it safe, and a few more words")

    text = conn.execute(
        "SELECT extracted_text FROM attachment_content WHERE attachment_id = ?",
        (out["attachment_id"],),
    ).fetchone()[0]
    assert token not in text
    assert "[REDACTED:telegram-token]" in text


def test_a_failed_write_leaves_no_rows(db):
    _path, conn = db
    with pytest.raises(sqlite3.IntegrityError):
        _doc(conn, status=None)

    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
