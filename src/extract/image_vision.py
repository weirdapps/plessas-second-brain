"""
Inline image classifier — Stage 3 (vision LLM with cache).
"""

import base64
import io
import logging
import sqlite3
from pathlib import Path

from PIL import Image

from src.extract.image_classifier import Classification, sha256_of_file

logger = logging.getLogger(__name__)

MAX_TOKENS = 150

# Anthropic measures the BASE64 payload (not the raw file) against a 5 MiB
# (5,242,880-byte) per-image cap — base64 inflates bytes by ~4/3, so a 4.5 MB
# PNG becomes a ~6 MB payload and 400s. Cap the base64 length with a margin.
VISION_IMAGE_MAX_B64 = 5_000_000

VISION_PROMPT = """\
Classify this email-embedded image. Reply with EXACTLY one line:

CONTENT: <one-sentence description> — if it shows meaningful information \
(charts, dashboards, screenshots, diagrams, tables, photos relevant to the message)

DECORATION: <one-sentence description> — if it is purely visual/branding \
(logos, signatures, banners, social media icons, marketing graphics)

Be strict: a screenshot of a UI bug is CONTENT; a company logo is DECORATION.
"""


def _guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(suffix, "image/png")


def _encode_image_for_vision(img_path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for the vision API.

    Images at or under the size limit are sent as-is. Oversized images are
    re-encoded as JPEG and progressively shrunk until they fit, so a 6 MB PNG
    doesn't 400 the request ("image exceeds 5 MB maximum").
    """
    raw = img_path.read_bytes()
    b64 = base64.b64encode(raw).decode()
    if len(b64) <= VISION_IMAGE_MAX_B64:
        return b64, _guess_media_type(img_path)

    im = Image.open(io.BytesIO(raw))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")

    for _ in range(12):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= VISION_IMAGE_MAX_B64:
            break
        w, h = im.size
        im = im.resize((max(1, int(w * 0.75)), max(1, int(h * 0.75))))

    logger.info(
        "Downscaled oversized image %s (raw %d B -> base64 %d B) for vision",
        img_path.name,
        len(raw),
        len(b64),
    )
    return b64, "image/jpeg"


def parse_vision_response(text: str) -> tuple[Classification, str]:
    """
    Parse the LLM's response. Defensive: any non-conforming output → SIGNATURE
    so we don't pollute the extractor with hallucinated descriptions.
    """
    text = text.strip()
    if text.startswith("CONTENT:"):
        return Classification.CONTENT, text[len("CONTENT:") :].strip(" -—")
    if text.startswith("DECORATION:"):
        return Classification.SIGNATURE, text[len("DECORATION:") :].strip(" -—")
    return Classification.SIGNATURE, ""


def classify_with_vision(img_path: Path, conn: sqlite3.Connection) -> tuple[Classification, str]:
    """
    Cache-checked vision classification.
    Returns (label, description). Description is empty for NOISE/SIGNATURE.
    """
    sha = sha256_of_file(img_path)

    cached = conn.execute(
        "SELECT classification, vision_description FROM inline_images WHERE sha256 = ? AND classification != ?",
        (sha, Classification.UNCLASSIFIED.value),
    ).fetchone()
    if cached:
        return Classification(cached[0]), cached[1] or ""

    img_b64, media_type = _encode_image_for_vision(img_path)

    from src.extract.claude_extract import _get_client_and_model

    client, model = _get_client_and_model()

    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": VISION_PROMPT},
                ],
            }
        ],
    )
    raw = resp.content[0].text
    label, desc = parse_vision_response(raw)

    # Persist. visioned_at records *when* vision ran (classified_at is only the
    # Stage-1 insert time and never updated here), giving an honest freshness signal.
    from datetime import UTC, datetime

    conn.execute(
        """UPDATE inline_images
           SET classification = ?, classification_method = 'vision_llm',
               vision_description = ?, visioned_at = ?
           WHERE sha256 = ?""",
        (label.value, desc, datetime.now(UTC).isoformat(), sha),
    )
    conn.commit()
    return label, desc
