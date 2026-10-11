"""The hourly registration scans the message directories that changed since its last
complete scan, and those of the mail its run loaded, not every directory.

Step 3b listed every directory under the attachments tree, and every file in each, to
register the attachments of the mail one run loaded: 8,970 directories on 2026-10-10. A
directory outlook-cli writes or adds a file to changes its mtime, so the ones that changed
since the last complete scan began are the ones with anything new; a directory downloaded
before its email loaded, and passed over then, is named by the run that loads the email.
The nightly `register-attachments` still walks every directory (sb-attachment-pass.sh).
"""

import os
import time
import types
from unittest.mock import patch

import pytest

from src.export.outlook_attachments import SCAN_STAMP_KEY, register_new_downloads
from src.store.schema import create_database

HOUR_NS = 3600 * 10**9


@pytest.fixture
def db():
    conn = create_database(":memory:")
    yield conn
    conn.close()


def _email(conn, message_id, received="2026-10-10T08:00:00Z"):
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, 's')",
        (message_id, received),
    )
    conn.commit()


def _downloaded(base, message_id, *names, days_ago=0.0):
    """A directory as outlook-cli leaves it, last written `days_ago`."""
    msg_dir = base / message_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (msg_dir / name).write_bytes(b"x" * 64)
    if days_ago:
        when = time.time() - days_ago * 86400
        os.utime(msg_dir, (when, when))
    return msg_dir


def _registered(conn):
    return sorted(row[0] for row in conn.execute("SELECT filename FROM attachments"))


def _stamp(conn):
    row = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = ?", (SCAN_STAMP_KEY,)
    ).fetchone()
    return int(row[0]) if row else None


def _scanned_an_hour_ago(conn):
    stamp = time.time_ns() - HOUR_NS
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        (SCAN_STAMP_KEY, str(stamp)),
    )
    conn.commit()
    return stamp


def test_the_first_scan_takes_every_directory_and_records_when_it_began(db, tmp_path):
    _email(db, "AAMk-old")
    _downloaded(tmp_path, "AAMk-old", "old.pdf", days_ago=3)
    _email(db, "AAMk-new")
    _downloaded(tmp_path, "AAMk-new", "new.pdf")
    began = time.time_ns()

    result = register_new_downloads(db, tmp_path)

    assert result["scanned"] == 2
    assert _registered(db) == ["new.pdf", "old.pdf"]
    assert began <= _stamp(db) <= time.time_ns()


def test_a_later_scan_takes_only_the_directories_changed_since(db, tmp_path):
    _scanned_an_hour_ago(db)
    _email(db, "AAMk-quiet")
    _downloaded(tmp_path, "AAMk-quiet", "quiet.pdf", days_ago=2)  # the nightly pass's
    _email(db, "AAMk-fresh")
    _downloaded(tmp_path, "AAMk-fresh", "fresh.pdf")

    result = register_new_downloads(db, tmp_path)

    assert result["scanned"] == 1
    assert _registered(db) == ["fresh.pdf"]


def test_a_file_added_to_an_old_directory_brings_it_back(db, tmp_path):
    """Adding a file changes the directory's mtime: a message can gain an attachment on a
    later pass."""
    _email(db, "AAMk-grew")
    _downloaded(tmp_path, "AAMk-grew", "first.pdf", days_ago=2)
    _scanned_an_hour_ago(db)
    _downloaded(tmp_path, "AAMk-grew", "second.pdf")

    register_new_downloads(db, tmp_path)

    assert _registered(db) == ["first.pdf", "second.pdf"]


def test_the_directory_of_mail_this_run_loaded_is_taken_though_it_did_not_change(db, tmp_path):
    """Downloaded before its email loaded, it was passed over as awaiting its email."""
    _scanned_an_hour_ago(db)
    _email(db, "AAMk-late")
    _downloaded(tmp_path, "AAMk-late", "late.pdf", days_ago=2)

    result = register_new_downloads(db, tmp_path, ["AAMk-late", "AAMk-no-attachments"])

    assert result["scanned"] == 1
    assert _registered(db) == ["late.pdf"]


def test_a_scan_the_limit_cut_short_keeps_the_old_stamp(db, tmp_path):
    stamp = _scanned_an_hour_ago(db)
    for n in range(3):
        _email(db, f"AAMk-{n}")
        _downloaded(tmp_path, f"AAMk-{n}", f"{n}.pdf")

    first = register_new_downloads(db, tmp_path, limit=1)

    assert first["registered"] == 1
    assert _stamp(db) == stamp
    register_new_downloads(db, tmp_path)
    assert _registered(db) == ["0.pdf", "1.pdf", "2.pdf"]
    assert _stamp(db) > stamp


def test_sync_scans_the_directories_of_the_mail_it_loaded(tmp_path, monkeypatch):
    from src import cli, llm_deadline
    from src.store.schema import get_connection

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    _scanned_an_hour_ago(conn)
    conn.close()
    attachments = tmp_path / "attachments"
    _downloaded(attachments, "AAMk-late", "late.pdf", days_ago=2)
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    monkeypatch.setattr("src.cli.ATTACHMENTS_DIR", attachments)
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda: None)

    def load(*args, **kwargs):
        loading = get_connection(str(db_path))
        received = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _email(loading, "AAMk-late", received=received)
        loading.close()
        return 1

    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=True
    )
    ok = {"extracted": 1, "failed": 0, "quota_paused": False}
    with (
        patch("src.extract.local.run_extraction", return_value=ok),
        patch("src.store.loader.load_extractions", side_effect=load),
        patch("src.store.dedup_people.run_dedup", return_value={"total_reduced": 0}),
        patch("src.store.embeddings.build_index"),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        cli.cmd_sync(args)

    conn = get_connection(str(db_path))
    assert _registered(conn) == ["late.pdf"]
    conn.close()
