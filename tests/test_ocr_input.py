"""OCR reads in colour first, and in grayscale only when colour finds too little; and a size bound.

Tesseract thresholds a colour image channel by channel, which reads most images best: on 40
producer screenshots a grayscale read found 2% more words in all but fewer on 5 of them. Where
the colour read came back with too little text, grayscale recovered 4 of 44 scanned PDFs and about
12% of content images. So grayscale is a second pass for those, and an image that reads today
reads the same. A transparent image is flattened onto white before grayscale: converted straight,
a transparent background over black pixels turns black and hides black text. An image past
OCR_MAX_PIXELS (Pillow's own decompression-bomb threshold) is skipped with that reason rather than
recorded as a failure, whatever pixel limit another module sets for its own work.
"""

import io
import math

import pytest
from PIL import Image, ImageDraw

from src.extract import attachment_extractors as ax

WORDS = "Branch network review for the coming quarter, read from a scanned page"


def _tesseract(monkeypatch, reads_in):
    """Tesseract that finds WORDS in an image whose mode is in `reads_in`, nothing in others."""
    seen = []

    def fake(image, lang):
        copy = image.copy()
        copy.format = image.format  # copy() drops it
        seen.append(copy)
        return WORDS if image.mode in reads_in else ""

    monkeypatch.setattr("pytesseract.image_to_string", fake)
    return seen


def _png(tmp_path, image, name="shot.png"):
    path = tmp_path / name
    image.save(path, format="PNG")
    return str(path)


def test_an_image_that_reads_in_colour_is_read_once(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in={"RGB", "L"})

    out = ax._extract_image_ocr(_png(tmp_path, Image.new("RGB", (40, 20), (200, 30, 30))))

    assert out["status"] == "extracted"
    assert [img.mode for img in seen] == ["RGB"]


def test_too_little_in_colour_is_read_again_in_grayscale(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in={"L"})

    out = ax._extract_image_ocr(_png(tmp_path, Image.new("RGB", (40, 20), (200, 30, 30))))

    assert (out["status"], out["text"]) == ("extracted", WORDS)
    assert [img.mode for img in seen] == ["RGB", "L"]


def test_too_little_in_both_is_still_a_skip(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in=set())

    out = ax._extract_image_ocr(_png(tmp_path, Image.new("RGB", (40, 20), "white")))

    assert (out["status"], out["error"]) == ("skipped", ax.OCR_INSUFFICIENT)
    assert len(seen) == 2


def test_transparency_is_flattened_onto_white_before_grayscale(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in={"L"})
    shot = Image.new("RGBA", (40, 20), (0, 0, 0, 0))  # transparent, over black
    ImageDraw.Draw(shot).rectangle((5, 5, 15, 15), fill=(0, 0, 0, 255))  # opaque black "text"

    ax._extract_image_ocr(_png(tmp_path, shot))

    gray = seen[-1]
    assert gray.mode == "L"
    assert gray.getpixel((30, 2)) == 255  # the transparent background reads as white paper
    assert gray.getpixel((10, 10)) == 0  # the opaque mark stays black


def _blank_pdf(tmp_path):
    import fitz

    doc = fitz.open()
    doc.new_page(width=200, height=100)
    pdf = tmp_path / "scan.pdf"
    doc.save(str(pdf))
    return str(pdf)


def test_a_scan_with_too_little_in_colour_is_read_again_in_grayscale(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in={"L"})

    out = ax._extract_pdf(_blank_pdf(tmp_path))

    assert (out["status"], out["method"]) == ("extracted", "pymupdf+tesseract")
    assert [img.mode for img in seen] == ["RGB", "L"]


def test_a_scan_that_reads_in_colour_is_read_once(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in={"RGB", "L"})

    out = ax._extract_pdf(_blank_pdf(tmp_path))

    assert out["status"] == "extracted"
    assert [img.mode for img in seen] == ["RGB"]


def test_a_scan_cut_short_is_not_read_again(monkeypatch, tmp_path):
    calls = []
    cut = "OCR returned insufficient text; time budget spent, 4 pages left unread"
    monkeypatch.setattr(
        ax,
        "_ocr_pdf_pages",
        lambda path, seconds=None, gray=False: (
            calls.append(gray)
            or {"text": None, "method": "pymupdf+tesseract", "status": "skipped", "error": cut}
        ),
    )

    out = ax._extract_pdf(_blank_pdf(tmp_path))

    assert calls == [False]
    assert "left unread" in out["error"]


def test_the_grayscale_pass_of_a_scan_gets_what_is_left_of_the_budget(monkeypatch, tmp_path):
    budgets = []
    monkeypatch.setattr(
        ax,
        "_ocr_pdf_pages",
        lambda path, seconds=None, gray=False: (
            budgets.append((gray, seconds))
            or {"text": None, "method": "pymupdf+tesseract", "status": "skipped", "error": "x"}
        ),
    )

    ax._extract_pdf(_blank_pdf(tmp_path), ocr_seconds=30)
    ax._extract_pdf(_blank_pdf(tmp_path), ocr_seconds=math.inf)

    assert [gray for gray, _ in budgets] == [False, True, False, True]
    assert budgets[0][1] == 30 and 0 <= budgets[1][1] <= 30
    assert budgets[2][1] == budgets[3][1] == math.inf


def test_an_image_past_the_pixel_bound_is_skipped_not_ocrd(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in={"RGB", "L"})
    monkeypatch.setattr(ax, "OCR_MAX_PIXELS", 100)

    out = ax._extract_image_ocr(_png(tmp_path, Image.new("RGB", (20, 20), "white")))

    assert (out["status"], out["method"]) == ("skipped", "ocr")
    assert out["error"] == "image too large to OCR: 400 pixels, over the 100 limit"
    assert seen == []


def test_a_decompression_bomb_is_skipped_with_the_reason(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in={"RGB", "L"})
    path = _png(tmp_path, Image.new("RGB", (20, 20), "white"))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)  # Pillow refuses past twice this

    out = ax._extract_image_ocr(path)

    assert (out["status"], out["method"]) == ("skipped", "ocr")
    assert out["error"].startswith("image too large to OCR")
    assert seen == []


def test_a_tiff_page_past_the_bound_is_named_and_the_rest_read(tmp_path, monkeypatch):
    seen = _tesseract(monkeypatch, reads_in={"RGB", "L"})
    monkeypatch.setattr(ax, "OCR_MAX_PIXELS", 1000)
    pages = [Image.new("RGB", (30, 30), "white"), Image.new("RGB", (40, 40), "white")]
    path = tmp_path / "scan.tif"
    pages[0].save(path, format="TIFF", save_all=True, append_images=pages[1:])

    out = ax._extract_image_ocr(str(path))

    assert len(seen) == 1
    assert out["status"] == "extracted"
    assert "page 2 too large to OCR" in out["error"]


@pytest.mark.parametrize("fmt", ["ICO"])
def test_a_format_tesseract_refuses_is_still_re_encoded(tmp_path, monkeypatch, fmt):
    seen = _tesseract(monkeypatch, reads_in={"RGB", "L"})
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buf, format=fmt)
    path = tmp_path / "icon.ico"
    path.write_bytes(buf.getvalue())

    out = ax._extract_image_ocr(str(path))

    assert out["status"] == "extracted"
    assert seen[0].format == "PNG"
