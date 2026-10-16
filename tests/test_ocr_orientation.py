"""A scan on its side or upside down is turned upright before it is read.

Tesseract reads a page in the orientation it is given. An upside-down scan came back as letters
and figures of no language that still passed the noise filter, so its summary was made from
nonsense (owner's report, audit 2026-10-16). Each image and scan page now goes through
Tesseract's orientation and script detection first (OSD, `--psm 0`), and is turned when OSD is
confident it lies at 90, 180 or 270 degrees. An image OSD cannot judge (a logo, too few letters, a
host without osd.traineddata) is read as it is. Tesseract is faked here, as CI has none; the last
test reads a real page when this host has tesseract and its OSD data.
"""

import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont

from src.extract import attachment_extractors as ax


def _marked() -> Image.Image:
    """A 3x2 image whose top-left pixel alone is black, so any turn moves it."""
    img = Image.new("RGB", (3, 2), "white")
    img.putpixel((0, 0), (0, 0, 0))
    return img


def _osd(monkeypatch, answer=None, fail=None):
    calls = []

    def fake(image, config="", output_type=None):
        calls.append(config)
        if fail is not None:
            raise fail
        return answer

    monkeypatch.setattr("pytesseract.image_to_osd", fake)
    return calls


def _read(monkeypatch):
    seen = []

    def fake(image, lang):
        seen.append(image.copy())
        return "a reading of the page"

    monkeypatch.setattr("pytesseract.image_to_string", fake)
    return seen


def _black(img) -> tuple[int, int]:
    return next(
        (x, y)
        for y in range(img.height)
        for x in range(img.width)
        if img.getpixel((x, y)) == (0, 0, 0)
    )


def test_an_upside_down_page_is_turned_before_it_is_read(monkeypatch):
    calls = _osd(monkeypatch, {"rotate": 180, "orientation_conf": 12.5})
    seen = _read(monkeypatch)

    ax._read_page(_marked())

    assert calls and "--psm 0" in calls[0]
    assert _black(seen[0]) == (2, 1)  # top-left went to bottom-right


@pytest.mark.parametrize(("rotate", "size", "black"), [(90, (2, 3), (1, 0)), (270, (2, 3), (0, 2))])
def test_a_page_on_its_side_is_turned_upright(monkeypatch, rotate, size, black):
    _osd(monkeypatch, {"rotate": rotate, "orientation_conf": 9.0})
    seen = _read(monkeypatch)

    ax._read_page(_marked())

    assert seen[0].size == size and _black(seen[0]) == black


def test_a_doubtful_orientation_leaves_the_page_as_it_is(monkeypatch):
    _osd(monkeypatch, {"rotate": 180, "orientation_conf": ax.OSD_MIN_CONFIDENCE / 2})
    seen = _read(monkeypatch)

    ax._read_page(_marked())

    assert _black(seen[0]) == (0, 0)


@pytest.mark.parametrize(
    "error", [RuntimeError("Too few characters. Skipping this page"), OSError()]
)
def test_a_page_osd_cannot_judge_is_read_as_it_is(monkeypatch, error):
    _osd(monkeypatch, fail=error)
    seen = _read(monkeypatch)

    assert ax._read_page(_marked()) == "a reading of the page"
    assert _black(seen[0]) == (0, 0)


def test_an_upright_page_is_judged_once_and_read_as_it_is(monkeypatch):
    calls = _osd(monkeypatch, {"rotate": 0, "orientation_conf": 15.0})
    seen = _read(monkeypatch)

    ax._read_page(_marked())

    # A Latin reading is read again English first (OCR_LANGS_LATIN), not judged again.
    assert len(calls) == 1 and seen and all(_black(img) == (0, 0) for img in seen)


def _has_osd() -> bool:
    if not shutil.which("tesseract"):
        return False
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    return "osd" in langs.stdout.split() and "ell" in langs.stdout.split()


@pytest.mark.skipif(not _has_osd(), reason="needs tesseract with its osd and ell data")
def test_a_real_upside_down_scan_reads_as_text(tmp_path):
    font = ImageFont.load_default(size=40)
    page = Image.new("RGB", (1400, 700), "white")
    draw = ImageDraw.Draw(page)
    lines = [
        "The committee approved the new card for young customers,",
        "with an annual fee of fifteen euros, from the first of June.",
        "The pricing goes to the product owner by the end of April.",
    ] * 3
    for i, line in enumerate(lines):
        draw.text((40, 30 + i * 70), line, fill="black", font=font)
    path = tmp_path / "scan.png"
    page.rotate(180).save(path)

    out = ax._extract_image_ocr(str(path))

    assert out["status"] == "extracted"
    assert "committee approved" in out["text"] and "annual fee" in out["text"]
