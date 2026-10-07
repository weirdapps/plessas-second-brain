"""Return images Stage 1 filed wrongly to 'unclassified', so the image pass describes them.

Both caches return early for any image that is not 'unclassified'
(image_classifier.classify_stage1, image_vision.classify_with_vision), so a wrong Stage 1
verdict was permanent. Two kinds are known:

    frequency      a signature by sender frequency alone, filed before the occurrence floor
                   (MIN_SIGNATURE_OCCURRENCES) landed: a thin sender history made a one-off
                   image look like 5% of its traffic. Taken when no sender's signature-index
                   row qualifies under today's rule, which is the test Stage 1 applies when it
                   runs again, so each goes on to vision. Most were seen fewer than 3 times in
                   all; the rest recur across senders (a chart forwarded around a thread) or
                   are under 5% of a busy sender's images.
    decode_failed  noise because Pillow's default guard refused a large screenshot before
                   Stage 1 shared vision's pixel limit. Taken when its file opens now.

An image is taken only while a file of it is on disk: the image pass reads the file, and an
unclassified image with no file would be owed on every run and never done. A user's override
is kept. `process-images` then runs Stage 1 and vision for each one: one vision call per image.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from src.extract.image_classifier import (
    MIN_SIGNATURE_OCCURRENCES,
    SIGNATURE_FREQUENCY_THRESHOLD,
    admit_large_images,
)

# When MIN_SIGNATURE_OCCURRENCES landed (commit 3fafcef). A frequency verdict filed after it
# met the floor when it was made.
SIGNATURE_FLOOR_LANDED = "2026-08-30T12:18:20+00:00"

_FREQUENCY_SQL = """
    SELECT ii.sha256,
           (SELECT COUNT(*) FROM inline_image_occurrences o WHERE o.sha256 = ii.sha256)
    FROM inline_images ii
    WHERE ii.classification = 'signature' AND ii.classification_method = 'frequency'
      AND ii.user_overridden = 0 AND ii.classified_at < ?
      AND NOT EXISTS (
          SELECT 1 FROM sender_signature_index s
          WHERE s.sha256 = ii.sha256 AND s.frequency >= ? AND s.occurrence_count >= ?)
    ORDER BY ii.sha256
"""

_DECODE_FAILED_SQL = """
    SELECT sha256 FROM inline_images
    WHERE classification = 'noise' AND classification_method = 'decode_failed'
      AND user_overridden = 0
    ORDER BY sha256
"""

# What each kind was filed as. The reset checks it again, so an image the pass reached
# between the plan and the reset is left as the pass left it.
_FILED_AS = {"frequency": "signature", "decode_failed": "noise"}


@dataclass
class ResetPlan:
    """The images to reset, by kind, and what was left alone and why."""

    frequency: list[str] = field(default_factory=list)
    decode_failed: list[str] = field(default_factory=list)
    # Of `frequency`, seen fewer than MIN_SIGNATURE_OCCURRENCES times in all.
    seen_under_floor: int = 0
    frequency_no_file: int = 0
    decode_failed_no_file: int = 0
    # decode_failed whose file still does not open under the pipeline's limit.
    undecodable: int = 0
    # Image attachment rows of the images to reset, which the image pass takes again.
    attachment_rows: int = 0


def _file_on_disk(conn: sqlite3.Connection, sha256: str) -> Path | None:
    for (file_path,) in conn.execute(
        "SELECT file_path FROM attachments WHERE sha256 = ? AND file_path IS NOT NULL"
        " AND file_path NOT LIKE 'text:%' ORDER BY id",
        (sha256,),
    ):
        path = Path(file_path)
        if path.exists():
            return path
    return None


def _opens(path: Path) -> bool:
    """Whether Stage 1 can read the size of this file now. Nothing is decoded."""
    try:
        with admit_large_images(), Image.open(path):
            return True
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        return False


def find_misfiled(conn: sqlite3.Connection) -> ResetPlan:
    """The images to reset, read from the store and the disk. Writes nothing."""
    plan = ResetPlan()
    for sha256, seen in conn.execute(
        _FREQUENCY_SQL,
        (SIGNATURE_FLOOR_LANDED, SIGNATURE_FREQUENCY_THRESHOLD, MIN_SIGNATURE_OCCURRENCES),
    ).fetchall():
        if _file_on_disk(conn, sha256) is None:
            plan.frequency_no_file += 1
            continue
        plan.frequency.append(sha256)
        if seen < MIN_SIGNATURE_OCCURRENCES:
            plan.seen_under_floor += 1
    for (sha256,) in conn.execute(_DECODE_FAILED_SQL).fetchall():
        path = _file_on_disk(conn, sha256)
        if path is None:
            plan.decode_failed_no_file += 1
        elif _opens(path):
            plan.decode_failed.append(sha256)
        else:
            plan.undecodable += 1
    for sha256 in plan.frequency + plan.decode_failed:
        plan.attachment_rows += conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE sha256 = ? AND mime_type LIKE 'image/%'"
            " AND file_path IS NOT NULL AND file_path NOT LIKE 'text:%'",
            (sha256,),
        ).fetchone()[0]
    return plan


def apply_reset(conn: sqlite3.Connection, plan: ResetPlan) -> int:
    """Set the plan's images to 'unclassified', in one transaction. Returns how many changed.

    classification_method records why (reset_frequency, reset_decode_failed) until Stage 1
    files the image again. classified_at is the reset's time, so the health check ages an
    image still waiting for vision from its reset, not from when it was misfiled.
    """
    now = datetime.now(UTC).isoformat()
    changed = 0
    for kind, hashes in (("frequency", plan.frequency), ("decode_failed", plan.decode_failed)):
        for sha256 in hashes:
            changed += conn.execute(
                "UPDATE inline_images SET classification = 'unclassified',"
                " classification_method = ?, classified_at = ?"
                " WHERE sha256 = ? AND classification = ? AND classification_method = ?"
                " AND user_overridden = 0",
                (f"reset_{kind}", now, sha256, _FILED_AS[kind], kind),
            ).rowcount
    conn.commit()
    return changed
