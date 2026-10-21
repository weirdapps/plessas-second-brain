"""Text files and converter output are decoded by what their bytes say, not by trying Latin-1.

A monthly card statement arrived as a UTF-16 CSV with a byte-order mark. The plain-text reader
tried UTF-8, failed, and read it as Latin-1, which never fails: the stored text began "ÿþ", held
a NUL after every Latin letter and showed its Greek as Latin-1 punctuation (28 rows, audit
2026-10-20). A legacy Word file failed outright: antiword wrote its text as UTF-8 with a few
characters encoded as surrogates, which Python's UTF-8 decoder refuses, so the whole agenda was
recorded as UnicodeDecodeError.
"""

import subprocess

from src.extract import attachment_extractors as ax
from src.extract.attachment_extractors import extract_text_from_file

GREEK = "Ημερομηνία\tΠοσό\tΠρατήριο"
ROWS = "\r\n".join(
    [GREEK, *(f"2026-03-{d:02d}\t{40 + d},50\tΣταθμός Αθήνας {d}" for d in range(1, 12))]
)


def test_a_utf16_csv_with_a_byte_order_mark_is_read_as_utf16(tmp_path):
    f = tmp_path / "statement.csv"
    f.write_bytes(ROWS.encode("utf-16"))  # little-endian, with its mark

    result = extract_text_from_file(str(f), "text/csv")

    assert (result["status"], result["method"]) == ("extracted", "direct_read")
    assert result["text"].startswith(GREEK)
    assert "\x00" not in result["text"] and "ÿþ" not in result["text"]
    assert "Σταθμός Αθήνας 11" in result["text"]
    assert "\r" not in result["text"]


def test_a_big_endian_utf16_text_is_read_too(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"\xfe\xff" + ROWS.encode("utf-16-be"))

    result = extract_text_from_file(str(f), "text/plain")

    assert result["text"].startswith(GREEK)


def test_a_plain_utf8_file_reads_as_before(tmp_path):
    f = tmp_path / "plain.csv"
    f.write_text(ROWS, encoding="utf-8")

    result = extract_text_from_file(str(f), "text/csv")

    assert result["text"] == ROWS.replace("\r\n", "\n")


def _converter_writes(monkeypatch, out: bytes):
    def fake(argv, **kwargs):
        assert not kwargs.get("text"), "the output is decoded here, not by subprocess"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake)


def test_converter_output_with_a_few_broken_sequences_keeps_its_utf8(tmp_path, monkeypatch):
    """What antiword wrote for the agenda: UTF-8 Greek, and a run of characters the Word file
    holds outside Unicode's planes, written as encoded surrogates."""
    agenda = "Ατζέντα συνεδρίασης\n1. Έγκριση πρακτικών της προηγούμενης συνεδρίασης.\n" * 4
    _converter_writes(monkeypatch, agenda.encode() + b"\xed\xbf\xad\xeb\xbf\x8f end\r\n")
    f = tmp_path / "agenda.doc"
    f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)

    result = ax._extract_doc(str(f))

    assert result["status"] == "extracted"
    assert result["text"].startswith("Ατζέντα συνεδρίασης")
    assert "�" in result["text"] and "\r" not in result["text"]


def test_converter_output_in_a_greek_code_page_is_read_as_greek(tmp_path, monkeypatch):
    agenda = "Ατζέντα της συνεδρίασης, με τα θέματα της ημέρας και τις αποφάσεις.\n" * 3
    _converter_writes(monkeypatch, agenda.encode("cp1253"))
    f = tmp_path / "agenda.doc"
    f.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)

    result = ax._extract_doc(str(f))

    assert result["text"] == agenda
