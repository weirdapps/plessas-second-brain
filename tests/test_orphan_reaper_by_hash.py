"""The reaper recognises a re-download by its stored hash, and a day is enough for that.

Once the sweep deletes attachment files, the registered copy the reaper used to compare an
orphan against is gone. The hash stays in the table, so a byte-identical re-download is still
a duplicate. Downloads happen after their email commits, so an unregistered directory a day
old will never get its email: its duplicates can go. A unique file still waits the full grace
before it is adopted, and deleting a duplicate beside it must not restart that clock.
"""

import hashlib
import time
from datetime import UTC, datetime, timedelta

import tests.test_orphan_reaper as tor

db_path = tor.db_path
db = tor.db
reap = tor.reap_orphan_attachments


def _registered_hash_only(
    db, message_id: str, name: str, body: bytes, *, read: bool = True, received: str | None = None
) -> None:
    """A row that holds the hash while its file is already gone, as after a sweep.

    `read` gives it the Phase 1 row that proves its content is stored. `received` dates its
    email; the default is now, so mail counts as flowing.
    """
    db.execute(
        "INSERT INTO emails (message_id, date_received) VALUES (?, ?)",
        (message_id, received or datetime.now(UTC).isoformat()),
    )
    email_id = db.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[0]
    cur = db.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_size, file_path,"
        " exported_at, sha256) VALUES (?, ?, ?, ?, ?, 'now', ?)",
        (
            email_id,
            message_id,
            name,
            len(body),
            f"/gone/{message_id}/{name}",
            hashlib.sha256(body).hexdigest(),
        ),
    )
    if read:
        db.execute(
            "INSERT INTO attachment_content (attachment_id, extraction_status, llm_status)"
            " VALUES (?, 'extracted', 'extracted')",
            (cur.lastrowid,),
        )
    db.commit()


def test_a_hash_match_is_a_duplicate_even_when_the_registered_copy_is_gone(db, db_path, tmp_path):
    _registered_hash_only(db, "AAMk-original", "report.pdf", b"same bytes")
    dead = tor._orphan(tmp_path, "AAMk-moved", {"report.pdf": b"same bytes"}, age_days=30)

    result = reap(db_path, tmp_path, apply=True)

    assert result["deleted"] == 1 and result["adopted"] == 0
    assert not dead.exists()


def test_duplicates_go_after_a_day_but_unique_files_wait_the_full_grace(db, db_path, tmp_path):
    _registered_hash_only(db, "AAMk-original", "dup.pdf", b"dup")
    d = tor._orphan(
        tmp_path, "AAMk-moved", {"dup.pdf": b"dup", "new.pdf": b"only here"}, age_days=2
    )

    result = reap(db_path, tmp_path, apply=True)

    assert not (d / "dup.pdf").exists()
    assert (d / "new.pdf").exists()
    assert result["deleted"] == 1 and result["adopted"] == 0 and result["waiting"] == 1


def test_the_grace_clock_survives_deleting_a_duplicate(db, db_path, tmp_path):
    _registered_hash_only(db, "AAMk-original", "dup.pdf", b"dup")
    d = tor._orphan(
        tmp_path, "AAMk-moved", {"dup.pdf": b"dup", "new.pdf": b"only here"}, age_days=2
    )
    before = d.stat().st_mtime

    reap(db_path, tmp_path, apply=True)

    assert abs(d.stat().st_mtime - before) < 1


def test_unique_files_are_still_adopted_after_the_grace(db, db_path, tmp_path):
    d = tor._orphan(tmp_path, "AAMk-gone", {"only.pdf": b"nowhere else"}, age_days=8)

    result = reap(db_path, tmp_path, apply=True)

    assert result["adopted"] == 1
    assert not d.exists()


def test_a_fresh_directory_is_untouched_even_if_it_holds_a_duplicate(db, db_path, tmp_path):
    _registered_hash_only(db, "AAMk-original", "dup.pdf", b"dup")
    d = tor._orphan(tmp_path, "AAMk-new", {"dup.pdf": b"dup"}, age_days=0.5)

    result = reap(db_path, tmp_path, apply=True)

    assert (d / "dup.pdf").exists()
    assert result["deleted"] == 0


def test_adopt_only_keeps_duplicates(db, db_path, tmp_path):
    _registered_hash_only(db, "AAMk-original", "dup.pdf", b"dup")
    d = tor._orphan(tmp_path, "AAMk-moved", {"dup.pdf": b"dup", "new.pdf": b"unique"}, age_days=8)

    result = reap(db_path, tmp_path, apply=True, adopt_only=True)

    assert (d / "dup.pdf").exists()
    assert result["adopted"] == 1 and result["deleted"] == 0


def test_the_cutoff_leaves_older_directories_alone(db, db_path, tmp_path):
    _registered_hash_only(db, "AAMk-original", "dup.pdf", b"dup")
    d = tor._orphan(tmp_path, "AAMk-moved", {"dup.pdf": b"dup"}, age_days=30)

    result = reap(db_path, tmp_path, apply=True, only_newer_than=time.time() - 86400)

    assert (d / "dup.pdf").exists()
    assert result["deleted"] == 0


def test_a_hash_match_needs_the_twin_content_stored(db, db_path, tmp_path):
    """A row whose file vanished before Phase 1 read it proves nothing: this orphan is the last copy."""
    _registered_hash_only(db, "AAMk-original", "report.pdf", b"only copy", read=False)
    dead = tor._orphan(tmp_path, "AAMk-moved", {"report.pdf": b"only copy"}, age_days=30)

    result = reap(db_path, tmp_path, apply=True)

    assert result["deleted"] == 0 and result["adopted"] == 1
    assert not dead.exists()


def test_duplicates_wait_the_full_grace_while_mail_loading_is_stalled(db, db_path, tmp_path):
    """Emails load after their attachments download; a stalled load leaves real emails pending."""
    stale = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    _registered_hash_only(db, "AAMk-original", "dup.pdf", b"dup", received=stale)
    d = tor._orphan(tmp_path, "AAMk-moved", {"dup.pdf": b"dup"}, age_days=2)

    result = reap(db_path, tmp_path, apply=True)

    assert (d / "dup.pdf").exists()
    assert result["deleted"] == 0
