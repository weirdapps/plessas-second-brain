"""Files no parser can read are recorded as encrypted or skipped, not failed.

A rights-protected Office file is stored as an OLE2 container holding \\x06DataSpaces, whatever
its name or declared type says, and a zero-byte file holds nothing to read. Recorded as failed,
each one read as a parser fault and was counted as a new failure in the nightly report. The
container's full directory says which protection it is (tests/test_encrypted_attachments.py);
these stubs hold only a first directory sector, and \\x06DataSpaces alone still reads as rights
management.
"""

from src.extract.attachment_extractors import extract_text_from_file
from tests.test_audit_extract_sniff import _ole_bytes

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_a_rights_protected_workbook_is_encrypted(tmp_path):
    f = tmp_path / "Report.xlsx"
    f.write_bytes(_ole_bytes("\x06DataSpaces"))

    out = extract_text_from_file(str(f), XLSX)

    assert (out["status"], out["method"]) == ("encrypted", "rms")
    assert "Rights-protected (RMS) .xlsx" in out["error"]


def test_a_rights_protected_document_is_encrypted_whatever_its_label(tmp_path):
    f = tmp_path / "Brief.docx"
    f.write_bytes(_ole_bytes("\x06DataSpaces"))

    assert extract_text_from_file(str(f), "application/octet-stream")["status"] == "encrypted"


def test_a_legacy_workbook_is_not_taken_for_a_protected_one(tmp_path):
    f = tmp_path / "Old.xlsx"
    f.write_bytes(_ole_bytes("Workbook"))

    out = extract_text_from_file(str(f), XLSX)

    assert out["status"] != "encrypted"
    assert "Rights-protected" not in (out["error"] or "")


def test_an_empty_file_is_skipped(tmp_path):
    f = tmp_path / "blank.pdf"
    f.write_bytes(b"")

    out = extract_text_from_file(str(f), "application/pdf")

    assert (out["status"], out["error"]) == ("skipped", "empty file (0 bytes)")
