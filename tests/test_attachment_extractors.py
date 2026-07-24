import os
import tempfile


def test_extract_text_file():
    from src.extract.attachment_extractors import extract_text_from_file

    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, encoding="utf-8") as f:
        f.write("Hello world, this is a test document with enough text to pass the threshold.")
        path = f.name
    try:
        result = extract_text_from_file(path, "text/plain")
        assert result["status"] == "extracted"
        assert result["method"] == "direct_read"
        assert "Hello world" in result["text"]
    finally:
        os.unlink(path)


def test_extract_html_strips_tags():
    from src.extract.attachment_extractors import extract_text_from_file

    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", delete=False, encoding="utf-8") as f:
        f.write(
            "<html><body><h1>Title</h1><p>This is paragraph text with enough content to pass.</p></body></html>"
        )
        path = f.name
    try:
        result = extract_text_from_file(path, "text/html")
        assert result["status"] == "extracted"
        assert "<html>" not in result["text"]
        assert "Title" in result["text"]
    finally:
        os.unlink(path)


def test_extract_csv():
    from src.extract.attachment_extractors import extract_text_from_file

    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, encoding="utf-8") as f:
        f.write(
            "Name,Value,Description\nAlpha,100,First item in the list\nBeta,200,Second item in the list\n"
        )
        path = f.name
    try:
        result = extract_text_from_file(path, "text/csv")
        assert result["status"] == "extracted"
        assert "Alpha" in result["text"]
    finally:
        os.unlink(path)


def test_extract_missing_file():
    from src.extract.attachment_extractors import extract_text_from_file

    result = extract_text_from_file("/nonexistent/path.pdf", "application/pdf")
    assert result["status"] == "failed"
    assert result["error"] is not None


def test_extract_skipped_mime_type():
    from src.extract.attachment_extractors import extract_text_from_file

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"\x00" * 100)
        path = f.name
    try:
        result = extract_text_from_file(path, "video/mp4")
        assert result["status"] == "skipped"
    finally:
        os.unlink(path)


def test_noise_filter_short_text():
    from src.extract.attachment_extractors import _apply_noise_filter

    assert _apply_noise_filter("ab") is True  # too short, should be filtered
    assert _apply_noise_filter("A" * 60) is False  # long enough, not filtered


def test_truncation():
    from src.extract.attachment_extractors import _truncate

    text = "A" * 200_000
    result = _truncate(text, max_chars=100_000)
    assert len(result) == 100_000


def test_extract_xls_returns_extracted_on_valid_file():
    """Mock xlrd to simulate a valid .xls file with one sheet, two rows."""
    from unittest.mock import MagicMock, patch

    from src.extract.attachment_extractors import _extract_xls

    fake_sheet = MagicMock()
    fake_sheet.name = "Q1 Report"
    fake_sheet.nrows = 2
    fake_sheet.row_values.side_effect = [
        ["Name", "Value", "Notes"],
        ["Alpha", 100, "First quarter sales"],
    ]
    fake_book = MagicMock()
    fake_book.sheets.return_value = [fake_sheet]

    with patch("xlrd.open_workbook", return_value=fake_book):
        result = _extract_xls("/tmp/fake.xls")

    assert result["status"] == "extracted"
    assert result["method"] == "xlrd"
    assert "Q1 Report" in result["text"]
    assert "Alpha" in result["text"]
    assert "First quarter sales" in result["text"]
    assert result["error"] is None


def test_extract_xls_returns_failed_on_corrupt_file(tmp_path):
    """Real xlrd call on garbage content should fail cleanly with status='failed'."""
    from src.extract.attachment_extractors import _extract_xls

    bad = tmp_path / "garbage.xls"
    bad.write_bytes(b"this is not a real .xls file")
    result = _extract_xls(str(bad))

    assert result["status"] == "failed"
    assert result["method"] == "xlrd"
    assert result["text"] is None
    assert result["error"] is not None
    assert len(result["error"]) > 0


def test_extract_xlsb_returns_extracted_on_valid_file():
    """Mock pyxlsb to simulate a valid .xlsb file."""
    from unittest.mock import MagicMock, patch

    from src.extract.attachment_extractors import _extract_xlsb

    fake_cell_a1 = MagicMock()
    fake_cell_a1.v = "Header1"
    fake_cell_a2 = MagicMock()
    fake_cell_a2.v = "Header2"
    fake_cell_b1 = MagicMock()
    fake_cell_b1.v = "Data1"
    fake_cell_b2 = MagicMock()
    fake_cell_b2.v = "Data2"

    fake_sheet = MagicMock()
    fake_sheet.rows.return_value = iter(
        [
            [fake_cell_a1, fake_cell_a2],
            [fake_cell_b1, fake_cell_b2],
        ]
    )

    fake_wb = MagicMock()
    fake_wb.sheets = ["Inventory"]
    fake_wb.get_sheet.return_value.__enter__.return_value = fake_sheet
    fake_wb.get_sheet.return_value.__exit__.return_value = False

    with patch("pyxlsb.open_workbook") as mock_open:
        mock_open.return_value.__enter__.return_value = fake_wb
        mock_open.return_value.__exit__.return_value = False
        result = _extract_xlsb("/tmp/fake.xlsb")

    assert result["status"] == "extracted"
    assert result["method"] == "pyxlsb"
    assert "Inventory" in result["text"]
    assert "Header1" in result["text"]
    assert "Data1" in result["text"]
    assert result["error"] is None


def test_extract_xlsb_returns_failed_on_corrupt_file(tmp_path):
    """Real pyxlsb call on garbage content should fail cleanly with status='failed'."""
    from src.extract.attachment_extractors import _extract_xlsb

    bad = tmp_path / "garbage.xlsb"
    bad.write_bytes(b"this is not a real .xlsb file")
    result = _extract_xlsb(str(bad))

    assert result["status"] == "failed"
    assert result["method"] == "pyxlsb"
    assert result["text"] is None
    assert result["error"] is not None
    assert len(result["error"]) > 0


def test_dispatcher_routes_xls_to_extract_xls(tmp_path):
    """Dispatcher must call _extract_xls for .xls files (not _extract_excel)."""
    from unittest.mock import patch

    from src.extract.attachment_extractors import extract_text_from_file

    f = tmp_path / "report.xls"
    f.write_bytes(b"placeholder")

    with (
        patch("src.extract.attachment_extractors._extract_xls") as mock_xls,
        patch("src.extract.attachment_extractors._extract_excel") as mock_excel,
    ):
        mock_xls.return_value = {
            "text": "ok",
            "method": "xlrd",
            "status": "extracted",
            "error": None,
        }
        result = extract_text_from_file(str(f), "application/vnd.ms-excel")

    mock_xls.assert_called_once()
    mock_excel.assert_not_called()
    assert result["method"] == "xlrd"


def test_dispatcher_routes_xlsb_to_extract_xlsb(tmp_path):
    """Dispatcher must call _extract_xlsb for .xlsb files (not _extract_excel)."""
    from unittest.mock import patch

    from src.extract.attachment_extractors import extract_text_from_file

    f = tmp_path / "report.xlsb"
    f.write_bytes(b"placeholder")

    with (
        patch("src.extract.attachment_extractors._extract_xlsb") as mock_xlsb,
        patch("src.extract.attachment_extractors._extract_excel") as mock_excel,
    ):
        mock_xlsb.return_value = {
            "text": "ok",
            "method": "pyxlsb",
            "status": "extracted",
            "error": None,
        }
        result = extract_text_from_file(str(f), "application/octet-stream")

    mock_xlsb.assert_called_once()
    mock_excel.assert_not_called()
    assert result["method"] == "pyxlsb"


def test_dispatcher_still_routes_xlsx_to_extract_excel(tmp_path):
    """Regression: .xlsx must still go to _extract_excel (openpyxl)."""
    from unittest.mock import patch

    from src.extract.attachment_extractors import extract_text_from_file

    f = tmp_path / "report.xlsx"
    f.write_bytes(b"placeholder")

    with patch("src.extract.attachment_extractors._extract_excel") as mock_excel:
        mock_excel.return_value = {
            "text": "ok",
            "method": "openpyxl",
            "status": "extracted",
            "error": None,
        }
        result = extract_text_from_file(
            str(f),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    mock_excel.assert_called_once()
    assert result["method"] == "openpyxl"


def test_extract_pptx_skips_non_table_graphic_frame_quietly():
    """Regression for hasattr(shape, 'table') bug. A GraphicFrame whose
    has_table is False (chart, SmartArt, OLE) MUST NOT trigger access to
    shape.table — that property raises ValueError on real python-pptx
    objects, and pre-fix the unhandled exception aborted _extract_pptx
    entirely. Post-fix: the shape is skipped, function continues, slide
    text from other shapes is still extracted.

    Uses PropertyMock to make .table access raise ValueError on the chart
    shape, mirroring real python-pptx behavior. Pre-fix this test would
    surface the ValueError; post-fix it completes normally."""
    from unittest.mock import MagicMock, PropertyMock, patch

    from src.extract.attachment_extractors import _extract_pptx

    # Real text content long enough to pass the _apply_noise_filter threshold
    text_para = MagicMock()
    text_para.text = "Slide title - important content with enough length for the noise filter"
    text_shape = MagicMock()
    text_shape.has_text_frame = True
    text_shape.has_table = False  # text frames are not tables
    text_shape.text_frame.paragraphs = [text_para]

    chart_shape = MagicMock()
    chart_shape.has_text_frame = False
    chart_shape.has_table = False
    # PropertyMock makes .table access raise — mirrors real python-pptx GraphicFrame
    type(chart_shape).table = PropertyMock(side_effect=ValueError("shape does not contain a table"))

    fake_slide = MagicMock()
    fake_slide.shapes = [text_shape, chart_shape]
    fake_slide.has_notes_slide = False

    fake_prs = MagicMock()
    fake_prs.slides = [fake_slide]

    with patch("pptx.Presentation", return_value=fake_prs):
        result = _extract_pptx("/tmp/fake.pptx")

    # The contract: no unhandled ValueError leaked, and the text content was extracted
    assert result["status"] == "extracted", (
        f"got status={result['status']}, error={result.get('error')}"
    )
    assert "Slide title - important content" in result["text"]
    # And the buggy error message must NOT appear
    assert result["error"] is None or "shape does not contain a table" not in (
        result["error"] or ""
    )


def test_extract_pptx_extracts_table_when_present():
    """When a shape genuinely IS a table, its rows must be extracted as 'cell | cell | cell' lines."""
    from unittest.mock import MagicMock, patch

    from src.extract.attachment_extractors import _extract_pptx

    # Multiple table rows so the joined text comfortably exceeds the noise-filter threshold
    def _row(*texts):
        cells = []
        for t in texts:
            cell = MagicMock()
            cell.text = t
            cells.append(cell)
        row = MagicMock()
        row.cells = cells
        return row

    table = MagicMock()
    table.rows = [
        _row("Quarter", "Revenue", "Notes"),
        _row("Q1 2026", "1,250,000 EUR", "Strong start to the year"),
        _row("Q2 2026", "1,480,000 EUR", "Continued growth in cards segment"),
    ]
    table_shape = MagicMock()
    table_shape.has_text_frame = False
    table_shape.has_table = True
    table_shape.table = table

    fake_slide = MagicMock()
    fake_slide.shapes = [table_shape]
    fake_slide.has_notes_slide = False

    fake_prs = MagicMock()
    fake_prs.slides = [fake_slide]

    with patch("pptx.Presentation", return_value=fake_prs):
        result = _extract_pptx("/tmp/fake.pptx")

    assert result["status"] == "extracted", (
        f"got status={result['status']}, error={result.get('error')}"
    )
    assert "Quarter | Revenue | Notes" in result["text"]
    assert "Q2 2026 | 1,480,000 EUR | Continued growth in cards segment" in result["text"]


def test_ocr_pdf_pages_concatenates_per_page_text():
    """Mock fitz + pytesseract to verify per-page rendering, OCR call, and concatenation.
    Pre-fix (when _extract_pdf called _extract_image_ocr on the PDF path): every
    scanned PDF silently failed OCR because Image.open() can't read PDFs."""
    from unittest.mock import MagicMock, patch

    from src.extract.attachment_extractors import _ocr_pdf_pages

    fake_pix = MagicMock()
    fake_pix.tobytes.return_value = b"fake_png_bytes"
    fake_page1 = MagicMock()
    fake_page1.get_pixmap.return_value = fake_pix
    fake_page2 = MagicMock()
    fake_page2.get_pixmap.return_value = fake_pix

    fake_doc = MagicMock()
    fake_doc.__iter__.return_value = iter([fake_page1, fake_page2])
    fake_doc.close.return_value = None

    page_texts = iter(
        [
            "First page content from scanned ACME document with sufficient length to pass filter",
            "Second page continues with additional regulatory content for completeness",
        ]
    )

    with (
        patch("fitz.open", return_value=fake_doc),
        patch("PIL.Image.open"),
        patch(
            "pytesseract.image_to_string",
            side_effect=lambda img, lang: next(page_texts),
        ),
    ):
        result = _ocr_pdf_pages("/tmp/fake.pdf")

    assert result["status"] == "extracted", (
        f"got status={result['status']}, error={result.get('error')}"
    )
    assert result["method"] == "pymupdf+tesseract"
    assert "First page content" in result["text"]
    assert "Second page continues" in result["text"]


def test_ocr_pdf_pages_returns_failed_on_corrupt_pdf(tmp_path):
    """Real fitz call on garbage content should fail cleanly with status='failed'."""
    from src.extract.attachment_extractors import _ocr_pdf_pages

    bad = tmp_path / "garbage.pdf"
    bad.write_bytes(b"not a real PDF")
    result = _ocr_pdf_pages(str(bad))

    assert result["status"] == "failed"
    assert result["method"] == "pymupdf+tesseract"
    assert result["text"] is None
    assert result["error"] is not None


def test_extract_xlsb_skips_ole_with_no_workbook(tmp_path):
    """OLE compound document but no Excel workbook stream → status='skipped',
    not 'failed'. These come from custom export tools that save with a .xlsb
    extension but the OLE container has no workbook payload — neither pyxlsb
    (zip-based) nor xlrd (OLE BIFF) can rescue them."""
    from unittest.mock import patch

    from src.extract.attachment_extractors import _extract_xlsb

    bad = tmp_path / "fake.xlsb"
    bad.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)

    fake_pyxlsb_err = type("BadZipFile", (Exception,), {})("File is not a zip file")
    with patch("pyxlsb.open_workbook", side_effect=fake_pyxlsb_err):
        fake_xlrd_err = type("XLRDError", (Exception,), {})(
            "Can't find workbook in OLE2 compound document"
        )
        with patch("xlrd.open_workbook", side_effect=fake_xlrd_err):
            result = _extract_xlsb(str(bad))

    assert result["status"] == "skipped", (
        f"got status={result['status']}, error={result.get('error')}"
    )
    assert "no Excel workbook stream" in result["error"]
    assert result["text"] is None
