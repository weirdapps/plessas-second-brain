"""OCR reads Greek first, a text PDF's scanned pages are read too, and Greek read as Latin
look-alike letters is marked.

With English first, tesseract read Greek capitals and many lower-case Greek letters as the Latin
letters they resemble, and about 800 images and scans became Latin text whose summaries were
wrong (audit 2026-10-11, data-quality-03). Greek first misreads some figures and signs of English
text instead, so a page whose Greek-first reading is Latin script is read again English first and
that reading is kept, unless it is itself Greek in Latin look-alikes. A text PDF used to be OCR'd
only when the whole document held under 50 characters, so a scanned annex inside it was never
read (code-extract-12). Tesseract is faked throughout: CI has none.
"""

import io
import math

import fitz
import pytest
from PIL import Image

from src.extract import attachment_extractors as ax
from src.extract.ocr_garble import is_garbled, script_profile
from tests.greek_text import ENGLISH, GREEK, garble

FIGURES = "1.204 | 33,1% | +2,4% | 12/05/2026 | 81.223.004 | 3.412.118 | 24,8 | 7 / 9"


def _tesseract(monkeypatch, readings=None, fail=None):
    """Tesseract answering by language from `readings` ("" for a language not in it), or raising
    `fail`. Returns the (image mode, language) of every call."""
    calls = []

    def fake(image, lang):
        calls.append((image.mode, lang))
        if fail is not None:
            raise fail
        return (readings or {}).get(lang, "")

    monkeypatch.setattr("pytesseract.image_to_string", fake)
    return calls


def _png(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (40, 20), (200, 30, 30)).save(path, format="PNG")
    return str(path)


def _pdf(tmp_path, *pages):
    """A PDF of `pages`: "scan" is a page holding only an image, "blank" an empty page, anything
    else a page whose text layer is that text."""
    picture = io.BytesIO()
    Image.new("RGB", (60, 30), "white").save(picture, format="PNG")
    doc = fitz.open()
    for kind in pages:
        page = doc.new_page(width=1000, height=200)
        if kind == "scan":
            page.insert_image(page.rect, stream=picture.getvalue())
        elif kind != "blank":
            page.insert_text((20, 40), kind, fontsize=8)
    path = tmp_path / "document.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


# --- the language order ---------------------------------------------------------------------


def test_a_greek_page_is_read_once_greek_first(tmp_path, monkeypatch):
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: GREEK, ax.OCR_LANGS_LATIN: garble(GREEK)})

    out = ax._extract_image_ocr(_png(tmp_path))

    assert (out["status"], out["text"], out["error"]) == ("extracted", GREEK, None)
    assert calls == [("RGB", "ell+eng")]


def test_a_latin_page_is_read_again_english_first_and_that_reading_is_kept(tmp_path, monkeypatch):
    misread = ENGLISH.replace("third", "thlrd")
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: misread, ax.OCR_LANGS_LATIN: ENGLISH})

    out = ax._extract_image_ocr(_png(tmp_path))

    assert out["text"] == ENGLISH
    assert [lang for _, lang in calls] == ["ell+eng", "eng+ell"]


def test_figures_alone_are_read_again_english_first(tmp_path, monkeypatch):
    """Greek first reads "+2,4%" as "42,48": a page of figures reads the way it did before."""
    misread = FIGURES.replace("+2,4%", "42,48")
    _tesseract(monkeypatch, {ax.OCR_LANGS: misread, ax.OCR_LANGS_LATIN: FIGURES})

    assert ax._extract_image_ocr(_png(tmp_path))["text"] == FIGURES


def test_a_greek_page_neither_order_reads_keeps_the_greek_first_reading(tmp_path, monkeypatch):
    poor = garble(GREEK[:220]) + " " + GREEK[220:]
    assert script_profile(poor).greek_share <= 0.3
    _tesseract(monkeypatch, {ax.OCR_LANGS: poor, ax.OCR_LANGS_LATIN: garble(GREEK)})

    assert ax._extract_image_ocr(_png(tmp_path))["text"] == poor


def test_an_image_without_text_is_read_greek_first_in_colour_and_in_grayscale(
    tmp_path, monkeypatch
):
    calls = _tesseract(monkeypatch)

    out = ax._extract_image_ocr(_png(tmp_path))

    assert (out["status"], out["error"]) == ("skipped", ax.OCR_INSUFFICIENT)
    assert calls == [("RGB", "ell+eng"), ("L", "ell+eng")]


def test_every_page_of_a_scan_is_read_greek_first(tmp_path, monkeypatch):
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: GREEK})

    out = ax._extract_pdf(_pdf(tmp_path, "blank", "blank"))

    assert (out["method"], out["text"]) == ("pymupdf+tesseract", f"{GREEK}\n\n{GREEK}")
    assert [lang for _, lang in calls] == ["ell+eng", "ell+eng"]


# --- the scanned pages of a text PDF ---------------------------------------------------------


def test_a_scanned_page_inside_a_text_pdf_is_read(tmp_path, monkeypatch):
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: GREEK})

    out = ax._extract_pdf(_pdf(tmp_path, ENGLISH[:120], "scan"))

    assert (out["status"], out["method"], out["error"]) == ("extracted", "pymupdf+tesseract", None)
    assert ENGLISH[:60] in out["text"] and GREEK in out["text"]
    assert out["text"].index(ENGLISH[:60]) < out["text"].index(GREEK)
    assert len(calls) == 1


def test_a_blank_page_of_a_text_pdf_is_not_ocrd(tmp_path, monkeypatch):
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: GREEK})

    out = ax._extract_pdf(_pdf(tmp_path, ENGLISH[:120], "blank"))

    assert (out["method"], calls) == ("pymupdf", [])
    assert ENGLISH[:60] in out["text"]


def test_a_text_pdf_without_thin_pages_is_read_as_before(tmp_path, monkeypatch):
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: GREEK})

    out = ax._extract_pdf(_pdf(tmp_path, ENGLISH[:120], ENGLISH[120:240]))

    assert (out["status"], out["method"], out["error"], calls) == (
        "extracted",
        "pymupdf",
        None,
        [],
    )


def test_the_scanned_pages_of_a_text_pdf_keep_to_the_ocr_budget(tmp_path, monkeypatch):
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: GREEK})

    out = ax._extract_pdf(_pdf(tmp_path, ENGLISH[:120], "scan", "scan", "scan"), ocr_seconds=5)

    assert out["status"] == "extracted"
    assert "pages left unread" in out["error"]
    assert len(calls) == 1 and "2 pages left unread" in out["error"]


def test_with_no_budget_every_scanned_page_of_a_text_pdf_is_read(tmp_path, monkeypatch):
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: GREEK})

    out = ax._extract_pdf(
        _pdf(tmp_path, ENGLISH[:120], "scan", "scan", "scan"), ocr_seconds=math.inf
    )

    assert (len(calls), out["error"]) == (3, None)
    assert out["text"].count(GREEK) == 3


def test_a_text_pdf_whose_scanned_pages_cannot_be_ocrd_keeps_its_text_and_its_file(
    tmp_path, monkeypatch
):
    """A host without tesseract read this PDF's text before; it still does, and the error says
    pages were left unread, so the sweep keeps the file and reextract --unread finishes it."""
    _tesseract(monkeypatch, fail=OSError("tesseract is not installed"))

    out = ax._extract_pdf(_pdf(tmp_path, ENGLISH[:120], "scan"))

    assert (out["status"], out["method"]) == ("extracted", "pymupdf")
    assert ENGLISH[:60] in out["text"]
    assert "left unread" in out["error"]


def test_a_text_pdf_whose_pages_cannot_be_inspected_keeps_its_text(tmp_path, monkeypatch):
    """A fault in looking for scanned pages must not cost the text layer already read."""
    calls = _tesseract(monkeypatch, {ax.OCR_LANGS: GREEK})

    def broken(self, *args, **kwargs):
        raise RuntimeError("cannot read the page's resources")

    monkeypatch.setattr(fitz.Page, "get_image_info", broken)

    out = ax.extract_text_from_file(_pdf(tmp_path, ENGLISH[:120], "scan"), "application/pdf")

    assert (out["status"], out["method"], calls) == ("extracted", "pymupdf", [])
    assert ENGLISH[:60] in out["text"]
    assert "left unread" in out["error"]


# --- the mark --------------------------------------------------------------------------------


def test_an_image_read_as_greek_look_alikes_is_marked_and_its_text_kept(tmp_path, monkeypatch):
    _tesseract(monkeypatch, {ax.OCR_LANGS: garble(GREEK), ax.OCR_LANGS_LATIN: garble(GREEK)})

    out = ax.extract_text_from_file(_png(tmp_path), "image/png")

    assert (out["status"], out["text"]) == ("extracted", garble(GREEK))
    assert is_garbled(out["error"])


def test_a_scan_read_as_greek_look_alikes_is_marked(tmp_path, monkeypatch):
    _tesseract(monkeypatch, {ax.OCR_LANGS: garble(GREEK), ax.OCR_LANGS_LATIN: garble(GREEK)})

    out = ax.extract_text_from_file(_pdf(tmp_path, "blank"), "application/pdf")

    assert out["method"] == "pymupdf+tesseract"
    assert is_garbled(out["error"])


@pytest.mark.parametrize("reading", [GREEK, ENGLISH], ids=["greek", "english"])
def test_a_clean_reading_is_not_marked(tmp_path, monkeypatch, reading):
    _tesseract(monkeypatch, {ax.OCR_LANGS: reading, ax.OCR_LANGS_LATIN: reading})

    out = ax.extract_text_from_file(_png(tmp_path), "image/png")

    assert (out["status"], out["error"]) == ("extracted", None)


def test_a_text_layer_is_not_judged(tmp_path, monkeypatch):
    """Only OCR misreads Greek this way; a PDF's own text is the text its author wrote."""
    calls = _tesseract(monkeypatch)
    look_alike = garble(GREEK)[:150]

    out = ax.extract_text_from_file(_pdf(tmp_path, look_alike), "application/pdf")

    assert (out["method"], out["error"], calls) == ("pymupdf", None, [])
