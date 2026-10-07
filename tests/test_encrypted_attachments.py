"""A file encrypted at rest is recorded as 'encrypted', with the kind of protection as its method.

Rights Management (RMS) and password encryption both store an Office file as an OLE2 container
whose directory says which it is, and an RMS-protected message (.rpmsg) is an MSIPC container.
No reader opens any of them without the key, so they were recorded as failures (BadZipFile, an
xlrd "Can't find workbook", CompoundFileInvalidMagicError) or as skips that blamed "a custom
export format". The contract other parts rely on: extraction_status 'encrypted', and
extraction_method 'rms', 'rms-message' or 'password', with an error that names the kind.
"""

from unittest.mock import patch

import pytest

from src.extract.attachment_extractors import _ole_stream_names, extract_text_from_file
from tests.ole_fixtures import LEGACY_RMS_NAMES, PASSWORD_NAMES, RMS_NAMES, STREAM, ole_file

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WORDS = "A quarterly review of the regional network, with enough words to pass the filter. "


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_the_directory_walk_reads_every_sector_of_the_chain(tmp_path):
    path = _write(tmp_path, "book.xlsx", ole_file(RMS_NAMES))

    names = _ole_stream_names(path)

    assert {"DRMEncryptedDataSpace", "DRMEncryptedTransform", "EncryptedPackage"} <= names


def test_the_walk_follows_the_chain_into_a_second_fat_sector(tmp_path):
    """128 sectors per FAT sector: a directory past them is found through the second one."""
    path = _write(tmp_path, "book.xlsx", ole_file(RMS_NAMES, pad_sectors=140))

    assert "EncryptedPackage" in _ole_stream_names(path)


def test_a_directory_chain_that_loops_ends(tmp_path):
    path = _write(tmp_path, "book.xlsx", ole_file(RMS_NAMES, cycle=True))

    assert "EncryptedPackage" in _ole_stream_names(path)


@pytest.mark.parametrize("name", ["Quarterly.xlsx", "Plan.xlsb", "Brief.docx", "Deck.pptx"])
def test_a_rights_protected_office_file_is_encrypted_rms(tmp_path, name):
    out = extract_text_from_file(_write(tmp_path, name, ole_file(RMS_NAMES)), XLSX)

    assert (out["status"], out["method"], out["text"]) == ("encrypted", "rms", None)
    assert "Rights-protected (RMS)" in out["error"]


def test_the_declared_type_does_not_decide(tmp_path):
    """Senders label these application/encrypted, octet-stream or a plain Office type."""
    for mime in ("application/encrypted", "application/octet-stream", DOCX):
        out = extract_text_from_file(_write(tmp_path, "Brief.docx", ole_file(RMS_NAMES)), mime)
        assert (out["status"], out["method"]) == ("encrypted", "rms")


def test_a_rights_protected_97_2003_file_is_encrypted_rms(tmp_path):
    out = extract_text_from_file(
        _write(tmp_path, "Old.doc", ole_file(LEGACY_RMS_NAMES)), "application/msword"
    )

    assert (out["status"], out["method"]) == ("encrypted", "rms")


def test_a_password_protected_office_file_is_encrypted_password(tmp_path):
    out = extract_text_from_file(_write(tmp_path, "Budget.xlsx", ole_file(PASSWORD_NAMES)), XLSX)

    assert (out["status"], out["method"]) == ("encrypted", "password")
    assert "Password-protected .xlsx" in out["error"]


def test_a_protected_message_is_encrypted_rms_message(tmp_path):
    for name in ("message.rpmsg", "Outlook-a1b2c"):
        data = b"\x76\xe8\x04\x60\xc4\x11\xe3\x86" + b"\x00" * 120
        out = extract_text_from_file(_write(tmp_path, name, data), "application/octet-stream")
        assert (out["status"], out["method"]) == ("encrypted", "rms-message")
        assert ".rpmsg" in out["error"]


def test_an_ordinary_legacy_workbook_is_not_taken_for_an_encrypted_one(tmp_path):
    data = ole_file([("Workbook", STREAM), ("\x05SummaryInformation", STREAM)])
    out = extract_text_from_file(_write(tmp_path, "Old.xls", data), "application/vnd.ms-excel")

    assert out["status"] != "encrypted"


def test_the_error_matches_no_unread_marker():
    """The sweep and reextract read 'left unread', 'file kept', 'not installed' as never read."""
    from src.extract.attachment_extractors import encrypted_result

    for method in ("rms", "rms-message", "password"):
        error = encrypted_result(method, ".xlsx")["error"]
        for marker in ("left unread", "file kept", "not installed", "File not found", "No such"):
            assert marker not in error


def _pdf(tmp_path, **save):
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), WORDS)
    path = tmp_path / "statement.pdf"
    doc.save(str(path), **save)
    return str(path)


def test_a_pdf_that_needs_a_password_is_encrypted_password(tmp_path):
    import fitz

    path = _pdf(tmp_path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u-secret", owner_pw="o")

    out = extract_text_from_file(path, "application/pdf")

    assert (out["status"], out["method"]) == ("encrypted", "password")
    assert "Password-protected .pdf" in out["error"]


def test_a_pdf_with_only_an_owner_password_is_read(tmp_path):
    import fitz

    path = _pdf(tmp_path, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner-only")

    out = extract_text_from_file(path, "application/pdf")

    assert out["status"] == "extracted"
    assert "regional network" in out["text"]


def test_an_encrypted_legacy_workbook_is_encrypted_password(tmp_path):
    from xlrd import XLRDError

    path = _write(tmp_path, "Old.xls", ole_file([("Workbook", STREAM)]))
    with patch("xlrd.open_workbook", side_effect=XLRDError("Workbook is encrypted")):
        out = extract_text_from_file(path, "application/vnd.ms-excel")

    assert (out["status"], out["method"]) == ("encrypted", "password")


def test_an_encrypted_workbook_named_xlsb_is_encrypted_password(tmp_path):
    from xlrd import XLRDError

    path = _write(tmp_path, "Old.xlsb", ole_file([("Workbook", STREAM)]))
    with patch("xlrd.open_workbook", side_effect=XLRDError("Workbook is encrypted")):
        out = extract_text_from_file(path, "application/octet-stream")

    assert (out["status"], out["method"]) == ("encrypted", "password")


def test_phase1_stores_the_encrypted_verdict_and_counts_it(tmp_path):
    import sqlite3

    from src.extract.attachment_pipeline import run_phase1
    from src.store.schema import create_database

    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'mail')"
    )
    path = _write(tmp_path, "Plan.xlsx", ole_file(RMS_NAMES))
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size, file_path,"
        " exported_at) VALUES (1, 7, 'Plan.xlsx', ?, 1, ?, '2026-09-01')",
        (XLSX, path),
    )
    conn.commit()
    conn.close()

    stats = run_phase1(str(db))

    row = (
        sqlite3.connect(db)
        .execute(
            "SELECT extraction_status, extraction_method, extracted_text FROM attachment_content"
        )
        .fetchone()
    )
    assert row == ("encrypted", "rms", None)
    assert (stats["encrypted"], stats["failed"], stats["skipped"]) == (1, 0, 0)


def test_phase1_reports_zero_encrypted_when_there_are_none(tmp_path):
    from src.extract.attachment_pipeline import run_phase1
    from src.store.schema import create_database

    db = tmp_path / "brain.db"
    create_database(str(db)).close()

    assert run_phase1(str(db))["encrypted"] == 0
