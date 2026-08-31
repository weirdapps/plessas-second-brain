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


def _email(
    conn, message_id: str, email_id: int | None = None, received: str = "2026-08-01T10:00:00Z"
) -> int:
    cur = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
        (message_id, received, f"subject for {message_id}"),
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


# Splitting incremental from bulk. The hourly sync has TimeoutStartSec=600 and
# is sized for a handful of new emails; letting it also chew the 9k-file
# backfill is what made every scheduled run on 2026-08-18 overrun and get
# SIGTERMed, with only the 90 s retry completing. `since` keeps the hourly path
# on fresh mail only, while the nightly sb-attachments job (TimeoutStartSec=1h)
# passes no `since` and drains everything.


def test_since_registers_attachments_for_recent_mail(db, tmp_path):
    _email(db, "AAMk-fresh", received="2026-08-18T09:00:00Z")
    _downloaded(tmp_path, "AAMk-fresh", "today.png")

    result = register_downloaded_attachments(db, tmp_path, since="2026-08-18T00:00:00Z")

    assert result["registered"] == 1


def test_since_leaves_the_backlog_for_the_bulk_pass(db, tmp_path):
    """An old email's attachments must not be pulled into the hourly window."""
    _email(db, "AAMk-old", received="2026-07-01T09:00:00Z")
    _downloaded(tmp_path, "AAMk-old", "backlog.pdf")

    result = register_downloaded_attachments(db, tmp_path, since="2026-08-18T00:00:00Z")

    assert result["registered"] == 0
    assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


def test_bulk_pass_registers_the_backlog_too(db, tmp_path):
    """No `since` is the nightly path: everything on disk, old and new."""
    _email(db, "AAMk-old2", received="2026-07-01T09:00:00Z")
    _email(db, "AAMk-new2", received="2026-08-18T09:00:00Z")
    _downloaded(tmp_path, "AAMk-old2", "backlog.pdf")
    _downloaded(tmp_path, "AAMk-new2", "today.png")

    assert register_downloaded_attachments(db, tmp_path)["registered"] == 2


def test_backlog_deferred_by_since_is_not_lost(db, tmp_path):
    """Skipped for the window, not consumed — the bulk pass must still find it."""
    _email(db, "AAMk-old3", received="2026-07-01T09:00:00Z")
    _downloaded(tmp_path, "AAMk-old3", "backlog.pdf")

    register_downloaded_attachments(db, tmp_path, since="2026-08-18T00:00:00Z")

    assert register_downloaded_attachments(db, tmp_path)["registered"] == 1


# Apple Mail-era message ids are INTEGERS (50,270 such emails); the outlook-cli
# era uses text ids. Directory names are always strings, so `emails.get("1000")`
# missed an email stored as 1000 and 17,804 directories reported as "awaiting
# their email" forever.
#
# The dedup set has the identical mismatch, and that is the dangerous half:
# normalising only the email lookup would have re-registered 29,957 files the
# macOS exporter had already recorded, then paid for Phase 2 on all of them.
# Only 31 files were genuinely missing. Both sides normalise, or neither.


def _email_int_id(conn, message_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
        (message_id, "2026-03-01T10:00:00Z", "legacy"),
    )
    conn.commit()
    return cur.lastrowid


def test_registers_against_an_integer_message_id(db, tmp_path):
    email_id = _email_int_id(db, 1000)
    _downloaded(tmp_path, "1000", "legacy.pdf")

    result = register_downloaded_attachments(db, tmp_path)

    assert result["registered"] == 1
    assert result["deferred"] == 0
    assert db.execute("SELECT email_id FROM attachments").fetchone()[0] == email_id


def test_does_not_duplicate_an_attachment_recorded_under_an_integer_id(db, tmp_path):
    """The macOS exporter wrote message_id as an integer. Re-registering those
    would have duplicated ~30k rows and re-billed Phase 2 for every one."""
    _email_int_id(db, 1000)
    _downloaded(tmp_path, "1000", "legacy.pdf")
    db.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_path, exported_at) "
        "VALUES (1, 1000, 'legacy.pdf', '/old/path/legacy.pdf', '2026-03-01T10:00:00')"
    )
    db.commit()

    result = register_downloaded_attachments(db, tmp_path)

    assert result["registered"] == 0
    assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1


def test_keeps_the_message_id_type_the_email_uses(db, tmp_path):
    """Storing '1000' beside an existing 1000 would break every join on the
    column — SQLite does not equate the two."""
    _email_int_id(db, 1000)
    _downloaded(tmp_path, "1000", "legacy.pdf")

    register_downloaded_attachments(db, tmp_path)

    assert db.execute("SELECT typeof(message_id) FROM attachments").fetchone()[0] == "integer"


# An unbounded deferral is a leak, not a wait. Graph message ids encode the
# containing folder, so triaging a message into Archive-<year> mints a NEW id and
# retires the old one; deleting it retires the id outright. Either way the
# directory `outlook-cli` already wrote under the OLD id can never be matched to
# an email again — all four ids probed against M365 on 2026-08-31 returned
# ErrorItemNotFound. The sync then re-exports the moved message under its new id
# and re-downloads the same files, which is why 4,678 of 5,989 orphaned files
# were byte-identical duplicates of rows that already existed.
#
# Nothing bounded that. 1,955 directories and 3.88 GiB accrued between April and
# August 2026, every one of them rescanned on every run and counted by the health
# check on every report, so the WARN could never clear no matter what was fixed.
# Ageing the deferral out separates "the email is still in flight" from "the email
# is never coming", which is the same distinction check_sharepoint already draws
# when it says "given up, no longer retried".


def _age_dir(path: Path, days: float) -> None:
    """Backdate a directory's mtime, as a stale download would be."""
    import os
    import time

    when = time.time() - days * 86400
    os.utime(path, (when, when))


def test_a_recent_deferral_is_still_actionable(db, tmp_path):
    """Attachments land post-commit and a batch can be mid-flight; today's
    directory must stay in the retry set."""
    _downloaded(tmp_path, "AAMk-inflight", "orphan.png")

    result = register_downloaded_attachments(db, tmp_path)

    assert result["deferred"] == 1
    assert result["abandoned"] == 0


def test_abandons_a_deferral_older_than_the_grace_period(db, tmp_path):
    """Its message id is gone from the store; no future run can resolve it."""
    msg_dir = _downloaded(tmp_path, "AAMk-moved-away", "orphan.png")
    _age_dir(msg_dir, days=30)

    result = register_downloaded_attachments(db, tmp_path)

    assert result["deferred"] == 0
    assert result["abandoned"] == 1


def test_an_aged_directory_is_still_registered_once_its_email_appears(db, tmp_path):
    """Abandonment classifies a report line; it must never refuse real work.
    A backfill that loads old mail has to be able to claim its attachments."""
    msg_dir = _downloaded(tmp_path, "AAMk-late-arrival", "late.png")
    _age_dir(msg_dir, days=30)
    _email(db, "AAMk-late-arrival")

    result = register_downloaded_attachments(db, tmp_path)

    assert result["registered"] == 1
    assert result["abandoned"] == 0


def test_grace_period_is_configurable(db, tmp_path):
    msg_dir = _downloaded(tmp_path, "AAMk-tunable", "orphan.png")
    _age_dir(msg_dir, days=3)

    assert register_downloaded_attachments(db, tmp_path, grace_days=1)["abandoned"] == 1
    assert register_downloaded_attachments(db, tmp_path, grace_days=10)["deferred"] == 1


def test_the_since_window_never_abandons_the_backlog(db, tmp_path):
    """`since` narrows the email set to the hourly window, so an old email that
    IS loaded looks identical to one that never existed. Abandoning on that view
    would condemn the very backlog the nightly bulk pass exists to register."""
    _email(db, "AAMk-old-backlog", received="2026-07-01T09:00:00Z")
    msg_dir = _downloaded(tmp_path, "AAMk-old-backlog", "backlog.pdf")
    _age_dir(msg_dir, days=30)

    result = register_downloaded_attachments(db, tmp_path, since="2026-08-18T00:00:00Z")

    assert result["abandoned"] == 0
    assert register_downloaded_attachments(db, tmp_path)["registered"] == 1
