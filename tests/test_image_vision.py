"""Tests for the vision Stage-3 helpers (image_vision)."""

import base64
import os

from PIL import Image

from src.extract.image_vision import VISION_IMAGE_MAX_B64, _encode_image_for_vision


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
