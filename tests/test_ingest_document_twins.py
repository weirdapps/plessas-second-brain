"""ingest_document does not store a second copy of bytes the store already holds.

A document is keyed on its own bytes, so ingesting the same file twice was already
skipped. A file saved from mail into a folder that `reverse-ingest` or `ingest` then
reads is another matter: its bytes arrived as a mail attachment, under the mail's own
id, and were stored a second time as a document, so search returned both
(scripts/forget_duplicate_documents.py removed 699 of them). attachments.sha256 names
those bytes, so the check reads it before anything is written.

Only a twin whose content is held counts (src/store/file_sweep.py's FULLY_HELD_SQL, the
rule the sweep and the orphan reaper use): a twin that failed, was skipped, is encrypted
or was read only in part holds nothing the document would repeat, and the document may
be the only readable copy of those bytes.
"""

import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest

from src.extract.attachment_pipeline import ingest_document
from src.store.schema import create_database

BODY = b"quarterly figures, the only copy"


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    monkeypatch.setattr("src.extract.attachment_pipeline.ATTACHMENTS_DIR", tmp_path / "att")
    return str(path)


def _mail_attachment(db_path: str, tmp_path: Path, status: str, error: str | None) -> tuple:
    """A mail attachment holding BODY, read by Phase 1 with this status and error."""
    path = tmp_path / "mail" / "report.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(BODY)
    conn = sqlite3.connect(db_path)
    email_id = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name)"
        " VALUES ('AAMk-mail', '2026-10-01', 'figures', 'Inbox')"
    ).lastrowid
    att_id = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_size, file_path,"
        " exported_at, sha256) VALUES (?, 'AAMk-mail', 'report.pdf', ?, ?, 'now', ?)",
        (email_id, len(BODY), str(path), hashlib.sha256(BODY).hexdigest()),
    ).lastrowid
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error) VALUES (?, 'quarterly figures', 'pymupdf', ?, ?)",
        (att_id, status, error),
    )
    conn.commit()
    conn.close()
    return att_id, str(path)


def _saved_copy(tmp_path: Path) -> Path:
    """The same bytes, saved from the mail into a folder under another name."""
    path = tmp_path / "saved" / "Q3 figures.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(BODY)
    return path


def _rows(db_path: str) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    try:
        emails = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        attachments = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    finally:
        conn.close()
    return emails, attachments


def test_bytes_a_mail_attachment_holds_are_not_stored_again(db_path, tmp_path, caplog):
    att_id, _ = _mail_attachment(db_path, tmp_path, "extracted", None)

    with caplog.at_level(logging.INFO, logger="src.extract.attachment_pipeline"):
        result = ingest_document(str(_saved_copy(tmp_path)), db_path=db_path)

    assert result["skipped"]
    assert result["attachment_id"] == att_id
    assert _rows(db_path) == (1, 1)
    assert not (tmp_path / "att").exists(), "nothing is copied for a skipped document"
    assert any(str(att_id) in r.getMessage() for r in caplog.records), "the reason is logged"


def test_the_skip_names_the_copy_a_caller_may_rely_on(db_path, tmp_path):
    """The orphan reaper removes its source only when the path it is given is on disk."""
    _, mail_path = _mail_attachment(db_path, tmp_path, "extracted", None)

    result = ingest_document(str(_saved_copy(tmp_path)), db_path=db_path)

    assert result["file_path"] == mail_path


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("encrypted", "rights-protected; the file is kept"),
        ("failed", "No such file or directory"),
        ("skipped", "application/octet-stream: no reader for this format"),
        ("extracted", "text cut at 2,000,000 characters; the rest is unread, file kept"),
    ],
    ids=["encrypted", "failed", "skipped", "read in part"],
)
def test_a_twin_that_holds_no_content_does_not_stop_the_document(db_path, tmp_path, status, error):
    _mail_attachment(db_path, tmp_path, status, error)

    result = ingest_document(str(_saved_copy(tmp_path)), db_path=db_path)

    assert not result["skipped"]
    assert _rows(db_path) == (2, 2)
    assert Path(result["file_path"]).read_bytes() == BODY
