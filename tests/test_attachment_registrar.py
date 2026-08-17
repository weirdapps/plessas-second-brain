"""Tests for registering attachments outlook-cli has already downloaded.

The macOS path (`export_sync_attachments`) drives Mail.app over AppleScript and
is correctly skipped on Linux (src/cli.py: `skip_export = ... or sys.platform
!= "darwin"`). But it is also the ONLY writer of `attachments` rows for real
mail, and nothing replaced it when ingestion moved to the VPS on 2026-06-30.

outlook-cli kept downloading the binaries — 7,387 files across 2,944 message
directories by 2026-08-17 — while the table gained zero rows for the 7,517
emails received in that window. Every downstream stage reads the table, not the
disk, so text extraction, LLM summaries and image vision all sat idle reporting
"0 pending". This registrar closes that gap by recording what is already on
disk, on any platform.
"""

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from src.export.outlook_attachments import register_downloaded_attachments
from src.store.schema import create_database


@pytest.fixture
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = create_database(":memory:")
    yield conn
    conn.close()


def _email(conn, message_id: str, email_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
        (message_id, "2026-08-01T10:00:00Z", f"subject for {message_id}"),
    )
    conn.commit()
    return email_id or cur.lastrowid


def _downloaded(base: Path, message_id: str, *filenames: str) -> Path:
    """Mimic `outlook-cli download-attachments --out <base>/<message_id>/`."""
    msg_dir = base / message_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        (msg_dir / name).write_bytes(b"x" * 512)
    return msg_dir


def test_records_a_downloaded_file_against_its_email(db, tmp_path):
    email_id = _email(db, "AAMk-1")
    _downloaded(tmp_path, "AAMk-1", "image001.png")

    result = register_downloaded_attachments(db, tmp_path)

    row = db.execute(
        "SELECT email_id, message_id, filename, file_path, file_size FROM attachments"
    ).fetchone()
    assert row[0] == email_id, "must link to the email so downstream JOINs see it"
    assert row[1] == "AAMk-1"
    assert row[2] == "image001.png"
    assert row[3] == str(tmp_path / "AAMk-1" / "image001.png")
    assert row[4] == 512
    assert result["registered"] == 1


def test_sets_mime_type_so_the_image_pipeline_can_find_images(db, tmp_path):
    """run_backfill selects on `mime_type LIKE 'image/%'`. A NULL here means the
    report screenshot is registered and still never classified."""
    _email(db, "AAMk-2")
    _downloaded(tmp_path, "AAMk-2", "report.png", "deck.pptx")

    register_downloaded_attachments(db, tmp_path)

    types = dict(db.execute("SELECT filename, mime_type FROM attachments").fetchall())
    assert types["report.png"] == "image/png"
    assert types["deck.pptx"] is not None


def test_is_idempotent_across_runs(db, tmp_path):
    """Runs hourly against a 50 GB tree; re-registering would duplicate every row."""
    _email(db, "AAMk-3")
    _downloaded(tmp_path, "AAMk-3", "a.pdf")

    register_downloaded_attachments(db, tmp_path)
    second = register_downloaded_attachments(db, tmp_path)

    assert second["registered"] == 0
    assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1


def test_registers_a_file_added_to_an_already_known_message(db, tmp_path):
    """Skipping whole directories on 'message already seen' would permanently miss
    an attachment that arrived on a later pass."""
    _email(db, "AAMk-4")
    _downloaded(tmp_path, "AAMk-4", "first.pdf")
    register_downloaded_attachments(db, tmp_path)

    _downloaded(tmp_path, "AAMk-4", "second.png")
    result = register_downloaded_attachments(db, tmp_path)

    assert result["registered"] == 1
    assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2


def test_defers_directories_whose_email_is_not_loaded_yet(db, tmp_path):
    """Attachments download post-commit but a batch can still be mid-flight.
    Recording email_id NULL would orphan the row: the image pipeline JOINs
    emails, so it would never be seen again even once the email lands."""
    _downloaded(tmp_path, "AAMk-not-loaded", "orphan.png")

    result = register_downloaded_attachments(db, tmp_path)

    assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert result["deferred"] == 1


def test_registers_a_deferred_directory_once_its_email_arrives(db, tmp_path):
    _downloaded(tmp_path, "AAMk-5", "late.png")
    register_downloaded_attachments(db, tmp_path)

    _email(db, "AAMk-5")
    result = register_downloaded_attachments(db, tmp_path)

    assert result["registered"] == 1


def test_ignores_the_sharepoint_subdirectory(db, tmp_path):
    """Reference attachments are tracked in sharepoint_links with their own fetch
    status; duplicating them as file attachments double-counts them."""
    _email(db, "AAMk-6")
    msg_dir = _downloaded(tmp_path, "AAMk-6", "real.pdf")
    sp = msg_dir / "sharepoint"
    sp.mkdir()
    (sp / "fetched.xlsx").write_bytes(b"y" * 10)

    register_downloaded_attachments(db, tmp_path)

    names = [r[0] for r in db.execute("SELECT filename FROM attachments").fetchall()]
    assert names == ["real.pdf"]


def test_survives_a_missing_attachments_directory(db, tmp_path):
    """A fresh host has no tree yet; the hourly sync must not crash on it."""
    result = register_downloaded_attachments(db, tmp_path / "nope")

    assert result["registered"] == 0


# The backlog is 7,387 files. Step 6 of the sync then runs Phase 1 and Phase 2
# over whatever became visible, both with `limit=0` (unbounded) — so registering
# the whole tree in one hourly pass would hand an unbounded LLM job to a unit
# with a TimeoutStartSec and get it SIGTERMed. The registrar throttles itself
# instead, and the backlog drains across runs.


def test_limit_bounds_how_much_is_registered_per_run(db, tmp_path):
    for i in range(5):
        _email(db, f"AAMk-lim-{i}")
        _downloaded(tmp_path, f"AAMk-lim-{i}", "a.pdf")

    result = register_downloaded_attachments(db, tmp_path, limit=2)

    assert result["registered"] == 2
    assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2


def test_successive_limited_runs_drain_the_backlog(db, tmp_path):
    for i in range(5):
        _email(db, f"AAMk-drain-{i}")
        _downloaded(tmp_path, f"AAMk-drain-{i}", "a.pdf")

    for _ in range(3):
        register_downloaded_attachments(db, tmp_path, limit=2)

    assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 5


def test_no_limit_registers_everything(db, tmp_path):
    """The deliberate one-shot backfill path."""
    for i in range(5):
        _email(db, f"AAMk-all-{i}")
        _downloaded(tmp_path, f"AAMk-all-{i}", "a.pdf")

    assert register_downloaded_attachments(db, tmp_path)["registered"] == 5


def test_returns_the_ids_it_created(db, tmp_path):
    """Step 6 scopes Phase 1/2 to `new_attachment_ids`, falling back to UNSCOPED
    when the list is empty. Without ids here, the first run after a backlog is
    registered would run an unbounded LLM pass over every pending attachment."""
    _email(db, "AAMk-ids")
    _downloaded(tmp_path, "AAMk-ids", "a.pdf", "b.png")

    result = register_downloaded_attachments(db, tmp_path)

    rows = [r[0] for r in db.execute("SELECT id FROM attachments ORDER BY id")]
    assert result["ids"] == rows
    assert len(result["ids"]) == 2
