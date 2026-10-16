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
    cuts_columns,
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


# --- a sheet with one column per period --------------------------------------------------
#
# A daily report mailed on 12 October held every day of January to September, one column a
# day. The digest kept its first 40 columns and the first 600 characters of each row, so the
# model saw January only, and the summary and the key facts said nothing of September (audit
# 2026-10-16). A wide sheet now keeps its label columns and its newest columns.


def _daily(first: date, days: int, channels=("Cards", "Internet", "Mobile")) -> list[list]:
    """A label column, then one column a day: the shape of a daily volumes report."""
    header: list = ["Channel", *(first + timedelta(days=i) for i in range(days))]
    return [
        header,
        *[[name, *(1_000 * (k + 1) + i for i in range(days))] for k, name in enumerate(channels)],
    ]


def test_a_wide_sheet_keeps_its_label_column_and_its_newest_columns(tmp_path):
    text = _dump(tmp_path, {"Volumes": _daily(date(2026, 1, 1), 273)})  # 1 Jan to 30 Sep

    section = _section(spreadsheet_digest(text), "Volumes")

    assert "2026-09-30" in section  # the newest day
    assert "2026-01-15" not in section  # January is left out
    header = next(line for line in section.splitlines() if line.startswith("Header:"))
    assert header.startswith("Header: Channel | ")
    assert header.rstrip().endswith("2026-09-30")
    # Every row keeps its label and its newest value: 1,000 a channel plus the day's index.
    for k, name in enumerate(("Cards", "Internet", "Mobile")):
        row = next(line for line in section.splitlines() if line.startswith(f"{name} |"))
        assert row.rstrip().endswith(str(1_000 * (k + 1) + 272))


def test_a_wide_sheet_says_which_columns_it_shows(tmp_path):
    text = _dump(tmp_path, {"Volumes": _daily(date(2026, 1, 1), 273)})

    section = _section(spreadsheet_digest(text), "Volumes")

    shown = next(line for line in section.splitlines() if line.startswith("Columns shown:"))
    assert "of 274" in shown and "left out" in shown
    assert "2026-01-01" in shown  # the first column left out, by its date


def test_the_header_dates_are_named_by_month_in_greek_and_english(tmp_path):
    text = _dump(tmp_path, {"Volumes": _daily(date(2026, 1, 1), 273)})

    section = _section(spreadsheet_digest(text), "Volumes")

    dates = next(line for line in section.splitlines() if line.startswith("Dates in the header:"))
    assert "2026-01-01 to 2026-09-30" in dates
    assert "Ιανουάριος 2026" in dates and "Σεπτέμβριος 2026" in dates
    assert "January" in dates and "September 2026" in dates


def test_a_sheet_whose_newest_period_comes_first_keeps_its_first_columns(tmp_path):
    rows = _daily(date(2026, 1, 1), 273)
    rows = [[row[0], *reversed(row[1:])] for row in rows]  # 30 Sep first, 1 Jan last

    section = _section(spreadsheet_digest(_dump(tmp_path, {"Volumes": rows})), "Volumes")

    header = next(line for line in section.splitlines() if line.startswith("Header:"))
    assert header.startswith("Header: Channel | 2026-09-30 | 2026-09-29")
    assert "2026-01-01 |" not in header


def test_the_columns_shown_get_their_statistics(tmp_path):
    text = _dump(tmp_path, {"Volumes": _daily(date(2026, 1, 1), 273)})

    section = _section(spreadsheet_digest(text), "Volumes")

    newest = next(line for line in section.splitlines() if line.startswith("- 2026-09-30"))
    assert "number" in newest and re.search(r"max 3,272\b", newest)
    assert not any(line.startswith("- 2026-01-02") for line in section.splitlines())


def test_a_sheet_without_a_label_column_keeps_its_last_columns(tmp_path):
    # Dates from the first column on, one block of header and values a year, as the report
    # mailed on 12 October has them.
    header = [date(2026, 1, 1) + timedelta(days=i) for i in range(273)] + ["Year total"]
    values = [*(5_000 + i for i in range(273)), 999_999]
    text = _dump(tmp_path, {"Count": [["Daily transactions, all channels"], header, values]})

    section = _section(spreadsheet_digest(text), "Count")

    assert "2026-09-30" in section and "Year total" in section and "999,999" in section
    assert "2026-01-03" not in section


def test_a_wide_digest_stays_under_its_cap(tmp_path):
    sheets = {
        f"Year {y}": _daily(date(y, 1, 1), 365, [f"Channel {i}" for i in range(30)])
        for y in (2024, 2025, 2026)
    }
    digest = spreadsheet_digest(_dump(tmp_path, sheets))

    assert len(digest) <= DIGEST_MAX_CHARS
    assert all(f'"Year {y}"' in digest for y in (2024, 2025, 2026))


def test_a_date_column_is_named_by_month_too(tmp_path):
    text = _dump(tmp_path, {"Ledger": _ledger(300)})

    ledger = _section(spreadsheet_digest(text), "Ledger")

    when = next(line for line in ledger.splitlines() if line.lstrip("- ").startswith("Date"))
    assert "Ιανουάριος 2025" in when and "Οκτώβριος 2025" in when and "October 2025" in when


def test_a_narrow_sheet_shows_every_column_as_before(tmp_path):
    text = _dump(tmp_path, {"Ledger": _ledger(300)})

    ledger = _section(spreadsheet_digest(text), "Ledger")

    assert "Columns shown:" not in ledger
    assert "Date | Branch | Amount | Code" in ledger


def test_the_digest_prompt_asks_the_summary_to_name_the_newest_month():
    from src.extract.attachment_prompt import (
        build_attachment_prompt,
        build_attachment_request_v2,
    )

    v1 = build_attachment_prompt("digest", "book.xlsx", XLSX, digest=True)
    v2 = build_attachment_request_v2("digest", "book.xlsx", XLSX, digest=True)
    for prompt in (v1, v2.user):
        assert "name its newest month" in prompt and "Σεπτέμβριος 2026" in prompt
    assert "newest month" not in build_attachment_prompt("text", "a.pdf", "application/pdf")


def test_cut_columns_tells_a_wide_sheet_from_a_narrow_one(tmp_path):
    wide = _dump(tmp_path, {"Volumes": _daily(date(2026, 1, 1), 273)})
    narrow = _dump(tmp_path, {"Ledger": _ledger(300)})

    assert cuts_columns(wide)
    assert not cuts_columns(narrow)
    assert not cuts_columns("")
