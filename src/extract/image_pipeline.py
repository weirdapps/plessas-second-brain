"""
Image pipeline orchestrator — connects classifier stages to database.
"""

import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from src.extract.image_classifier import (
    Classification,
    classify_stage1,
    refresh_signature_index,
    sha256_of_file,
)
from src.redact import redact_secrets
from src.store.email_html import markup_or_text
from src.store.file_sweep import VISION_ATTEMPTS_LIMIT

logger = logging.getLogger(__name__)

# An image the pipeline is finished with: described, filed as a signature or noise by Stage 1,
# or given up on after VISION_ATTEMPTS_LIMIT failed descriptions. The sweep applies the same
# rule (src/store/file_sweep.py) before it deletes an image file.
IMAGE_DONE_SQL = (
    "(ii.vision_description IS NOT NULL OR ii.classification IN ('signature', 'noise')"
    f" OR ii.vision_attempts >= {VISION_ATTEMPTS_LIMIT})"
)

# An image attachment the pipeline still owes work (alias a): its occurrence in this message is
# not recorded, which image search and the signature counts read, or the image is not done. A
# known image in a new message is therefore taken once, as a cache hit with no vision call.
IMAGE_OWED_SQL = (
    "(NOT EXISTS (SELECT 1 FROM inline_image_occurrences o"
    " WHERE o.sha256 = a.sha256 AND o.message_id = a.message_id)"
    " OR NOT EXISTS (SELECT 1 FROM inline_images ii"
    f" WHERE ii.sha256 = a.sha256 AND {IMAGE_DONE_SQL}))"
)


# An attachment_content row (alias ac) that an image's vision text may fill: it holds no text
# read from the file (OCR found too little, or failed), or it is vision's own. A row with text
# from the file is never overwritten, and none is inserted: Phase 1 stays the first writer, so
# an image Phase 1 has not reached yet is filled once it has.
VISION_FILLABLE_SQL = (
    "(COALESCE(trim(ac.extracted_text, char(32, 9, 10, 11, 12, 13)), '') = ''"
    " OR ac.extraction_method = 'vision')"
)

# A content image (alias ii) that still owes its transcription: described, not transcribed,
# under VISION_ATTEMPTS_LIMIT failed attempts, and with an attachment row its text may fill
# (an image whose rows all hold OCR text is searchable by it already). run_transcription takes
# these, and it reads the file, so the sweep must keep the file of such an image.
TRANSCRIPTION_OWED_SQL = (
    "(ii.classification = 'content' AND ii.vision_description IS NOT NULL"
    " AND ii.transcribed_at IS NULL"
    f" AND ii.transcription_attempts < {VISION_ATTEMPTS_LIMIT}"
    " AND EXISTS (SELECT 1 FROM attachments ta"
    " JOIN attachment_content ac ON ac.attachment_id = ta.id"
    f" WHERE ta.sha256 = ii.sha256 AND {VISION_FILLABLE_SQL}))"
)


class VisionFailed(Exception):
    """Stage 3 could not describe the image. The Stage-1 row is still recorded."""


def file_on_disk(conn: sqlite3.Connection, sha256: str) -> Path | None:
    """The first file of these bytes still on disk, by attachment id, or None."""
    for (file_path,) in conn.execute(
        "SELECT file_path FROM attachments WHERE sha256 = ? AND file_path IS NOT NULL"
        " AND file_path NOT LIKE 'text:%' ORDER BY id",
        (sha256,),
    ):
        path = Path(file_path)
        if path.exists():
            return path
    return None


def project_vision_text(conn: sqlite3.Connection) -> int:
    """Write each content image's vision text into its attachments' text-free rows.

    That is how a content image reaches attachment search (attachment_content_fts) and the
    embeddings index (build_index), with no namespace of its own. Each row VISION_FILLABLE_SQL
    allows becomes extraction_method 'vision', extracted and summarised:

        summary         the description: what build_index embeds. It does not change when the
                        transcription arrives, so the vector made from it stays right.
        extracted_text  the description, then the transcription when there is one.

    Credentials are redacted on the way, as Phase 1 redacts what it reads. Returns the rows
    written; a row that already holds exactly this is left alone.
    """
    rows = conn.execute(
        f"""SELECT ac.id, ii.vision_description, ii.vision_transcription,
                   ac.extracted_text, ac.summary, ac.extraction_method
            FROM inline_images ii
            JOIN attachments a ON a.sha256 = ii.sha256
            JOIN attachment_content ac ON ac.attachment_id = a.id
            WHERE ii.classification = 'content' AND ii.vision_description IS NOT NULL
              AND {VISION_FILLABLE_SQL}"""
    ).fetchall()
    # attachment_content keeps Phase 1's timestamp format.
    now = datetime.now().isoformat()
    written = 0
    for ac_id, description, transcription, stored_text, stored_summary, method in rows:
        summary = redact_secrets(description)
        text = "\n\n".join(part for part in (summary, transcription) if part)
        if method == "vision" and stored_text == text and stored_summary == summary:
            continue
        conn.execute(
            """UPDATE attachment_content
               SET extracted_text = ?, extraction_method = 'vision',
                   extraction_status = 'extracted', extraction_error = NULL, extracted_at = ?,
                   summary = ?, llm_status = 'extracted', llm_error = NULL,
                   llm_extracted_at = ?
               WHERE id = ?""",
            (text, now, summary, now, ac_id),
        )
        written += 1
    conn.commit()
    return written


def _service_failed(e: Exception) -> bool:
    """The service or the host failed, not the image: offer it again, count no attempt.

    The same split as Phase 2 (attachment_pipeline._extract_one_attachment): an outage, a
    quota or an expired login must not use up the attempts of every image it meets. A request
    that ran out of time will again, so it counts.
    """
    from src.extract.image_vision import VisionDecodeTooLarge
    from src.extract.policy_bridge import classify_exception, is_item_timeout, is_transient
    from src.llm_policy import Outcome

    if isinstance(e, VisionDecodeTooLarge):
        return True
    if classify_exception(e, None) is Outcome.AUTH_REAUTH_REQUIRED:
        return True
    return is_transient(e) and not is_item_timeout(e)


def run_transcription(
    conn: sqlite3.Connection,
    limit: int | None = None,
    workers: int = 1,
    dry_run: bool = False,
    deadline_s: float | None = None,
) -> dict:
    """Transcribe the content images whose attachments hold no text from their file.

    Taken: TRANSCRIPTION_OWED_SQL, newest description first. An image with no file on disk is
    counted `missing` and does not use up `limit`.

    One call per image (image_vision.transcribe_image), then project_vision_text writes the
    text into the rows. A failure of the image counts an attempt; a failure of the service or
    of the host (_service_failed) is counted `failed` but offered again next run. `workers`
    and `deadline_s` work as in run_backfill.

    Returns {candidates, missing, transcribed, empty, failed, deferred, projected}:
    `candidates` have a file on disk, and `empty` is an image that shows no text.
    """
    stats = dict.fromkeys(
        ("candidates", "missing", "transcribed", "empty", "failed", "deferred", "projected"), 0
    )
    hashes = [
        sha
        for (sha,) in conn.execute(
            f"SELECT ii.sha256 FROM inline_images ii WHERE {TRANSCRIPTION_OWED_SQL}"
            " ORDER BY ii.visioned_at DESC, ii.sha256"
        )
    ]
    todo: list[tuple[str, Path]] = []
    for sha in hashes:
        path = file_on_disk(conn, sha)
        if path is None:
            stats["missing"] += 1
            continue
        stats["candidates"] += 1
        if not limit or len(todo) < limit:
            todo.append((sha, path))
    if dry_run:
        return stats

    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    def _transcribe(sha: str, path: Path, work_conn: sqlite3.Connection) -> str:
        if deadline is not None and time.monotonic() >= deadline:
            return "deferred"
        from src.extract.image_vision import transcribe_image

        try:
            text = transcribe_image(path)
        except Exception as e:
            logger.error(f"Transcription failed for {path}: {e}")
            if not _service_failed(e):
                work_conn.execute(
                    "UPDATE inline_images SET transcription_attempts ="
                    " transcription_attempts + 1 WHERE sha256 = ?",
                    (sha,),
                )
                work_conn.commit()
            return "failed"
        work_conn.execute(
            "UPDATE inline_images SET vision_transcription = ?, transcribed_at = ?"
            " WHERE sha256 = ?",
            (text, datetime.now(UTC).isoformat(), sha),
        )
        work_conn.commit()
        return "transcribed" if text else "empty"

    db_file = next(
        (f for _id, name, f in conn.execute("PRAGMA database_list") if name == "main"),
        None,
    )
    if workers > 1 and db_file:
        # One connection per task, opened and closed in its own thread, as in run_backfill.
        from concurrent.futures import ThreadPoolExecutor

        from src.store.schema import get_connection

        def worker(item: tuple[str, Path]) -> str:
            c = get_connection(db_file)
            try:
                return _transcribe(*item, c)
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for outcome in ex.map(worker, todo):
                stats[outcome] += 1
    else:
        for sha, path in todo:
            stats[_transcribe(sha, path, conn)] += 1

    stats["projected"] = project_vision_text(conn)
    return stats


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
    # A row registered before hashes were recorded gets its hash here, so the next run can
    # tell the image is done (IMAGE_OWED_SQL keys on it). Committed with the occurrence below.
    conn.execute(
        "UPDATE attachments SET sha256 = ? WHERE id = ? AND sha256 IS NULL",
        (sha256, attachment_id),
    )

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
            # Counted, so an image the model keeps failing on is given up after
            # VISION_ATTEMPTS_LIMIT tries instead of being offered every run for ever.
            # Not when the service or the host failed rather than the image: a giant
            # deferred for memory waits for a quiet run, and an outage must not give up
            # every image it meets.
            if not _service_failed(e):
                conn.execute(
                    "UPDATE inline_images SET vision_attempts = vision_attempts + 1"
                    " WHERE sha256 = ?",
                    (sha256,),
                )
                conn.commit()
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


def _image_is_done(conn: sqlite3.Connection, sha256: str) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM inline_images ii WHERE ii.sha256 = ? AND {IMAGE_DONE_SQL}", (sha256,)
        ).fetchone()
        is not None
    )


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
        unprocessed_only: If True, take only images the pipeline still owes work
            (IMAGE_OWED_SQL), so the per-run limit targets that work and the backlog
            drains instead of re-scanning the same newest images every run.
        deadline_s: Optional wall-clock budget in seconds. Once spent, no further
            images are STARTED and the rest are returned as `deferred`; images
            already in flight run to completion. Lets a caller under an external
            timeout (systemd `TimeoutStartSec`) return cleanly with partial work
            instead of being SIGTERMed mid-flight. None = no time box.

    Returns:
        Stats dict: {scanned, classified, recorded, missing, failed, deferred, projected}.
        `recorded` counts occurrences written from the stored hash for an image whose file is
        gone but which is already done (see below). `projected` counts attachment rows given
        a content image's vision text (project_vision_text).
    """
    stats = {
        "scanned": 0,
        "classified": 0,
        "recorded": 0,
        "missing": 0,
        "failed": 0,
        "deferred": 0,
        "projected": 0,
    }

    # Build query
    query = """
        SELECT
            a.id,
            a.file_path,
            a.filename,
            a.message_id,
            COALESCE(e.sender_address, ''),
            e.content,
            e.date_received,
            h.html,
            a.sha256
        FROM attachments a
        LEFT JOIN emails e ON a.email_id = e.id
        LEFT JOIN email_html h ON h.email_id = e.id
        WHERE a.mime_type LIKE 'image/%'
          AND a.file_path IS NOT NULL
          AND a.file_path NOT LIKE 'text:%'
    """

    params: list[str | int] = []
    if since:
        query += " AND e.date_received >= ?"
        params.append(since)

    # Only work still owed. This used to skip every image of a message once one of them had
    # an occurrence, so a message's second image was never processed. An image whose email
    # row is gone is taken too (the LEFT JOIN above), with a blank sender.
    if unprocessed_only:
        query += f" AND {IMAGE_OWED_SQL}"

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

    # Count scanned + drop missing files up front; collect the real work list. A file that is
    # gone but whose image is done (the sweep deletes a file once its hash is done, whether or
    # not this message's occurrence was recorded yet) only needs the occurrence, which the
    # stored hash is enough for.
    todo = []
    known = []
    for row in rows:
        file_path = row[1]
        stats["scanned"] += 1
        if not Path(file_path).exists():
            if row[8] and _image_is_done(conn, row[8]):
                known.append(row)
                continue
            logger.warning(f"Image file not found: {file_path}")
            stats["missing"] += 1
            continue
        todo.append(row)

    if dry_run:
        return stats

    for row in known:
        attachment_id, _, filename, message_id, sender_address, content, _, html, sha256 = row
        position = compute_position_in_body(markup_or_text(content, html), filename)
        conn.execute(
            "INSERT OR IGNORE INTO inline_image_occurrences"
            " (sha256, message_id, sender_email, position_in_body) VALUES (?, ?, ?, ?)",
            (sha256, message_id, sender_address, position),
        )
        conn.commit()
        stats["recorded"] += 1

    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    def _out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _process(row, work_conn) -> bool:
        attachment_id, file_path, filename, message_id, sender_address, content, _, html, _ = row
        # cid: references live in the markup, which an HTML body keeps in email_html.
        position = compute_position_in_body(markup_or_text(content, html), filename)
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
    for sender in {row[4] for row in todo + known if row[4]}:
        refresh_signature_index(conn, sender)

    # A description written above reaches attachment search and the embeddings index through
    # the image's text-free attachment rows; its transcription follows in run_transcription.
    stats["projected"] = project_vision_text(conn)

    return stats
