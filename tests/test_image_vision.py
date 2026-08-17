"""Tests for the vision Stage-3 helpers (image_vision)."""

import base64
import io
import os

import pytest
from PIL import Image

from src.extract.image_vision import (
    MAX_TOKENS,
    VISION_DECODE_BYTES_PER_PIXEL,
    VISION_IMAGE_BOMB_LIMIT,
    VISION_IMAGE_MAX_B64,
    VISION_IMAGE_MAX_DIMENSION,
    VisionDecodeTooLarge,
    _decode_fits_in_memory,
    _encode_image_for_vision,
    _response_text,
)

# Measured dimensions of the two largest report screenshots found undescribed in
# production. Both are full-page UI captures pasted into an email body, so both
# are pinned to Outlook's 32768 px ceiling on the long edge.
LARGEST_OBSERVED_PX = 16237 * 32768
SECOND_LARGEST_OBSERVED_PX = 10610 * 32768

# Pillow warns above MAX_IMAGE_PIXELS and only raises above 2x it.
PILLOW_HARD_REFUSAL_MULTIPLIER = 2


def test_small_image_passes_through_unchanged(tmp_path):
    """An image whose base64 is under the cap is sent as-is (original bytes)."""
    p = tmp_path / "small.png"
    Image.new("RGB", (60, 60), (10, 20, 30)).save(p, format="PNG")

    b64, media_type = _encode_image_for_vision(p)

    assert media_type == "image/png"
    assert base64.b64decode(b64) == p.read_bytes()
    assert len(b64) <= VISION_IMAGE_MAX_B64


def test_oversized_image_is_downscaled_under_limit(tmp_path):
    """A PNG whose base64 exceeds the cap is re-encoded as JPEG under the cap."""
    p = tmp_path / "big.png"
    # Random pixels defeat PNG compression -> a genuinely oversized payload.
    noise = os.urandom(1600 * 1600 * 3)
    Image.frombytes("RGB", (1600, 1600), noise).save(p, format="PNG")

    # Precondition: as-is, this image would 400 the vision API.
    assert len(base64.b64encode(p.read_bytes())) > VISION_IMAGE_MAX_B64

    b64, media_type = _encode_image_for_vision(p)

    assert media_type == "image/jpeg"
    assert len(b64) <= VISION_IMAGE_MAX_B64


# The byte cap and the pixel cap are INDEPENDENT API limits, and the payload
# guard above only enforces the byte one. A full-page report screenshot pasted
# into an email body is tall and flat-coloured, so it compresses to a tiny PNG
# and sails past the byte check untouched — then the API rejects it with
# "At least one of the image dimensions exceed max allowed size: 8000 pixels".
# Two such screenshots sat undescribed in production for weeks.


def _decoded_size(b64: str) -> tuple[int, int]:
    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        return im.size


def test_tall_screenshot_is_downscaled_under_pixel_limit(tmp_path):
    """Exceeds the pixel cap while well under the byte cap — the prod failure."""
    p = tmp_path / "report.png"
    Image.new("RGB", (400, VISION_IMAGE_MAX_DIMENSION + 2000), (250, 250, 250)).save(
        p, format="PNG"
    )

    # Precondition: the byte guard alone would let this through untouched.
    assert len(base64.b64encode(p.read_bytes())) <= VISION_IMAGE_MAX_B64

    b64, _ = _encode_image_for_vision(p)

    assert max(_decoded_size(b64)) <= VISION_IMAGE_MAX_DIMENSION


def test_downscaling_preserves_aspect_ratio(tmp_path):
    """A squashed report is an unreadable report — the description is the product."""
    p = tmp_path / "wide.png"
    Image.new("RGB", (VISION_IMAGE_MAX_DIMENSION + 4000, 3000), (250, 250, 250)).save(
        p, format="PNG"
    )

    w, h = _decoded_size(_encode_image_for_vision(p)[0])

    assert abs((w / h) - (12000 / 3000)) < 0.05


def test_image_at_the_pixel_limit_is_left_alone(tmp_path):
    """Only images actually over the line get re-encoded; the cap is inclusive."""
    p = tmp_path / "exact.png"
    Image.new("RGB", (VISION_IMAGE_MAX_DIMENSION, 10), (250, 250, 250)).save(p, format="PNG")

    b64, media_type = _encode_image_for_vision(p)

    assert media_type == "image/png"
    assert base64.b64decode(b64) == p.read_bytes()


# Calibration of the decompression-bomb ceiling. Asserted on arithmetic rather
# than real 500 M px fixtures: allocating those in CI would cost ~4 GB per test.


def test_bomb_limit_admits_the_real_report_screenshots():
    """Pillow's ~89 M px default refuses both; tightening past this reinstates that."""
    admitted = PILLOW_HARD_REFUSAL_MULTIPLIER * VISION_IMAGE_BOMB_LIMIT

    assert LARGEST_OBSERVED_PX < admitted
    assert SECOND_LARGEST_OBSERVED_PX < admitted


def test_bomb_limit_keeps_the_largest_admitted_image_within_host_memory():
    """7 GB host, sequential workers. Measured decode cost ~8.1 bytes/px."""
    peak_gb = PILLOW_HARD_REFUSAL_MULTIPLIER * VISION_IMAGE_BOMB_LIMIT * 8.1 / 1e9

    assert peak_gb < 5.0, f"largest admitted image would peak at {peak_gb:.1f} GB"


# Reading the answer out of the response. `resp.content[0].text` held only while
# the first block was the answer; with extended thinking the model emits a
# ThinkingBlock first, so every single call died on
# "'ThinkingBlock' object has no attribute 'text'" — and still counted as
# success, because image_pipeline swallows the exception. A verified live
# response carried ThinkingBlock at [0] and the description at [1].


class _Block:
    def __init__(self, text=None):
        if text is not None:
            self.text = text


class _Resp:
    def __init__(self, *blocks, stop_reason="end_turn"):
        self.content = list(blocks)
        self.stop_reason = stop_reason


def test_reads_the_answer_past_a_thinking_block():
    resp = _Resp(_Block(), _Block("CONTENT: a chart of Q3 revenue"))

    assert _response_text(resp) == "CONTENT: a chart of Q3 revenue"


def test_reads_the_answer_when_it_is_the_only_block():
    """Thinking is not guaranteed; the no-thinking shape must keep working."""
    resp = _Resp(_Block("DECORATION: a logo"))

    assert _response_text(resp) == "DECORATION: a logo"


def test_raises_a_diagnosable_error_when_no_text_block_came_back():
    """Truncation mid-thinking yields thinking only. An AttributeError deep in a
    swallowed handler is what hid this for weeks; the stop_reason is the clue."""
    resp = _Resp(_Block(), stop_reason="max_tokens")

    with pytest.raises(ValueError, match="max_tokens"):
        _response_text(resp)


def test_token_budget_leaves_room_for_an_answer_after_thinking():
    """A live call spent 42 of its tokens thinking before writing 70 of answer.
    At the original 150 ceiling a longer deliberation truncates the answer away."""
    assert MAX_TOKENS >= 400


# Decoding a 532 M px screenshot costs ~4.3 GB. The nightly image job runs alone
# and can afford that on the 7 GB host; the hourly sync cannot — it already
# peaks at 3-4.1 GB with up to 1 GB of swap, because Step 8 classifies images in
# the same process as an embeddings rebuild. Raising Pillow's bomb ceiling to
# reach these images therefore moved a real OOM into the hourly path. Gating on
# memory actually free at decode time keeps one ceiling for both callers: the
# same image is deferred under load and processed when the box is quiet.

GB = 1_000_000_000


def test_permits_a_decode_that_fits_the_free_memory():
    """Quiet box: the largest observed screenshot must still get described."""
    assert _decode_fits_in_memory(LARGEST_OBSERVED_PX, available=6 * GB)


def test_refuses_a_decode_that_would_exhaust_the_host():
    """Mid-sync, most of the box is already committed."""
    assert not _decode_fits_in_memory(LARGEST_OBSERVED_PX, available=2 * GB)


def test_small_images_are_unaffected_even_under_pressure():
    """The ordinary case must never be gated, or the pipeline stalls under load."""
    assert _decode_fits_in_memory(1920 * 1080, available=2 * GB)


def test_keeps_a_reserve_rather_than_spending_all_free_memory():
    """Sizing to exactly MemAvailable leaves nothing for everything else running."""
    just_under_available = int(2 * GB / VISION_DECODE_BYTES_PER_PIXEL)

    assert not _decode_fits_in_memory(just_under_available, available=2 * GB)


def test_permits_the_decode_when_free_memory_cannot_be_measured():
    """No /proc/meminfo (macOS dev, odd containers) must not block the pipeline."""
    assert _decode_fits_in_memory(LARGEST_OBSERVED_PX, available=None)


def test_encoder_refuses_an_image_it_cannot_safely_decode(tmp_path, monkeypatch):
    """End to end: the gate has to fire before Pillow allocates, not after."""
    p = tmp_path / "huge.png"
    Image.new("RGB", (12000, 400), (250, 250, 250)).save(p, format="PNG")
    monkeypatch.setattr("src.extract.image_vision._available_memory_bytes", lambda: 1024)

    with pytest.raises(VisionDecodeTooLarge):
        _encode_image_for_vision(p)
