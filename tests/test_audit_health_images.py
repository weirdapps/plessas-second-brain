"""check_images ages the pending queue, not only its depth (health-3).

sync swallows every Step 8 exception, so a Step 8 that raises on every run
writes no occurrences: `stuck` stays 0 and `pending` grows about 65 a weekday.
On depth alone that stayed OK for 8-10 days until it crossed IMAGE_QUEUE_WARN.
"""

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(pending_received, processed_received=()):
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE inline_images (sha256 TEXT PRIMARY KEY, vision_description TEXT, "
        "classification TEXT, classified_at TEXT, visioned_at TEXT, "
        "vision_attempts INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY, date_received TEXT)")
    db.execute(
        "CREATE TABLE attachments (id INTEGER PRIMARY KEY, email_id INTEGER, message_id TEXT, "
        "mime_type TEXT, file_path TEXT, sha256 TEXT)"
    )
    db.execute("CREATE TABLE inline_image_occurrences (sha256 TEXT, message_id TEXT)")
    rows = [(r, False) for r in pending_received] + [(r, True) for r in processed_received]
    for i, (received, done) in enumerate(rows, start=1):
        db.execute("INSERT INTO emails (id, date_received) VALUES (?, ?)", (i, received))
        db.execute(
            "INSERT INTO attachments (email_id, message_id, mime_type, file_path, sha256)"
            " VALUES (?,?,?,?,?)",
            (i, f"m{i}", "image/png", f"/tmp/i{i}.png", f"s{i}"),
        )
        if done:
            db.execute(
                "INSERT INTO inline_images (sha256, vision_description, classification,"
                " classified_at) VALUES (?, 'described', 'content', ?)",
                (f"s{i}", received),
            )
            db.execute(
                "INSERT INTO inline_image_occurrences (sha256, message_id) VALUES (?, ?)",
                (f"s{i}", f"m{i}"),
            )
    db.commit()
    return db


def _ago(td):
    return (datetime.now(UTC) - td).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_small_queue_that_stopped_draining_warns(hc):
    old = hc.STALE_THRESHOLDS["images_vision"] + hc.IMAGE_PENDING_GRACE + timedelta(hours=2)
    db = _db([_ago(old), _ago(timedelta(hours=1))])

    r = hc.check_images(db)

    assert r["pending"] == 2
    assert r["stuck"] == 0
    assert r["status"] == "WARN"
    assert r["pending_age"] > hc.STALE_THRESHOLDS["images_vision"]


def test_the_hourly_lag_does_not_trip_it(hc):
    """Just past the vision threshold is still inside the grace, so a queue that
    is merely behind a heavy day's arrivals stays quiet."""
    behind = hc.STALE_THRESHOLDS["images_vision"] + hc.IMAGE_PENDING_GRACE - timedelta(hours=2)
    db = _db([_ago(behind), _ago(timedelta(minutes=30))])

    assert hc.check_images(db)["status"] == "OK"


def test_old_processed_images_do_not_count(hc):
    """Only the pending predicate is aged; an image already recorded is done."""
    db = _db([_ago(timedelta(hours=1))], processed_received=[_ago(timedelta(days=30))])

    assert hc.check_images(db)["status"] == "OK"


def test_report_names_the_oldest_queued_image(hc):
    db = _db([_ago(timedelta(days=9))])

    report, issues = hc.build_report([hc.check_images(db)], {}, {}, {}, [])

    line = next(ln for ln in report.splitlines() if "Inline Images" in ln)
    assert "oldest queued 9d" in line
    assert any("Inline Images" in str(i) for i in issues)


def test_an_image_whose_email_is_gone_counts_as_queued(hc):
    db = _db([])
    db.execute(
        "INSERT INTO attachments (email_id, message_id, mime_type, file_path, sha256)"
        " VALUES (999, 'm-gone', 'image/png', '/tmp/gone.png', 's-gone')"
    )

    assert hc.check_images(db)["pending"] == 1


def test_an_image_given_up_on_is_neither_owed_nor_stuck(hc):
    db = _db([])
    db.execute(
        "INSERT INTO inline_images (sha256, classification, classified_at, vision_attempts)"
        " VALUES ('s-x', 'unclassified', '2026-01-01T00:00:00Z', 3)"
    )

    r = hc.check_images(db)

    assert (r["given_up"], r["owed"], r["stuck"]) == (1, 0, 0)
