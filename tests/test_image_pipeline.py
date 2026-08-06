"""
Tests for image pipeline orchestrator.
"""

import itertools
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from PIL import Image

from src.extract import image_pipeline
from src.extract.image_pipeline import (
    compute_position_in_body,
    process_single_image,
    run_backfill,
)
from src.store.schema import create_database


@pytest.fixture
def db() -> Generator[sqlite3.Connection, None, None]:
    """Create a fresh in-memory DB with schema."""
    conn = create_database(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def sample_image(tmp_path: Path) -> Path:
    """Generate a 200x200 PNG that passes Stage 1 dimension/byte filters."""
    img_path = tmp_path / "test_image.png"
    img = Image.new("RGB", (200, 200), color="red")
    img.save(img_path, "PNG")
    return img_path


def _insert_test_email_and_attachment(
    conn: sqlite3.Connection,
    email_id: int,
    msg_id: str,
    sender: str,
    img_path: Path,
) -> int:
    """
    Insert minimal email + attachment rows for testing.
    Returns attachment_id.
    """
    # Insert email
    conn.execute(
        """
        INSERT INTO emails (id, message_id, sender_address, subject, date_received, content)
        VALUES (?, ?, ?, 'Test', datetime('now'), 'Test email with image')
        """,
        (email_id, msg_id, sender),
    )

    # Insert attachment
    file_size = img_path.stat().st_size if img_path.exists() else 0
    cursor = conn.execute(
        """
        INSERT INTO attachments (
            email_id, message_id, filename, file_path,
            mime_type, file_size, exported_at
        )
        VALUES (?, ?, ?, ?, 'image/png', ?, datetime('now'))
        """,
        (email_id, msg_id, img_path.name, str(img_path), file_size),
    )
    attachment_id = cursor.lastrowid
    assert attachment_id is not None
    conn.commit()
    return attachment_id


def test_process_image_attachment_stage1(db: sqlite3.Connection, sample_image: Path):
    """Process an image with run_vision=False, verify DB rows created."""
    email_id = 1
    msg_id = "test-msg-001"
    sender = "test@example.com"
    attachment_id = _insert_test_email_and_attachment(db, email_id, msg_id, sender, sample_image)

    # Process with Stage 1 only
    result = process_single_image(
        conn=db,
        attachment_id=attachment_id,
        img_path=sample_image,
        sender_email=sender,
        message_id=msg_id,
        position_in_body=0.5,
        run_vision=False,
    )

    # Should have a classification result
    assert result is not None

    # Verify inline_images has at least 1 row
    count_images = db.execute("SELECT COUNT(*) FROM inline_images").fetchone()[0]
    assert count_images >= 1

    # Verify inline_image_occurrences has at least 1 row
    count_occurrences = db.execute("SELECT COUNT(*) FROM inline_image_occurrences").fetchone()[0]
    assert count_occurrences >= 1


def test_process_image_idempotent(db: sqlite3.Connection, sample_image: Path):
    """Process same image twice, verify inline_images has exactly 1 row."""
    email_id = 1
    msg_id = "test-msg-002"
    sender = "test@example.com"
    attachment_id = _insert_test_email_and_attachment(db, email_id, msg_id, sender, sample_image)

    # Process twice
    process_single_image(
        conn=db,
        attachment_id=attachment_id,
        img_path=sample_image,
        sender_email=sender,
        message_id=msg_id,
        position_in_body=0.5,
        run_vision=False,
    )

    process_single_image(
        conn=db,
        attachment_id=attachment_id,
        img_path=sample_image,
        sender_email=sender,
        message_id=msg_id,
        position_in_body=0.5,
        run_vision=False,
    )

    # Should have exactly 1 row in inline_images (same SHA256)
    count_images = db.execute("SELECT COUNT(*) FROM inline_images").fetchone()[0]
    assert count_images == 1

    # Should have exactly 1 row in inline_image_occurrences (same PK)
    count_occurrences = db.execute("SELECT COUNT(*) FROM inline_image_occurrences").fetchone()[0]
    assert count_occurrences == 1


def test_compute_position_in_body_found():
    """Test position computation when filename is found in content."""
    content = 'Some text before <img src="cid:test_image.png"> some text after'
    position = compute_position_in_body(content, "test_image.png")
    # Should be around the middle (18 chars before / ~64 total)
    assert 0.0 <= position <= 1.0
    assert position > 0.2  # Should be past the start


def test_compute_position_in_body_not_found():
    """Test position computation when filename is not in content."""
    content = "Some text without image reference"
    position = compute_position_in_body(content, "test_image.png")
    # Should default to 0.5
    assert position == 0.5


def test_compute_position_in_body_none_content():
    """Test position computation when content is None."""
    position = compute_position_in_body(None, "test_image.png")
    # Should default to 0.5
    assert position == 0.5


def test_run_backfill_dry_run(db: sqlite3.Connection, sample_image: Path):
    """Test backfill in dry_run mode (just count, don't classify)."""
    email_id = 1
    msg_id = "test-msg-003"
    sender = "test@example.com"
    _insert_test_email_and_attachment(db, email_id, msg_id, sender, sample_image)

    stats = run_backfill(conn=db, dry_run=True)

    # Should have scanned 1 image
    assert stats["scanned"] == 1
    # Should not have classified (dry run)
    assert stats["classified"] == 0
    # No rows in inline_images (dry run)
    count_images = db.execute("SELECT COUNT(*) FROM inline_images").fetchone()[0]
    assert count_images == 0


def test_run_backfill_with_limit(db: sqlite3.Connection, sample_image: Path):
    """Test backfill with limit parameter."""
    # Insert 3 test emails with images
    for i in range(3):
        _insert_test_email_and_attachment(
            db, i + 1, f"test-msg-{i:03d}", "test@example.com", sample_image
        )

    # Backfill with limit=2
    stats = run_backfill(conn=db, limit=2, run_vision=False)

    # Should have scanned only 2 (limit)
    assert stats["scanned"] == 2
    assert stats["classified"] == 2


def test_run_backfill_unprocessed_only_skips_processed_messages(
    db: sqlite3.Connection, sample_image: Path
):
    """With unprocessed_only=True, images from messages already recorded in
    inline_image_occurrences are skipped, so the limited budget targets new work
    instead of re-scanning the same newest images (cache-hits) every run."""
    # msg-done: process it once so it gains an inline_image_occurrences row.
    _insert_test_email_and_attachment(db, 1, "msg-done", "a@example.com", sample_image)
    run_backfill(conn=db, run_vision=False)

    # msg-new arrives later, never processed.
    _insert_test_email_and_attachment(db, 2, "msg-new", "b@example.com", sample_image)
    db.commit()

    stats = run_backfill(conn=db, run_vision=False, unprocessed_only=True)

    # msg-done already has an occurrence → skipped; only msg-new is scanned.
    assert stats["scanned"] == 1


def test_undecodable_image_is_recorded_not_infinitely_rescanned(
    db: sqlite3.Connection, tmp_path: Path
):
    """An image file PIL cannot decode (corrupt / unsupported / decompression
    bomb) is recorded as noise/decode_failed with an occurrence, so
    unprocessed_only does not re-scan it every run forever."""
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"this is definitely not a valid PNG")
    _insert_test_email_and_attachment(db, 1, "msg-bad", "x@example.com", bad)

    stats = run_backfill(conn=db, run_vision=False)
    assert stats["scanned"] == 1

    # Recorded as a decode failure (not silently dropped)...
    row = db.execute("SELECT classification, classification_method FROM inline_images").fetchone()
    assert tuple(row) == ("noise", "decode_failed")
    # ...and a second unprocessed_only pass skips it (not re-scanned).
    stats2 = run_backfill(conn=db, run_vision=False, unprocessed_only=True)
    assert stats2["scanned"] == 0


def test_heic_image_is_decoded_not_marked_failed(db: sqlite3.Connection, tmp_path: Path):
    """With pillow-heif registered, an .heic image (iPhone default) is decoded and
    classified normally rather than marked as a decode failure."""
    pytest.importorskip("pillow_heif")
    p = tmp_path / "photo.heic"
    Image.new("RGB", (200, 200), "blue").save(p, format="HEIF")
    _insert_test_email_and_attachment(db, 1, "msg-heic", "y@example.com", p)

    run_backfill(conn=db, run_vision=False)

    method = db.execute("SELECT classification_method FROM inline_images").fetchone()[0]
    assert method != "decode_failed"  # decoded, not marked unprocessable


def test_run_backfill_defers_remaining_work_at_deadline(
    db: sqlite3.Connection, sample_image: Path, monkeypatch: pytest.MonkeyPatch
):
    """With deadline_s set, the backfill stops starting new images once the budget is
    spent and reports the rest as deferred — instead of running past the caller's
    systemd timeout and being SIGTERMed mid-flight."""
    for i in range(5):
        _insert_test_email_and_attachment(
            db, i + 1, f"dl-msg-{i:03d}", "t@example.com", sample_image
        )

    # Fake monotonic clock: each consultation advances 1s, so a 2s budget runs out
    # partway through the 5 images regardless of how fast the real work is.
    ticks = itertools.count()
    monkeypatch.setattr(image_pipeline.time, "monotonic", lambda: next(ticks))

    stats = run_backfill(conn=db, run_vision=False, deadline_s=2)

    assert stats["classified"] < 5, "should have stopped before working the whole list"
    assert stats["deferred"] > 0
    # Every scanned image is accounted for — nothing silently vanishes.
    assert stats["classified"] + stats["failed"] + stats["deferred"] == stats["scanned"]


def test_run_backfill_without_deadline_processes_everything(
    db: sqlite3.Connection, sample_image: Path
):
    """deadline_s=None (the default) keeps the pre-existing behaviour: no time box,
    nothing deferred."""
    for i in range(3):
        _insert_test_email_and_attachment(
            db, i + 1, f"nodl-msg-{i:03d}", "t@example.com", sample_image
        )

    stats = run_backfill(conn=db, run_vision=False)

    assert stats["classified"] == 3
    assert stats["deferred"] == 0


def test_run_backfill_refreshes_signature_index(db: sqlite3.Connection, sample_image: Path):
    """run_backfill defers per-image refresh, then rebuilds the sender index once."""
    # Same image from one sender across 3 emails -> 3 occurrences, 1 sha.
    for i in range(3):
        _insert_test_email_and_attachment(
            db, i + 1, f"sig-msg-{i:03d}", "sig@example.com", sample_image
        )

    run_backfill(conn=db, run_vision=False)

    rows = db.execute(
        "SELECT occurrence_count, sender_total, frequency FROM sender_signature_index "
        "WHERE sender_email = ?",
        ("sig@example.com",),
    ).fetchall()
    # End-of-run refresh must have populated the index from all occurrences.
    assert len(rows) == 1
    occurrence_count, sender_total, frequency = rows[0]
    assert occurrence_count == 3
    assert sender_total == 3
    assert frequency == 1.0


def test_process_single_image_missing_file(db: sqlite3.Connection):
    """Test processing when image file doesn't exist."""
    email_id = 1
    msg_id = "test-msg-004"
    sender = "test@example.com"
    missing_path = Path("/nonexistent/image.png")

    # Insert email + attachment with missing path
    attachment_id = _insert_test_email_and_attachment(db, email_id, msg_id, sender, missing_path)

    # Process should return None and log warning
    result = process_single_image(
        conn=db,
        attachment_id=attachment_id,
        img_path=missing_path,
        sender_email=sender,
        message_id=msg_id,
        position_in_body=0.5,
        run_vision=False,
    )

    assert result is None
