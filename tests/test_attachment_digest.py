"""A long spreadsheet is summarised from its structure, not its cells.

Phase 2 summarised a workbook's cell dump in 40,000-character parts, up to 50 of them plus a
merge, about 45 calls a workbook for the 918 workbooks over a million characters (audit
2026-10-11). The digest is built from the same dump Phase 1 stores, so it needs neither the file
(the sweep deletes it once the text is stored) nor a schema change. Every workbook here is made
in the test with openpyxl and read by the real Phase 1 reader.
"""

import re
from datetime import date, timedelta

import openpyxl
import pytest

from src.extract.attachment_digest import (
    DIGEST_MAX_CHARS,
    FREE_TEXT_CHARS,
    SAMPLE_ROWS,
    is_spreadsheet,
    spreadsheet_digest,
)
from src.extract.attachment_extractors import extract_text_from_file

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _dump(tmp_path, sheets: dict[str, list[list]]) -> str:
    """The text Phase 1 stores for a workbook holding these sheets."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    path = tmp_path / "book.xlsx"
    wb.save(path)
    result = extract_text_from_file(str(path), XLSX)
    assert result["method"] == "openpyxl" and result["status"] == "extracted"
    return result["text"]


def _ledger(n: int) -> list[list]:
    start = date(2025, 1, 1)
    rows: list[list] = [["Date", "Branch", "Amount", "Code"]]
    for i in range(n):
        rows.append([start + timedelta(days=i % 365), f"Branch {i % 7}", i + 1, f"C{i % 40}"])
    return rows


def _section(digest: str, sheet: str) -> str:
    """The part of the digest about one sheet."""
    at = digest.index(f'"{sheet}"')
    nxt = digest.find('Sheet "', at + 1)
    return digest[at : nxt if nxt != -1 else len(digest)]


def test_spreadsheet_methods_are_the_three_workbook_readers():
    for method in ("openpyxl", "pyxlsb", "xlrd", "xlrd (fallback from .xlsb)"):
        assert is_spreadsheet(method)
    for method in ("direct_read", "pymupdf", "python-docx", "zip", "pyxlsb→xlrd", "", None):
        assert not is_spreadsheet(method)


def test_the_digest_names_each_sheet_its_size_and_its_header(tmp_path):
    text = _dump(tmp_path, {"Ledger": _ledger(300), "Notes": [["Owner", "Remark"], ["A", "B"]]})

    digest = spreadsheet_digest(text)

    ledger = _section(digest, "Ledger")
    assert "301 rows" in ledger and "4 columns" in ledger
    assert "Date | Branch | Amount | Code" in ledger
    assert "Owner | Remark" in _section(digest, "Notes")


def test_column_types_and_statistics_come_from_every_row(tmp_path):
    text = _dump(tmp_path, {"Ledger": _ledger(300)})

    ledger = _section(spreadsheet_digest(text), "Ledger")

    amount = next(line for line in ledger.splitlines() if line.lstrip("- ").startswith("Amount"))
    assert "number" in amount
    assert re.search(r"min 1\b", amount) and re.search(r"max 300\b", amount)
    assert "150.50" in amount  # the mean of 1..300
    branch = next(line for line in ledger.splitlines() if line.lstrip("- ").startswith("Branch"))
    assert "text" in branch and "7 distinct" in branch
    when = next(line for line in ledger.splitlines() if line.lstrip("- ").startswith("Date"))
    assert "date" in when and "2025-01-01" in when and "2025-10-27" in when


def test_at_most_twenty_sample_rows_first_and_last(tmp_path):
    text = _dump(tmp_path, {"Ledger": _ledger(300)})

    digest = spreadsheet_digest(text)

    samples = [line for line in digest.splitlines() if re.match(r"\d{4}-\d{2}-\d{2}", line)]
    assert len(samples) == SAMPLE_ROWS
    amounts = [int(line.split(" | ")[2]) for line in samples]
    assert amounts[0] == 1 and amounts[-1] == 300  # the first data row and the last


def test_a_short_sheet_is_shown_whole(tmp_path):
    rows = [["Item", "Count"], *[[f"thing {i}", i] for i in range(5)]]
    text = _dump(tmp_path, {"Small": rows})

    digest = spreadsheet_digest(text)

    assert all(f"thing {i} | {i}" in digest for i in range(5))


def test_a_title_above_the_table_is_free_text_not_the_header(tmp_path):
    rows = [["Quarterly volumes by branch, prepared for the steering committee"], *_ledger(30)]
    text = _dump(tmp_path, {"Volumes": rows})

    section = _section(spreadsheet_digest(text), "Volumes")

    assert "Date | Branch | Amount | Code" in section
    free = section[section.index("Free text") :]
    assert "prepared for the steering committee" in free


def test_prose_cells_are_free_text_cut_at_two_thousand_characters(tmp_path):
    remark = "The branch reported a delay in the rollout because the vendor missed a delivery"
    rows = [["Ref", "Remark"], *[[f"R{i}", f"{remark} number {i}."] for i in range(200)]]
    text = _dump(tmp_path, {"Remarks": rows})

    section = _section(spreadsheet_digest(text), "Remarks")

    free = section[section.index("Free text") :].split("\n", 1)[1]
    assert "vendor missed a delivery number 15." in free  # the first remark no sample row shows
    assert "number 0." not in free  # shown once, in the sample rows
    assert len(free.strip()) <= FREE_TEXT_CHARS


def test_the_digest_of_a_long_sheet_is_a_small_share_of_its_dump(tmp_path):
    text = _dump(tmp_path, {"Ledger": _ledger(5_000)})

    digest = spreadsheet_digest(text)

    assert len(text) > 150_000 and len(digest) < len(text) // 20


def test_a_workbook_of_many_sheets_stays_under_the_cap_and_names_every_sheet(tmp_path):
    sheets = {f"Sheet {i}": _ledger(25) for i in range(40)}
    text = _dump(tmp_path, sheets)

    digest = spreadsheet_digest(text)

    assert len(digest) <= DIGEST_MAX_CHARS
    assert all(f'"Sheet {i}"' in digest for i in range(40))


def test_cells_are_clipped_so_one_long_cell_cannot_fill_the_digest(tmp_path):
    rows = [["Key", "Blob"], *[[f"k{i}", "x" * 5_000] for i in range(30)]]
    text = _dump(tmp_path, {"Blobs": rows})

    digest = spreadsheet_digest(text)

    assert len(digest) < 20_000
    assert "x" * 5_000 not in digest


def test_a_sheet_of_numbers_alone_has_no_header(tmp_path):
    rows = [[i, i * 2, i * 3] for i in range(1, 51)]
    text = _dump(tmp_path, {"Grid": rows})

    section = _section(spreadsheet_digest(text), "Grid")

    assert "no header row" in section.lower()
    assert re.search(r"min 1\b.*max 50\b", section)


def test_text_without_sheet_markers_is_read_as_one_sheet():
    text = "Name | Score\n" + "\n".join(f"person {i} | {i}" for i in range(40))

    digest = spreadsheet_digest(text)

    assert "Name | Score" in digest and "41 rows" in digest


@pytest.mark.parametrize("bad", ["", "   \n\n  "])
def test_an_empty_text_still_gives_a_digest(bad):
    assert "0 sheets" in spreadsheet_digest(bad)
