"""
Inline image classifier — Stage 1 (deterministic).

Cascade:
  1a. dimensions < 100x100 → NOISE
  1b. bytes < 5KB → NOISE
  1c. SHA256 frequency dedup against sender's history → SIGNATURE
  1d. position > 0.85 in body → provisional SIGNATURE (vision can override)
  Otherwise → UNCLASSIFIED (caller proceeds to Stage 3 vision LLM)

Stage 3 lives in image_vision.py to keep the LLM dependency optional.
"""

import hashlib
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Register HEIF/HEIC support so PIL can decode iPhone photos. If the optional
# dependency is missing, HEIC images fall through to the decode-failure marker.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover
    pass


class Classification(StrEnum):
    NOISE = "noise"
    SIGNATURE = "signature"
    CONTENT = "content"
    UNCLASSIFIED = "unclassified"


SIGNATURE_FREQUENCY_THRESHOLD = 0.05
# Frequency alone is a ratio, and a thin sender history makes the denominator
# tiny: a sender with only 19 inline images turns a single one-off image into
# 1/19 = 5.3%, over the threshold, and it is filed as a signature forever
# (both caches return early for anything that isn't 'unclassified', so no
# reprocess path revisits it). Measured on the corpus: of 675 frequency-flagged
# images, 271 had been seen EXACTLY ONCE. An absolute floor alongside the ratio
# is what separates a recurring logo from a one-off photo.
MIN_SIGNATURE_OCCURRENCES = 3
MIN_DIMENSION_PX = 100
MIN_BYTES = 5_000
SIGNATURE_POSITION_CUTOFF = 0.85

# Deliberate, bounded raise of Pillow's decompression-bomb guard for this
# pipeline's own opens. Its ~89 M px default hard-refuses the report screenshots
# the pipeline exists to read (measured on prod: 10610x32768 = 348 M px and
# 16237x32768 = 532 M px), and under it Stage 1 filed five such screenshots as
# noise/decode_failed, so vision never saw them.
#
# Pillow WARNS above this value and only RAISES above 2x it, so the effective
# admission ceiling is 550 M px. Decoding measured ~8.1 bytes/px, putting the
# largest admitted image at ~4.5 GB peak; image_vision gates every decode on the
# memory free at the time.
IMAGE_PIXEL_LIMIT = 275_000_000

# The guard is one value for the whole process, and every other Pillow caller
# (Phase 1 OCR above all, which reaches this module through file_hashes) keeps
# Pillow's default. So the limit is raised only around the pipeline's own opens,
# never at import, and one lock keeps two threads from restoring each other's
# value: interleaved, the second would put the first one's raised value back.
_PIXEL_LIMIT_LOCK = threading.Lock()


@contextmanager
def admit_large_images() -> Iterator[None]:
    """Pillow's bomb guard at IMAGE_PIXEL_LIMIT for the Image.open calls inside, restored after.

    Pillow checks the size when it opens a file, from the header, so hold this
    around the open only and decode outside it. A lower guard is raised; a higher
    one, or none, is kept.
    """
    with _PIXEL_LIMIT_LOCK:
        previous = Image.MAX_IMAGE_PIXELS
        if previous is not None and previous < IMAGE_PIXEL_LIMIT:
            Image.MAX_IMAGE_PIXELS = IMAGE_PIXEL_LIMIT
        try:
            yield
        finally:
            Image.MAX_IMAGE_PIXELS = previous


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_known_signature(
    conn: sqlite3.Connection,
    sender_email: str,
    sha: str,
    threshold: float = SIGNATURE_FREQUENCY_THRESHOLD,
) -> bool:
    row = conn.execute(
        "SELECT frequency, occurrence_count FROM sender_signature_index "
        "WHERE sender_email = ? AND sha256 = ?",
        (sender_email, sha),
    ).fetchone()
    return row is not None and row[0] >= threshold and row[1] >= MIN_SIGNATURE_OCCURRENCES


def refresh_signature_index(conn: sqlite3.Connection, sender: str) -> None:
    """Recompute the materialized frequency for all images from this sender."""
    sender_total = conn.execute(
        "SELECT COUNT(*) FROM inline_image_occurrences WHERE sender_email = ?",
        (sender,),
    ).fetchone()[0]
    if sender_total == 0:
        return
    rows = conn.execute(
        """SELECT sha256, COUNT(*)
           FROM inline_image_occurrences
           WHERE sender_email = ?
           GROUP BY sha256""",
        (sender,),
    ).fetchall()
    conn.execute("DELETE FROM sender_signature_index WHERE sender_email = ?", (sender,))
    for sha, count in rows:
        conn.execute(
            "INSERT INTO sender_signature_index VALUES (?, ?, ?, ?, ?)",
            (sender, sha, count, sender_total, count / sender_total),
        )
    conn.commit()


def classify_stage1(
    img_path: Path,
    sender: str,
    position: float,
    conn: sqlite3.Connection,
) -> Classification:
    """
    Run Stage 1 of the cascade. Does NOT call any LLM.
    Returns UNCLASSIFIED if the image needs vision-LLM stage 3.
    """
    sha = sha256_of_file(img_path)

    # Cache hit — fast path
    cached = conn.execute(
        "SELECT classification, user_overridden FROM inline_images WHERE sha256 = ?",
        (sha,),
    ).fetchone()
    if cached:
        c, overridden = cached
        if overridden or c != Classification.UNCLASSIFIED.value:
            return Classification(c)

    # Stage 1a — dimensions, read from the header; nothing is decoded. An image PIL
    # cannot identify (corrupt, unsupported format such as HEIC without a plugin, or
    # over the pipeline's 550 M px ceiling) is recorded as a decode failure so the
    # caller stops re-scanning it on every run.
    bytes_size = img_path.stat().st_size
    try:
        with admit_large_images(), Image.open(img_path) as im:
            width, height = im.size
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        logger.warning("Undecodable image %s: %s", img_path, e)
        return _store(conn, sha, Classification.NOISE, "decode_failed", 0, 0, bytes_size)
    if width < MIN_DIMENSION_PX or height < MIN_DIMENSION_PX:
        return _store(conn, sha, Classification.NOISE, "dimensions", width, height, bytes_size)

    # Stage 1b — byte size
    if bytes_size < MIN_BYTES:
        return _store(conn, sha, Classification.NOISE, "byte_size", width, height, bytes_size)

    # Stage 1c — sender-scoped frequency dedup
    if is_known_signature(conn, sender, sha):
        return _store(conn, sha, Classification.SIGNATURE, "frequency", width, height, bytes_size)

    # Stage 1d — position (provisional, vision can override)
    # We don't decide here — let stage 3 see the image. Position is recorded on the occurrence row.
    return _store(
        conn,
        sha,
        Classification.UNCLASSIFIED,
        "stage1_passed",
        width,
        height,
        bytes_size,
    )


def _store(
    conn: sqlite3.Connection,
    sha: str,
    classification: Classification,
    method: str,
    w: int,
    h: int,
    b: int,
) -> Classification:
    now = datetime.now(UTC).isoformat()
    # An upsert, not INSERT OR REPLACE: REPLACE deletes the row first, which reset
    # vision_attempts and, with foreign keys on, cascaded away the image's occurrences.
    conn.execute(
        """INSERT INTO inline_images
           (sha256, width, height, bytes, classification, classification_method,
            classified_at, user_overridden)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0)
           ON CONFLICT(sha256) DO UPDATE SET
             width = excluded.width,
             height = excluded.height,
             bytes = excluded.bytes,
             classification = excluded.classification,
             classification_method = excluded.classification_method,
             classified_at = excluded.classified_at""",
        (sha, w, h, b, classification.value, method, now),
    )
    conn.commit()
    return classification
