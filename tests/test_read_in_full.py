"""Spreadsheets, scanned PDFs and text files are read in full.

The old readers stopped at 20 sheets of 51 rows, at 30 scanned pages, and at 2,000,000
characters. Now every sheet and row is read, every page is OCR'd inside a time budget, and text
stops only at a 50,000,000-character ceiling against runaway input. A read that stops short says
what it left unread, and the sweep keeps its file. Rows the old caps read in part are kept too,
until reextract --partial reads them again. Phase 2 summarises a very long text from 50 parts
spread across it.
"""

import hashlib
import math
from unittest.mock import MagicMock, patch

import pytest

from src.extract import attachment_extractors as ax
from src.store.file_sweep import SweepPolicy, sweep_files
from src.store.schema import create_database

WORDS = "The plan for the regional network, in enough words to pass the filter. "
APPLY = SweepPolicy(apply=True)


def test_every_sheet_and_every_row_of_a_workbook_is_read(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for s in range(1, 26):
        ws = wb.create_sheet(f"Sheet{s}")
        for r in range(1, 61):
            ws.append([f"s{s}r{r}", "value"])
    path = tmp_path / "big.xlsx"
    wb.save(path)

    text = ax._extract_excel(str(path))["text"]

    assert "--- Sheet: Sheet25 ---" in text
    assert "s25r60" in text


def test_every_sheet_and_every_row_of_a_legacy_workbook_is_read():
    def sheet(name):
        s = MagicMock()
        s.name = name
        s.nrows = 60
        s.row_values.side_effect = lambda i: [f"{name}r{i}", "value"]
        return s

    book = MagicMock()
    book.sheets.return_value = [sheet(f"S{n}") for n in range(1, 26)]
    with patch("xlrd.open_workbook", return_value=book):
        text = ax._extract_xls("/tmp/fake.xls")["text"]

    assert "S25r59" in text


def test_every_sheet_and_every_row_of_a_binary_workbook_is_read():
    def cell(v):
        c = MagicMock()
        c.v = v
        return c

    rows = {f"S{n}": [[cell(f"S{n}r{r}"), cell("value")] for r in range(60)] for n in range(1, 26)}

    def get_sheet(name):
        handle = MagicMock()
        handle.__enter__.return_value.rows.return_value = iter(rows[name])
        handle.__exit__.return_value = False
        return handle

    wb = MagicMock()
    wb.sheets = list(rows)
    wb.get_sheet.side_effect = get_sheet
    with patch("pyxlsb.open_workbook") as opened:
        opened.return_value.__enter__.return_value = wb
        opened.return_value.__exit__.return_value = False
        text = ax._extract_xlsb("/tmp/fake.xlsb")["text"]

    assert "S25r59" in text


def _scan(pages):
    page = MagicMock()
    page.get_pixmap.return_value.tobytes.return_value = b"png"
    doc = MagicMock()
    doc.__iter__.return_value = iter([page] * pages)
    doc.page_count = pages
    numbers = iter(range(1, pages + 1))
    return (
        patch("fitz.open", return_value=doc),
        patch("PIL.Image.open"),
        patch(
            "pytesseract.image_to_string",
            side_effect=lambda img, lang: f"Page {next(numbers)} {WORDS}",
        ),
    )


def test_every_page_of_a_scan_is_read():
    opened, image, ocr = _scan(35)
    with opened, image, ocr:
        result = ax._ocr_pdf_pages("/tmp/scan.pdf", seconds=math.inf)

    assert "Page 35 " in result["text"]
    assert result["error"] is None


def test_a_scan_its_time_budget_cuts_short_keeps_what_it_read_and_says_so(monkeypatch):
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    opened, image, ocr = _scan(5)
    with opened, image, ocr:
        result = ax._ocr_pdf_pages("/tmp/scan.pdf", seconds=25)

    assert result["status"] == "extracted"
    assert "Page 1 " in result["text"] and "Page 5 " not in result["text"]
    assert "pages left unread" in result["error"]


def test_the_ocr_budget_reaches_a_scanned_pdf(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        ax,
        "_ocr_pdf_pages",
        lambda path, seconds=None: (
            seen.append(seconds)
            or {"text": None, "method": "pymupdf+tesseract", "status": "skipped", "error": "x"}
        ),
    )
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 scanned")
    page = MagicMock()
    page.get_text.return_value = ""

    for budget in (math.inf, None):
        doc = MagicMock()
        doc.__iter__.return_value = iter([page])
        with patch("fitz.open", return_value=doc):
            ax.extract_text_from_file(str(pdf), "application/pdf", ocr_seconds=budget)

    assert seen == [math.inf, ax.OCR_MAX_SECONDS]


def test_the_text_ceiling_is_fifty_million_characters():
    assert ax.MAX_TEXT_CHARS == 50_000_000


def test_a_text_cut_at_the_ceiling_says_the_rest_was_left_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "MAX_TEXT_CHARS", 1_000)
    log = tmp_path / "log.txt"
    log.write_text(WORDS * 100)

    result = ax.extract_text_from_file(str(log), "text/plain")

    assert len(result["text"]) == 1_000
    assert result["status"] == "extracted"
    assert "left unread" in result["error"]


def test_a_very_long_text_is_summarised_from_fifty_parts_spread_across_it(monkeypatch):
    from src.extract import attachment_pipeline as ap
    from src.extract import attachment_prompt as prompt

    monkeypatch.setattr(prompt, "split_text", lambda text: [f"part {i}" for i in range(1, 121)])
    asked, merged = [], []
    monkeypatch.setattr(
        prompt, "build_attachment_prompt", lambda **kw: asked.append(kw["part"]) or "p"
    )
    monkeypatch.setattr(
        prompt,
        "build_merge_prompt",
        lambda parts, **kw: merged.append((len(parts), kw["covered"])) or "m",
    )
    monkeypatch.setattr(ap, "_complete_and_parse", lambda _prompt: {"summary": "s"})

    ap._extract_in_parts("x", "log.txt", "text/plain", None, None, None)

    assert len(asked) == 50
    assert (asked[0], asked[-1]) == ((1, 120), (120, 120))
    assert merged == [(50, (50, 120))]


def test_a_long_text_of_fifty_parts_or_fewer_is_summarised_whole(monkeypatch):
    from src.extract import attachment_pipeline as ap
    from src.extract import attachment_prompt as prompt

    monkeypatch.setattr(prompt, "split_text", lambda text: [f"part {i}" for i in range(1, 31)])
    asked, merged = [], []
    monkeypatch.setattr(
        prompt, "build_attachment_prompt", lambda **kw: asked.append(kw["part"]) or "p"
    )
    monkeypatch.setattr(
        prompt,
        "build_merge_prompt",
        lambda parts, **kw: merged.append((len(parts), kw["covered"])) or "m",
    )
    monkeypatch.setattr(ap, "_complete_and_parse", lambda _prompt: {"summary": "s"})

    ap._extract_in_parts("x", "log.txt", "text/plain", None, None, None)

    assert len(asked) == 30
    assert merged == [(30, (30, 30))]


def test_the_merge_prompt_says_how_much_of_the_document_it_covers():
    from src.extract.attachment_prompt import build_merge_prompt

    text = build_merge_prompt(
        [{"summary": "a"}], filename="log.txt", mime_type="text/plain", covered=(50, 120)
    )

    assert "50 of 120" in text


def _file_row(
    db, root, name, *, method, text=WORDS, extracted_at="2026-09-01T10:00:00", error=None
):
    d = root / "AAMk-1"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"data")
    cur = db.execute(
        "INSERT INTO attachments (message_id, filename, mime_type, file_size, file_path,"
        " exported_at, sha256) VALUES ('AAMk-1', ?, 'application/octet-stream', 4, ?,"
        " '2026-09-01', ?)",
        (name, str(f), hashlib.sha256(name.encode()).hexdigest()),
    )
    db.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error, extracted_at, llm_status)"
        " VALUES (?, ?, ?, 'extracted', ?, ?, 'extracted')",
        (cur.lastrowid, text, method, error, extracted_at),
    )
    db.commit()
    return f


@pytest.mark.parametrize("method", ["openpyxl", "xlrd", "pyxlsb", "pymupdf+tesseract", "zip"])
def test_a_file_the_old_caps_read_in_part_is_kept_until_read_again(tmp_path, method):
    db = create_database(":memory:")
    f = _file_row(db, tmp_path, "book.bin", method=method)

    stats = sweep_files(db, tmp_path, APPLY)

    assert f.exists() and stats["unread"] == 1


def test_a_text_at_the_old_ceiling_is_kept_until_read_again(tmp_path):
    db = create_database(":memory:")
    f = _file_row(db, tmp_path, "log.txt", method="direct_read", text="x" * 2_000_000)

    sweep_files(db, tmp_path, APPLY)

    assert f.exists()


def test_a_file_read_again_after_the_backfill_began_may_go(tmp_path):
    db = create_database(":memory:")
    db.execute(
        "INSERT INTO sync_metadata (key, value)"
        " VALUES ('reextract_partial_since', '2026-10-01T00:00:00')"
    )
    old = _file_row(db, tmp_path, "old.xlsx", method="openpyxl")
    new = _file_row(db, tmp_path, "new.xlsx", method="openpyxl", extracted_at="2026-10-02T10:00:00")

    sweep_files(db, tmp_path, APPLY)

    assert old.exists() and not new.exists()


@pytest.mark.parametrize(
    "error",
    [
        "time budget spent, 12 pages left unread",
        "text cut at 50,000,000 characters, the rest left unread",
    ],
)
def test_a_file_read_in_part_is_kept(tmp_path, error):
    db = create_database(":memory:")
    f = _file_row(db, tmp_path, "scan.pdf", method="pymupdf", error=error)

    sweep_files(db, tmp_path, APPLY)

    assert f.exists()
