"""Tests for reaping attachment directories no run can ever register.

A Graph message id encodes the folder holding the message, so triaging one into
Archive-<year> mints a new id and retires the old. `outlook-cli` had already
written the binaries under the old id, and the registrar defers any directory
whose id is absent from `emails`, for ever, because that id is never coming
back. All four ids probed against M365 on 2026-08-31 answered ErrorItemNotFound.

By then the tree held 1,955 such directories and 3.88 GiB. 4,678 of their 5,989
files were byte-for-byte duplicates of rows that already existed, because the
sync re-exported each moved message under its new id and re-downloaded the same
attachments. The other 954 were the only surviving copy: their message was
deleted outright, so nothing re-downloaded them and no row anywhere referenced
them. Deleting the directory wholesale would have destroyed those.

So the reaper splits on that line. Duplicates are removed; unique files are
adopted through the reverse-ingest path, which mints a synthetic email anchor
and makes them searchable for the first time.
"""

import importlib.util
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from src.store.schema import create_database

# Loaded from the file, as test_health_check loads health_check.py: scripts/ is
# not a package, and importing it as one makes mypy see the same source under
# two module names.
_SPEC = importlib.util.spec_from_file_location(
    "reap_orphan_attachments",
    Path(__file__).resolve().parent.parent / "scripts" / "reap_orphan_attachments.py",
)
assert _SPEC and _SPEC.loader
_REAPER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_REAPER)
reap_orphan_attachments = _REAPER.reap_orphan_attachments


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    """A file-backed store, because adoption opens its own connection.

    ATTACHMENTS_DIR is redirected too: ingest_document copies adopted files to
    the module-level root, and left unpatched a test run writes into the real
    data/attachments tree.
    """
    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    monkeypatch.setattr(
        "src.extract.attachment_pipeline.ATTACHMENTS_DIR", tmp_path / "adopted", raising=True
    )
    return str(path)


@pytest.fixture
def db(db_path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()


def _orphan(base: Path, message_id: str, files: dict[str, bytes], age_days: float = 30) -> Path:
    """A directory outlook-cli wrote whose message id is no longer in the store."""
    import os
    import time

    d = base / message_id
    d.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (d / name).write_bytes(body)
    when = time.time() - age_days * 86400
    os.utime(d, (when, when))
    return d


def _registered(db, base: Path, message_id: str, name: str, body: bytes) -> Path:
    """A healthy attachment: bytes on disk and a row that points at them."""
    d = base / message_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_bytes(body)
    db.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?,?,?)",
        (message_id, "2026-08-01T10:00:00Z", "s"),
    )
    eid = db.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[0]
    db.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_size, file_path, "
        "exported_at) VALUES (?,?,?,?,?,?)",
        (eid, message_id, name, len(body), str(path), "2026-08-01T10:00:00"),
    )
    db.commit()
    return path


def test_deletes_a_file_already_registered_under_another_directory(db, db_path, tmp_path):
    """The moved-message case: the sync re-downloaded these exact bytes under the
    message's new id and registered them there. This copy is pure waste."""
    _registered(db, tmp_path, "AAMk-new", "deck.pptx", b"z" * 4096)
    dead = _orphan(tmp_path, "AAMk-old", {"deck.pptx": b"z" * 4096})

    result = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert result["deleted"] == 1
    assert result["adopted"] == 0
    assert not dead.exists()


def test_adopts_a_file_that_exists_nowhere_else(db, db_path, tmp_path):
    """The deleted-message case. Nothing re-downloaded it, no row references it:
    this directory holds the only copy in existence."""
    dead = _orphan(tmp_path, "AAMk-gone", {"contract.pdf": b"unique-bytes"})

    result = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert result["adopted"] == 1
    assert result["deleted"] == 0
    row = db.execute(
        "SELECT filename, file_size FROM attachments WHERE filename = 'contract.pdf'"
    ).fetchone()
    assert row is not None, "the only copy must end up in the table, not the bin"
    assert row[1] == len(b"unique-bytes")
    assert not dead.exists()


def test_an_adopted_file_is_reachable_from_an_email_row(db, db_path, tmp_path):
    """email_id NULL is invisible: every downstream pipeline JOINs emails. An
    adoption that skipped the synthetic anchor would register the row and still
    leave the content unsearchable."""
    _orphan(tmp_path, "AAMk-anchor", {"minutes.docx": b"unique"})

    reap_orphan_attachments(db_path, tmp_path, apply=True)

    row = db.execute(
        "SELECT a.email_id, e.subject FROM attachments a JOIN emails e ON e.id = a.email_id "
        "WHERE a.filename = 'minutes.docx'"
    ).fetchone()
    assert row is not None
    assert row[0] is not None


def test_leaves_a_directory_inside_the_grace_period_alone(db, db_path, tmp_path):
    """Fresh deferrals are a live race with the registrar, not garbage. Reaping
    one destroys an attachment whose email lands in the next batch."""
    fresh = _orphan(tmp_path, "AAMk-inflight", {"new.pdf": b"unique"}, age_days=0)

    result = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert result["adopted"] == 0
    assert result["deleted"] == 0
    assert (fresh / "new.pdf").exists()


def test_never_touches_a_registered_directory(db, db_path, tmp_path):
    live = _registered(db, tmp_path, "AAMk-live", "keep.pdf", b"x" * 32)
    import os
    import time

    old = time.time() - 90 * 86400
    os.utime(live.parent, (old, old))

    result = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert result["deleted"] == 0
    assert live.exists()


def test_dry_run_changes_nothing(db, db_path, tmp_path):
    _registered(db, tmp_path, "AAMk-new2", "dup.pdf", b"q" * 100)
    dead = _orphan(tmp_path, "AAMk-old2", {"dup.pdf": b"q" * 100, "solo.pdf": b"unique2"})
    before = db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]

    result = reap_orphan_attachments(db_path, tmp_path, apply=False)

    assert result["deleted"] == 1
    assert result["adopted"] == 1
    assert (dead / "dup.pdf").exists(), "dry run must not delete"
    assert (dead / "solo.pdf").exists()
    assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == before


def test_keeps_a_duplicate_whose_registered_copy_has_vanished(db, db_path, tmp_path):
    """A row is not bytes. If the registered file_path no longer resolves, this
    orphan is the last copy and deleting it on the strength of the row alone
    loses the content for good."""
    ghost = _registered(db, tmp_path, "AAMk-ghost", "report.xlsx", b"w" * 64)
    ghost.unlink()
    _orphan(tmp_path, "AAMk-old3", {"report.xlsx": b"w" * 64})

    result = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert result["deleted"] == 0
    assert result["adopted"] == 1


def test_leaves_the_sharepoint_subtree_for_its_own_tracker(db, db_path, tmp_path):
    """Reference attachments live in sharepoint_links with their own fetch state
    and their own given-up rule. They are not this reaper's to delete."""
    dead = _orphan(tmp_path, "AAMk-sp", {})
    sp = dead / "sharepoint"
    sp.mkdir()
    (sp / "fetched.xlsx").write_bytes(b"sp-bytes")

    result = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert (sp / "fetched.xlsx").exists()
    assert result["adopted"] == 0
    assert result["deleted"] == 0


def test_removes_an_empty_orphan_directory(db, db_path, tmp_path):
    dead = _orphan(tmp_path, "AAMk-empty", {})

    result = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert not dead.exists()
    assert result["dirs_removed"] == 1


def test_is_idempotent(db, db_path, tmp_path):
    _registered(db, tmp_path, "AAMk-new4", "dup.pdf", b"e" * 20)
    _orphan(tmp_path, "AAMk-old4", {"dup.pdf": b"e" * 20, "solo.pdf": b"unique4"})

    reap_orphan_attachments(db_path, tmp_path, apply=True)
    second = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert second["adopted"] == 0
    assert second["deleted"] == 0


def test_limit_bounds_the_work_per_run(db, db_path, tmp_path):
    """3.88 GiB and 1,955 directories in one pass would blow the nightly unit's
    TimeoutStartSec, and every adoption also queues an LLM summary."""
    for i in range(5):
        _orphan(tmp_path, f"AAMk-many-{i}", {"solo.pdf": f"unique-{i}".encode()})

    result = reap_orphan_attachments(db_path, tmp_path, apply=True, limit=2)

    assert result["dirs_removed"] == 2


def test_counts_content_already_ingested_separately_from_a_fresh_adoption(db, db_path, tmp_path):
    """ingest_document keys on a CONTENT hash, so a second file with identical
    bytes is reported skipped rather than stored again. Prod holds 98 files of
    exactly 64 bytes in one directory and 95 of exactly 137 in another, all from
    a web fetch that kept returning the same stub. Counting those 193 as
    adoptions would claim the reaper rescued content it merely recognised."""
    _orphan(tmp_path, "AAMk-same-a", {"one.html": b"identical"})
    _orphan(tmp_path, "AAMk-same-b", {"two.html": b"identical"})

    result = reap_orphan_attachments(db_path, tmp_path, apply=True)

    assert result["adopted"] == 1
    assert result["already_ingested"] == 1
    assert db.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1
