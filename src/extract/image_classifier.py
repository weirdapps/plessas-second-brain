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
MIN_DIMENSION_PX = 100
MIN_BYTES = 5_000
SIGNATURE_POSITION_CUTOFF = 0.85


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
        "SELECT frequency FROM sender_signature_index WHERE sender_email = ? AND sha256 = ?",
        (sender_email, sha),
    ).fetchone()
    return row is not None and row[0] >= threshold


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

    # Stage 1a — dimensions. An image PIL cannot decode (corrupt, unsupported
    # format such as HEIC without a plugin, or a decompression bomb) is recorded
    # as a decode failure so the caller stops re-scanning it on every run.
    bytes_size = img_path.stat().st_size
    try:
        width, height = Image.open(img_path).size
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
    conn.execute(
        """INSERT OR REPLACE INTO inline_images
           (sha256, width, height, bytes, classification, classification_method, classified_at, user_overridden)
           VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(
              (SELECT user_overridden FROM inline_images WHERE sha256 = ?), 0))""",
        (sha, w, h, b, classification.value, method, now, sha),
    )
    conn.commit()
    return classification
