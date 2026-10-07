""".mso object containers are read: the Excel workbooks behind charts pasted into a mail.

Outlook keeps the objects pasted into an HTML mail in an oledata.mso part: a 4-byte length and a
zlib stream holding an OLE2 file, whose root streams each hold one object packed the same way.
On the producer all 255 .mso files have that shape, and their 349 objects are either a BIFF
workbook (a Workbook stream) or an Office 2007+ file (a Package stream). .mso sat in the skip
list, so their numbers never reached search.
"""

import io

from src.extract import attachment_extractors as ax
from src.extract.attachment_extractors import extract_text_from_file
from tests.ole_fixtures import (
    STREAM,
    biff8_workbook,
    mso_file,
    ole_file,
    package_object,
    packed,
    workbook_object,
)

ROWS = [
    ["Region", "Quarter", "Branch network revenue and the plan"],
    ["North", "Q1", "Twelve branches, renovated in spring"],
]
SHEET = "Revenue by region, enough words for the filter to keep the sheet"


def _xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Plan"
    wb.active.append(["Channel", "Target", "Digital sales across the year"])
    wb.active.append(["Mobile", "35%", "Most of the growth comes from the app"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_an_mso_with_a_chart_workbook_and_a_package_is_read(tmp_path):
    data = mso_file(
        [workbook_object(biff8_workbook("Chart data", [[SHEET], *ROWS])), package_object(_xlsx())]
    )

    out = extract_text_from_file(_write(tmp_path, "oledata.mso", data), "application/octet-stream")

    assert (out["status"], out["method"]) == ("extracted", "mso")
    assert "=== embedded object 1 ===" in out["text"]
    assert "Branch network revenue and the plan" in out["text"]
    assert "=== embedded object 2 ===" in out["text"]
    assert "Most of the growth comes from the app" in out["text"]


def test_the_declared_type_does_not_send_an_mso_elsewhere(tmp_path):
    """Two producer rows declare image/g3fax, one application/x-coff."""
    data = mso_file([package_object(_xlsx())])
    for mime in ("image/g3fax", "application/x-coff", "application/gzip"):
        out = extract_text_from_file(_write(tmp_path, "oledata.mso", data), mime)
        assert (out["status"], out["method"]) == ("extracted", "mso")


def test_an_object_of_another_kind_is_named_and_the_rest_kept(tmp_path):
    other = ole_file([("\x01Ole10Native", STREAM, b"\x00" * 64)])
    data = mso_file([other, package_object(_xlsx())])

    out = extract_text_from_file(_write(tmp_path, "oledata.mso", data), "")

    assert out["status"] == "extracted"
    assert "embedded object 1: no workbook or package inside" in out["error"]
    assert "Digital sales across the year" in out["text"]


def test_bytes_that_are_not_a_container_are_skipped_with_the_reason(tmp_path):
    out = extract_text_from_file(_write(tmp_path, "editdata.mso", b"\x00\x01junk" * 40), "")

    assert (out["status"], out["method"]) == ("skipped", "mso")
    assert "not an Office object container" in out["error"]


def test_a_container_that_inflates_past_the_ceiling_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "INFLATE_MAX_BYTES", 1024)
    data = packed(ole_file([("_1", STREAM, b"\x00" * 8192)]))

    out = extract_text_from_file(_write(tmp_path, "oledata.mso", data), "")

    assert out["status"] == "skipped"
    assert "inflates past" in out["error"]


def test_an_mso_with_no_readable_object_is_skipped_not_failed(tmp_path):
    data = mso_file([ole_file([("\x01Ole10Native", STREAM, b"\x00" * 64)])])

    out = extract_text_from_file(_write(tmp_path, "oledata.mso", data), "")

    assert (out["status"], out["method"]) == ("skipped", "mso")
