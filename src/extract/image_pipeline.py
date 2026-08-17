"""
Image pipeline orchestrator — connects classifier stages to database.
"""

import logging
import sqlite3
import time
from pathlib import Path

from src.extract.image_classifier import (
    Classification,
    classify_stage1,
    refresh_signature_index,
    sha256_of_file,
)

logger = logging.getLogger(__name__)


class VisionFailed(Exception):
    """Stage 3 could not describe the image. The Stage-1 row is still recorded."""


def process_single_image(
    conn: sqlite3.Connection,
    attachment_id: int,
    img_path: Path,
    sender_email: str,
    message_id: str,
    position_in_body: float,
    run_vision: bool = True,
    refresh_index: bool = True,
) -> Classification | None:
    """
    Process a single image attachment through the classification pipeline.

    Args:
        conn: Database connection
        attachment_id: attachments.id
        img_path: Path to the image file
        sender_email: Email address of the sender
        message_id: Email message ID
        position_in_body: Float in [0.0, 1.0] representing image position
        run_vision: If True and Stage 1 returns UNCLASSIFIED, call Stage 3 vision
        refresh_index: If True (default), recompute the sender's signature index
            after this image. Bulk callers set False and refresh once at the end.

    Returns:
        Classification result, or None if image doesn't exist
    """
    # Check if file exists
    if not img_path.exists():
        logger.warning(f"Image file not found: {img_path}")
        return None

    # Compute SHA256
    sha256 = sha256_of_file(img_path)

    # Call Stage 1 classifier first (creates inline_images row)
    classification = classify_stage1(img_path, sender_email, position_in_body, conn)

    # Record occurrence in inline_image_occurrences (INSERT OR IGNORE, idempotent)
    # Must come AFTER classify_stage1 which creates the inline_images row
    conn.execute(
        """
        INSERT OR IGNORE INTO inline_image_occurrences
        (sha256, message_id, sender_email, position_in_body)
        VALUES (?, ?, ?, ?)
        """,
        (sha256, message_id, sender_email, position_in_body),
    )
    conn.commit()

    # If UNCLASSIFIED and run_vision=True, call Stage 3 vision
    if classification == Classification.UNCLASSIFIED and run_vision:
        try:
            # Deferred import to avoid loading vision module unless needed
            from src.extract.image_vision import classify_with_vision

            classification, _ = classify_with_vision(img_path, conn)
        except Exception as e:
            logger.error(f"Vision classification failed for {img_path}: {e}")
            # The Stage-1 row and its occurrence stay — they are real
            # observations and the signature index is built from them. But the
            # image did NOT get described, and reporting that as success is how
            # a fully dead vision stage kept exiting 0 with "Failed: 0" for
            # three weeks. Raise so the caller counts it as failed.
            raise VisionFailed(str(e)) from e

    # Refresh signature index for this sender. Bulk callers (run_backfill) pass
    # refresh_index=False and refresh once per sender at the end instead — the
    # per-image DELETE+recompute is O(images) and serializes parallel workers.
    if refresh_index:
        refresh_signature_index(conn, sender_email)

    return classification


def compute_position_in_body(email_content: str | None, attachment_filename: str) -> float:
    """
    Compute the normalized position of an image in the email body.

    Returns a float in [0.0, 1.0] representing where the image appears.
    Tries to find the filename (case-insensitive) in the content via cid: or src= patterns.
    If found, returns byte_offset / total_length.
    If not found or content is None, returns 0.5.

    Args:
        email_content: Email body content (HTML or plain text)
        attachment_filename: Name of the attachment file

    Returns:
        Float in [0.0, 1.0]
    """
    if email_content is None:
        return 0.5

    # Try to find the filename in the content (case-insensitive)
    content_lower = email_content.lower()
    filename_lower = attachment_filename.lower()

    # Look for cid: or src= patterns
    patterns = [
        f"cid:{filename_lower}",
        f'src="{filename_lower}"',
        f"src='{filename_lower}'",
        filename_lower,
    ]

    for pattern in patterns:
        idx = content_lower.find(pattern)
        if idx != -1:
            # Found it! Compute normalized position
            return idx / len(email_content)

    # Not found, default to middle
    return 0.5


def run_backfill(
    conn: sqlite3.Connection,
    since: str | None = None,
    limit: int | None = 1000,
    run_vision: bool = True,
    dry_run: bool = False,
    workers: int = 1,
    unprocessed_only: bool = False,
    deadline_s: float | None = None,
) -> dict:
    """
    Backfill image classification for existing attachments.

    Args:
        conn: Database connection
        since: Optional ISO date string (YYYY-MM-DD) to filter emails
        limit: Maximum number of images to process
        run_vision: If True, call vision classifier for UNCLASSIFIED images
        dry_run: If True, just count without classifying
        workers: Concurrent workers for the slow vision calls. >1 uses one
            dedicated connection per worker thread (WAL + busy_timeout serialize
            the writes); requires a file-backed DB. Defaults to 1 (sequential,
            uses `conn`) so callers and tests are unaffected.
        unprocessed_only: If True, skip attachments whose message already has
            recorded image occurrences (i.e. processed on a prior run), so the
            per-run limit targets new images and the backlog drains instead of
            re-scanning the same newest images every run.
        deadline_s: Optional wall-clock budget in seconds. Once spent, no further
            images are STARTED and the rest are returned as `deferred`; images
            already in flight run to completion. Lets a caller under an external
            timeout (systemd `TimeoutStartSec`) return cleanly with partial work
            instead of being SIGTERMed mid-flight. None = no time box.

    Returns:
        Stats dict: {scanned, classified, missing, failed, deferred}
    """
    stats = {
        "scanned": 0,
        "classified": 0,
        "missing": 0,
        "failed": 0,
        "deferred": 0,
    }

    # Build query
    query = """
        SELECT
            a.id,
            a.file_path,
            a.filename,
            a.message_id,
            e.sender_address,
            e.content,
            e.date_received
        FROM attachments a
        JOIN emails e ON a.email_id = e.id
        WHERE a.mime_type LIKE 'image/%'
          AND a.file_path IS NOT NULL
    """

    params: list[str | int] = []
    if since:
        query += " AND e.date_received >= ?"
        params.append(since)

    # Skip attachments whose message already has recorded image occurrences —
    # i.e. images processed on a prior run. Lets the limited per-run budget
    # target genuinely new images instead of re-scanning the same newest ones
    # (dedup makes those cache-hits), and guarantees the backlog drains.
    if unprocessed_only:
        query += " AND a.message_id NOT IN (SELECT message_id FROM inline_image_occurrences)"

    query += " ORDER BY e.date_received DESC"

    if limit:
        query += " LIMIT ?"
        params.append(limit)

    # Materialize the whole result set up front (fetchall) so the read statement
    # is FINALIZED before the per-image commits below. process_single_image()
    # commits on `conn` for every image; iterating a *live* cursor while
    # committing on the same connection is a read->write lock upgrade that SQLite
    # rejects with an immediate SQLITE_BUSY ("database is locked") that
    # busy_timeout cannot retry. (A second read connection is worse: its held
    # read lock makes SQLite suppress conn's busy handler for same-process
    # deadlock avoidance, so every write fails instantly whenever an external
    # writer is active.) With no open cursor, busy_timeout properly waits out the
    # hourly launchd / conversation-capture-hook writers.
    rows = conn.execute(query, params).fetchall()

    # Count scanned + drop missing files up front; collect the real work list.
    todo = []
    for row in rows:
        file_path = row[1]
        stats["scanned"] += 1
        if not Path(file_path).exists():
            logger.warning(f"Image file not found: {file_path}")
            stats["missing"] += 1
            continue
        todo.append(row)

    if dry_run:
        return stats

    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    def _out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _process(row, work_conn) -> bool:
        attachment_id, file_path, filename, message_id, sender_address, content, _ = row
        position = compute_position_in_body(content, filename)
        try:
            result = process_single_image(
                conn=work_conn,
                attachment_id=attachment_id,
                img_path=Path(file_path),
                sender_email=sender_address,
                message_id=message_id,
                position_in_body=position,
                run_vision=run_vision,
                refresh_index=False,
            )
            return result is not None
        except Exception as e:
            logger.error(f"Failed to process attachment {attachment_id} ({filename}): {e}")
            return False

    db_file = next(
        (f for _id, name, f in conn.execute("PRAGMA database_list") if name == "main"),
        None,
    )

    if workers and workers > 1 and db_file:
        # Parallelize the (slow) vision calls. Each task opens AND closes its own
        # connection in its own worker thread — sqlite3 forbids using/closing a
        # connection from a different thread than created it. WAL + busy_timeout
        # serialize the per-image writes across the workers.
        from concurrent.futures import ThreadPoolExecutor

        from src.store.schema import get_connection

        def worker(row) -> str:
            # Checked per task rather than per submission: queued tasks drain
            # instantly once the budget is spent, so the pool closes promptly.
            if _out_of_time():
                return "deferred"
            c = get_connection(db_file)
            try:
                return "classified" if _process(row, c) else "failed"
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for outcome in ex.map(worker, todo):
                stats[outcome] += 1
    else:
        for row in todo:
            if _out_of_time():
                stats["deferred"] += 1
                continue
            ok = _process(row, conn)
            stats["classified" if ok else "failed"] += 1

    # Per-image refresh was skipped above; recompute each touched sender's
    # signature index once now. Same final state as per-image refresh (the last
    # recompute wins) but without serializing the parallel workers on every
    # write. Runs on `conn` after the worker pool has closed.
    for sender in {row[4] for row in todo if row[4]}:
        refresh_signature_index(conn, sender)

    return stats
