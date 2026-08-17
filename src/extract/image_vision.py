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

# Thinking tokens are drawn from this same budget before the answer is written:
# a measured call spent 42 thinking, then 70 on a one-line description. At the
# original 150 a longer deliberation truncates the answer away entirely, leaving
# a response with no text block at all.
MAX_TOKENS = 400

# Anthropic measures the BASE64 payload (not the raw file) against a 5 MiB
# (5,242,880-byte) per-image cap — base64 inflates bytes by ~4/3, so a 4.5 MB
# PNG becomes a ~6 MB payload and 400s. Cap the base64 length with a margin.
VISION_IMAGE_MAX_B64 = 5_000_000

# Independent of the byte cap: the API also rejects any image with a side over
# 8000 px. A full-page report screenshot is tall and flat-coloured, so it
# compresses to a tiny PNG, clears the byte cap untouched, and still 400s.
VISION_IMAGE_MAX_DIMENSION = 8000

# Deliberate, bounded raise of Pillow's decompression-bomb guard, whose ~89 M px
# default hard-refuses the report screenshots this pipeline exists to read
# (measured on prod: 10610x32768 = 348 M px and 16237x32768 = 532 M px).
#
# Pillow WARNS above this value and only RAISES above 2x it, so the effective
# admission ceiling is 550 M px. Decoding measured ~8.1 bytes/px, putting the
# largest admitted image at ~4.5 GB peak — survivable on the 7 GB host because
# `process-images` runs sequentially (--workers defaults to 1). Beyond that
# Pillow raises DecompressionBombError, which the caller records as a visible
# failure rather than an image that quietly never gets described.
VISION_IMAGE_BOMB_LIMIT = 275_000_000
Image.MAX_IMAGE_PIXELS = VISION_IMAGE_BOMB_LIMIT

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

    Images within BOTH limits are sent as-is. Oversized ones are re-encoded as
    JPEG and progressively shrunk until they fit, so neither a 6 MB PNG
    ("image exceeds 5 MB maximum") nor a tall screenshot ("image dimensions
    exceed max allowed size: 8000 pixels") 400s the request.

    The two caps are independent and a payload can breach either alone: a
    16237x32768 report screenshot of flat UI colour is only ~1 MB as PNG, so
    checking bytes first and returning early let exactly the images worth
    describing through to a guaranteed 400.
    """
    raw = img_path.read_bytes()

    with Image.open(io.BytesIO(raw)) as probe:
        oversized_px = max(probe.size) > VISION_IMAGE_MAX_DIMENSION

    b64 = base64.b64encode(raw).decode()
    if not oversized_px and len(b64) <= VISION_IMAGE_MAX_B64:
        return b64, _guess_media_type(img_path)

    im = Image.open(io.BytesIO(raw))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")

    # thumbnail() preserves aspect ratio and is a no-op when already within
    # bounds. A squashed report is an unreadable report, and the description is
    # the entire product here.
    if oversized_px:
        im.thumbnail((VISION_IMAGE_MAX_DIMENSION, VISION_IMAGE_MAX_DIMENSION))

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


def _response_text(resp) -> str:
    """The model's answer, whatever precedes it in the content list.

    Indexing content[0] assumed the answer came first. With extended thinking
    the model emits a ThinkingBlock there and the answer lands at [1], so every
    call raised "'ThinkingBlock' object has no attribute 'text'" — swallowed by
    the caller and counted as success.
    """
    for block in resp.content:
        text = getattr(block, "text", None)
        if text:
            return text
    raise ValueError(
        "vision response carried no text block "
        f"(stop_reason={getattr(resp, 'stop_reason', 'unknown')!r}, "
        f"blocks={[type(b).__name__ for b in resp.content]})"
    )


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

    from src.extract.claude_extract import _get_client_and_model, call_with_policy

    # _do_call re-fetches the client on every attempt so a successful reauth
    # (which calls reset_client_cache) is picked up by the retry rather than
    # silently reusing the stale credential.
    def _do_call():
        cur_client, cur_model = _get_client_and_model()
        return cur_client.messages.create(
            model=cur_model,
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

    resp = call_with_policy(_do_call, max_call_seconds=120.0)
    raw = _response_text(resp)
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
