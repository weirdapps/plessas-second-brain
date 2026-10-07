"""The health row shows what the VPS still holds, and warns when the sweep stops.

Report-only while the policy says so, however many stored files wait: until the backlog
deletion is confirmed they are supposed to be there. Once the policy turns deletion on, a
stored file the policy covers should be gone within the hour; one older than STALL_HOURS
means the sweep stopped running.
"""

import hashlib
import importlib.util
import json
import os
import sqlite3
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from src.store.schema import create_database

_SPEC = importlib.util.spec_from_file_location(
    "health_check", Path(__file__).resolve().parent.parent / "scripts" / "health_check.py"
)
assert _SPEC and _SPEC.loader
hc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hc)


@pytest.fixture
def db(tmp_path, monkeypatch) -> Generator[sqlite3.Connection, None, None]:
    monkeypatch.setattr(hc, "ATTACHMENTS_DIR", tmp_path / "att")
    monkeypatch.setattr("src.store.file_sweep.SWEEP_POLICY_FILE", tmp_path / "policy.json")
    conn = create_database(":memory:")
    yield conn
    conn.close()


def _stored(
    db,
    root: Path,
    name: str,
    hours_old: float,
    mime: str = "application/pdf",
    content: bool = True,
) -> None:
    d = root / f"AAMk-{name}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(name.encode())
    cur = db.execute(
        "INSERT INTO attachments (message_id, filename, mime_type, file_path, exported_at, sha256)"
        " VALUES (?, ?, ?, ?, 'now', ?)",
        (d.name, name, mime, str(f), hashlib.sha256(name.encode()).hexdigest()),
    )
    if content:
        db.execute(
            "INSERT INTO attachment_content (attachment_id, extraction_status, llm_status)"
            " VALUES (?, 'extracted', 'pending')",
            (cur.lastrowid,),
        )
    db.commit()
    when = time.time() - hours_old * 3600
    os.utime(f, (when, when))


def _policy(tmp_path: Path, apply: bool, cutoff: str | None = None) -> None:
    (tmp_path / "policy.json").write_text(json.dumps({"apply": apply, "only_newer_than": cutoff}))


def test_report_only_is_ok_however_much_waits(db, tmp_path):
    _stored(db, tmp_path / "att", "old.pdf", hours_old=500)
    row = hc.check_files_on_disk(db)
    assert row["status"] == "OK" and row["mode"] == "report-only"
    assert row["counts"]["deletable"] == 1


def test_a_stalled_sweep_warns_once_deletion_is_on(db, tmp_path):
    _policy(tmp_path, apply=True)
    _stored(db, tmp_path / "att", "late.pdf", hours_old=7)
    row = hc.check_files_on_disk(db)
    assert row["status"] == "WARN" and row["stalled"] == 1


def test_files_before_the_cutoff_do_not_count_as_stalled(db, tmp_path):
    _policy(tmp_path, apply=True, cutoff="2099-01-01T00:00:00Z")
    _stored(db, tmp_path / "att", "backlog.pdf", hours_old=500)
    assert hc.check_files_on_disk(db)["status"] == "OK"


def test_an_image_waiting_three_days_for_vision_warns(db, tmp_path):
    _stored(db, tmp_path / "att", "i.png", hours_old=80, mime="image/png")
    row = hc.check_files_on_disk(db)
    assert row["status"] == "WARN" and row["image_late"] == 1


def test_the_report_line_names_the_mode_and_the_states(db, tmp_path):
    _stored(db, tmp_path / "att", "a.pdf", hours_old=1)
    row = hc.check_files_on_disk(db)
    line = hc.files_on_disk_detail(row)
    assert "report-only" in line and "1 stored and removable" in line
    assert chr(0x2014) not in line and chr(0x2013) not in line


def test_a_scan_error_is_a_warn_row_not_a_crash(db, monkeypatch):
    """main() runs the checks unguarded, so an exception here would take down the whole report."""

    def boom(*_a, **_k):
        raise OSError("disk went away")

    monkeypatch.setattr("src.store.file_sweep.classify_files", boom)
    row = hc.check_files_on_disk(db)
    assert row["status"] == "WARN"
    assert "disk went away" in hc.files_on_disk_detail(row)


def test_an_unreadable_policy_file_warns(db, tmp_path):
    """A hand edit with a trailing comma must not stop deletion while the row reads OK."""
    (tmp_path / "policy.json").write_text('{"apply": true,}')
    row = hc.check_files_on_disk(db)
    assert row["status"] == "WARN"
    assert "unreadable" in hc.files_on_disk_detail(row)


def test_files_kept_for_their_content_or_for_curation_are_named(db, tmp_path, monkeypatch):
    _stored(db, tmp_path / "att", "locked.xlsx", hours_old=1)
    _stored(db, tmp_path / "att", "deck.pptx", hours_old=1)
    db.execute(
        "UPDATE attachment_content SET extraction_status = 'encrypted',"
        " extraction_method = 'password' WHERE attachment_id = 1"
    )
    db.commit()
    state = tmp_path / "curate-state.json"
    state.write_text(json.dumps({"deferred": {"2": {"folder": "x", "attempts": 1}}}))
    monkeypatch.setenv("BRAIN_CURATE_STATE", str(state))

    row = hc.check_files_on_disk(db)
    line = hc.files_on_disk_detail(row)

    assert row["counts"]["not-held"] == 1 and row["counts"]["curation"] == 1
    assert "1 content not held" in line and "1 awaiting curation" in line


def test_an_unreadable_curation_state_warns(db, tmp_path, monkeypatch):
    """The sweep deletes nothing while it cannot tell which originals curation needs."""
    state = tmp_path / "curate-state.json"
    state.write_text('{"deferred": ')
    monkeypatch.setenv("BRAIN_CURATE_STATE", str(state))

    row = hc.check_files_on_disk(db)

    assert row["status"] == "WARN"
    assert "deleted nothing" in hc.files_on_disk_detail(row)


def test_files_phase_1_could_not_read_are_counted(db, tmp_path):
    _stored(db, tmp_path / "att", "a.pdf", hours_old=1)
    db.execute(
        "UPDATE attachment_content SET extraction_status = 'failed',"
        " extraction_error = 'File not found: /elsewhere/a.pdf'"
    )
    db.commit()
    row = hc.check_files_on_disk(db)
    assert row["counts"]["unread"] == 1
    assert "1 unread" in hc.files_on_disk_detail(row)
