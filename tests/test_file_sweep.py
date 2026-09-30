"""The sweep deletes attachment files whose content is already stored, and nothing else.

A file is an input: once Phase 1 has recorded its text (or recorded it as unreadable), and,
for an image, once the vision pass is done with it, nothing reads the file again. Deleting
anything whose content is not stored would lose it, so every "keep" below matters as much as
every "delete".
"""

import hashlib
import json
import os
import sqlite3
import time
from argparse import Namespace
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.store.file_sweep import (
    SweepPolicy,
    classify_files,
    load_policy,
    sweep_files,
)
from src.store.schema import create_database

APPLY = SweepPolicy(apply=True)


@pytest.fixture
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = create_database(":memory:")
    yield conn
    conn.close()


def _att(
    db,
    root: Path,
    dirname: str,
    name: str,
    body: bytes = b"data",
    *,
    mime: str = "application/pdf",
    content: bool = True,
    sha: bool = True,
) -> tuple[Path, int]:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(body)
    cur = db.execute(
        "INSERT INTO attachments (message_id, filename, mime_type, file_size, file_path,"
        " exported_at, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            dirname,
            name,
            mime,
            len(body),
            str(f),
            "2026-09-30T00:00:00",
            hashlib.sha256(body).hexdigest() if sha else None,
        ),
    )
    if content:
        db.execute(
            "INSERT INTO attachment_content (attachment_id, extraction_status, llm_status)"
            " VALUES (?, 'extracted', 'pending')",
            (cur.lastrowid,),
        )
    db.commit()
    return f, cur.lastrowid


def _image(
    db,
    body: bytes,
    *,
    classification: str = "content",
    described: bool = False,
    attempts: int = 0,
) -> None:
    db.execute(
        "INSERT INTO inline_images (sha256, width, height, bytes, classification,"
        " classification_method, classified_at, vision_description, vision_attempts)"
        " VALUES (?, 10, 10, ?, ?, 'test', '2026-09-30', ?, ?)",
        (
            hashlib.sha256(body).hexdigest(),
            len(body),
            classification,
            "a chart" if described else None,
            attempts,
        ),
    )
    db.commit()


def _backdate(path: Path, hours: float) -> None:
    when = time.time() - hours * 3600
    os.utime(path, (when, when))


def test_deletes_a_registered_file_whose_text_is_stored(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "a.pdf")
    stats = sweep_files(db, tmp_path, APPLY)
    assert not f.exists()
    assert stats["deleted"] == 1


def test_keeps_a_file_phase_1_has_not_read(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "a.pdf", content=False)
    stats = sweep_files(db, tmp_path, APPLY)
    assert f.exists()
    assert stats["pending-text"] == 1 and stats["deleted"] == 0


def test_keeps_an_image_the_vision_pass_still_owes(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "i.png", b"img", mime="image/png")
    _image(db, b"img")
    stats = sweep_files(db, tmp_path, APPLY)
    assert f.exists()
    assert stats["pending-image"] == 1


def test_keeps_an_image_the_vision_pass_has_not_seen(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "i.png", b"img", mime="image/png")
    sweep_files(db, tmp_path, APPLY)
    assert f.exists()


def test_deletes_an_image_once_described(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "i.png", b"img", mime="image/png")
    _image(db, b"img", described=True)
    sweep_files(db, tmp_path, APPLY)
    assert not f.exists()


@pytest.mark.parametrize("classification", ["signature", "noise"])
def test_deletes_an_image_skipped_by_design(db, tmp_path, classification):
    f, _ = _att(db, tmp_path, "AAMk-1", "i.png", b"img", mime="image/png")
    _image(db, b"img", classification=classification)
    sweep_files(db, tmp_path, APPLY)
    assert not f.exists()


def test_deletes_an_image_after_three_failed_vision_attempts(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "i.png", b"img", mime="image/png")
    _image(db, b"img", attempts=3)
    sweep_files(db, tmp_path, APPLY)
    assert not f.exists()


def test_an_image_without_a_hash_is_kept(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "i.png", b"img", mime="image/png", sha=False)
    _image(db, b"img", described=True)
    sweep_files(db, tmp_path, APPLY)
    assert f.exists()


def test_report_only_deletes_nothing_and_counts_everything(db, tmp_path):
    kept, _ = _att(db, tmp_path, "AAMk-1", "a.pdf")
    stats = sweep_files(db, tmp_path, SweepPolicy())
    assert kept.exists()
    assert stats["to_delete"] == 1 and stats["deleted"] == 0 and stats["applied"] is False


def test_the_cutoff_keeps_files_older_than_it(db, tmp_path):
    old, _ = _att(db, tmp_path, "AAMk-1", "old.pdf", b"old")
    new, _ = _att(db, tmp_path, "AAMk-2", "new.pdf", b"new")
    _backdate(old, 48)
    cutoff = datetime.now(UTC) - timedelta(hours=1)

    stats = sweep_files(db, tmp_path, SweepPolicy(apply=True, only_newer_than=cutoff))

    assert old.exists() and not new.exists()
    assert stats["before_cutoff"] == 1


def test_leaves_unregistered_files_to_the_reaper(db, tmp_path):
    (tmp_path / "AAMk-orphan").mkdir()
    orphan = tmp_path / "AAMk-orphan" / "x.pdf"
    orphan.write_bytes(b"x")
    stats = sweep_files(db, tmp_path, APPLY)
    assert orphan.exists()
    assert stats["unregistered"] == 1


def test_removes_the_directory_it_empties_and_keeps_one_it_does_not(db, tmp_path):
    only, _ = _att(db, tmp_path, "AAMk-1", "a.pdf", b"a")
    shared, _ = _att(db, tmp_path, "AAMk-2", "b.pdf", b"b")
    stranger = tmp_path / "AAMk-2" / "unregistered.pdf"
    stranger.write_bytes(b"s")

    stats = sweep_files(db, tmp_path, APPLY)

    assert not only.parent.exists()
    assert shared.parent.exists() and stranger.exists() and not shared.exists()
    assert stats["dirs_removed"] == 1


def test_stamps_file_removed_at(db, tmp_path):
    _, att = _att(db, tmp_path, "AAMk-1", "a.pdf")
    sweep_files(db, tmp_path, APPLY)
    stamp = db.execute("SELECT file_removed_at FROM attachments WHERE id = ?", (att,)).fetchone()[0]
    assert stamp is not None


def test_matches_a_document_copy_under_its_unsigned_directory(db, tmp_path):
    """ingest_document files a copy under abs(message_id) while the row keeps the negative id."""
    d = tmp_path / "123456789"
    d.mkdir()
    f = d / "report.pdf"
    f.write_bytes(b"doc")
    cur = db.execute(
        "INSERT INTO attachments (message_id, filename, file_path, exported_at, sha256)"
        " VALUES (-123456789, 'report.pdf', ?, 'now', 'h')",
        (str(f),),
    )
    db.execute(
        "INSERT INTO attachment_content (attachment_id, extraction_status, llm_status)"
        " VALUES (?, 'extracted', 'extracted')",
        (cur.lastrowid,),
    )
    db.commit()

    sweep_files(db, tmp_path, APPLY)

    assert not f.exists()


def test_matches_through_a_symlinked_root(db, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    f, _ = _att(db, link, "AAMk-1", "a.pdf")

    sweep_files(db, real, APPLY)

    assert not f.exists()


def test_classify_reports_each_state(db, tmp_path):
    _att(db, tmp_path, "AAMk-1", "done.pdf", b"1")
    _att(db, tmp_path, "AAMk-2", "todo.pdf", b"2", content=False)
    states = sorted(f.state for f in classify_files(db, tmp_path))
    assert states == ["deletable", "pending-text"]


def test_a_missing_policy_is_report_only(tmp_path):
    assert load_policy(tmp_path / "absent.json") == SweepPolicy()


def test_the_policy_file_turns_on_apply_and_a_cutoff(tmp_path):
    p = tmp_path / "sweep-policy.json"
    p.write_text(json.dumps({"apply": True, "only_newer_than": "2026-10-01T09:00:00Z"}))
    policy = load_policy(p)
    assert policy.apply is True
    assert policy.only_newer_than == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("text", ["{not json", '{"apply": true, "only_newer_than": "yesterday"}'])
def test_a_malformed_policy_is_report_only(tmp_path, text):
    p = tmp_path / "sweep-policy.json"
    p.write_text(text)
    assert load_policy(p) == SweepPolicy()


def test_the_cli_defaults_to_report_only(tmp_path, capsys):
    from src.cli import cmd_sweep_files

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    f, _ = _att(conn, tmp_path / "att", "AAMk-1", "a.pdf")
    conn.close()

    cmd_sweep_files(
        Namespace(
            db=db_path, apply=False, only_newer_than=None, policy=False, root=str(tmp_path / "att")
        )
    )

    assert f.exists()
    assert "REPORT ONLY" in capsys.readouterr().out


def test_the_cli_applies_the_policy_file(tmp_path, monkeypatch, capsys):
    import src.store.file_sweep
    from src.cli import cmd_sweep_files

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    f, _ = _att(conn, tmp_path / "att", "AAMk-1", "a.pdf")
    conn.close()
    policy = tmp_path / "sweep-policy.json"
    policy.write_text(json.dumps({"apply": True, "only_newer_than": None}))
    monkeypatch.setattr(src.store.file_sweep, "SWEEP_POLICY_FILE", policy)

    cmd_sweep_files(
        Namespace(
            db=db_path,
            apply=False,
            only_newer_than=None,
            policy=True,
            root=str(tmp_path / "att"),
        )
    )

    assert not f.exists()
    assert "APPLIED" in capsys.readouterr().out


def _elsewhere_policy(tmp_path: Path) -> Path:
    policy = tmp_path / "elsewhere.json"
    policy.write_text(json.dumps({"apply": True, "only_newer_than": None}))
    return policy


def test_sweep_files_takes_no_policy_path(tmp_path, monkeypatch, capsys):
    """--policy reads SWEEP_POLICY_FILE and nothing else: no path typed on the command line
    reaches a file read (SonarCloud pythonsecurity:S8707)."""
    import sys

    from src.cli import main

    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()
    argv = ["brain", "--db", str(db_path), "sweep-files", "--root", str(tmp_path / "att")]
    monkeypatch.setattr(sys, "argv", [*argv, "--policy", str(_elsewhere_policy(tmp_path))])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_the_reaper_takes_no_policy_path(tmp_path, monkeypatch, capsys):
    import sys

    import tests.test_orphan_reaper as tor

    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()
    (tmp_path / "att").mkdir()
    argv = ["reap_orphan_attachments", "--db", str(db_path), "--root", str(tmp_path / "att")]
    monkeypatch.setattr(sys, "argv", [*argv, "--policy", str(_elsewhere_policy(tmp_path))])

    with pytest.raises(SystemExit) as exc:
        tor._REAPER.main()

    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


# ---- Final-review fixes: nothing whose bytes were never read, and every image, is kept ----


def _content(db, att_id: int, status: str, error: str | None) -> None:
    db.execute("DELETE FROM attachment_content WHERE attachment_id = ?", (att_id,))
    db.execute(
        "INSERT INTO attachment_content (attachment_id, extraction_status, extraction_error,"
        " llm_status) VALUES (?, ?, ?, 'pending')",
        (att_id, status, error),
    )
    db.commit()


def test_keeps_a_file_phase_1_could_not_find(db, tmp_path):
    """Phase 1 opens the recorded absolute path; the sweep finds files by directory and name."""
    f, att = _att(db, tmp_path, "AAMk-1", "a.pdf")
    _content(db, att, "failed", "File not found: /Users/someone/Mail/AAMk-1/a.pdf")

    stats = sweep_files(db, tmp_path, APPLY)

    assert f.exists()
    assert stats["unread"] == 1 and stats["deleted"] == 0


@pytest.mark.parametrize(
    "status,error",
    [
        ("skipped", "No legacy .doc converter available (textutil/antiword/catdoc)"),
        ("failed", "pytesseract or Pillow not installed"),
    ],
)
def test_keeps_a_file_this_host_had_no_tool_for(db, tmp_path, status, error):
    f, att = _att(db, tmp_path, "AAMk-1", "old.doc")
    _content(db, att, status, error)
    sweep_files(db, tmp_path, APPLY)
    assert f.exists()


def test_keeps_an_image_recorded_as_octet_stream_until_vision_is_done(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "chart.png", b"img", mime="application/octet-stream")
    stats = sweep_files(db, tmp_path, APPLY)
    assert f.exists()
    assert stats["pending-image"] == 1


def test_an_upper_case_image_mime_is_still_an_image(db, tmp_path):
    f, _ = _att(db, tmp_path, "AAMk-1", "scan", b"img", mime="IMAGE/PNG")
    sweep_files(db, tmp_path, APPLY)
    assert f.exists()


@pytest.mark.parametrize("stored_first", [True, False])
def test_rows_sharing_a_directory_and_name_must_all_be_stored(db, tmp_path, stored_first):
    f, first = _att(db, tmp_path, "AAMk-1", "a.pdf", content=stored_first)
    cur = db.execute(
        "INSERT INTO attachments (message_id, filename, file_path, exported_at, sha256)"
        " VALUES ('AAMk-1', 'a.pdf', ?, 'now', 'h2')",
        (str(f),),
    )
    if not stored_first:
        db.execute(
            "INSERT INTO attachment_content (attachment_id, extraction_status, llm_status)"
            " VALUES (?, 'extracted', 'pending')",
            (cur.lastrowid,),
        )
    db.commit()

    sweep_files(db, tmp_path, APPLY)

    assert f.exists()


def test_never_follows_a_symlinked_directory(db, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    f, _ = _att(db, outside, "AAMk-1", "a.pdf")
    (root / "AAMk-1").symlink_to(outside / "AAMk-1")

    sweep_files(db, root, APPLY)

    assert f.exists()


def test_a_file_that_vanishes_mid_scan_is_skipped(db, tmp_path, monkeypatch):
    kept, _ = _att(db, tmp_path, "AAMk-1", "a.pdf", b"a")
    real_scandir = os.scandir

    class Vanished:
        name = "gone.pdf"
        path = str(tmp_path / "AAMk-1" / "gone.pdf")

        def is_file(self, follow_symlinks=True):
            return True

        def stat(self, follow_symlinks=True):
            raise FileNotFoundError(self.path)

    def scandir(path):
        entries = list(real_scandir(path))
        if str(path).endswith("AAMk-1"):
            entries.insert(0, Vanished())
        return iter(entries)

    monkeypatch.setattr("src.store.file_sweep.os.scandir", scandir)

    stats = sweep_files(db, tmp_path, APPLY)

    assert stats["deleted"] == 1 and not kept.exists()


def test_an_unlink_error_is_counted_and_the_rest_are_swept_and_stamped(db, tmp_path):
    stuck, stuck_id = _att(db, tmp_path, "AAMk-1", "a.pdf", b"a")
    gone, gone_id = _att(db, tmp_path, "AAMk-2", "b.pdf", b"b")
    stuck.parent.chmod(0o500)
    try:
        stats = sweep_files(db, tmp_path, APPLY)
    finally:
        stuck.parent.chmod(0o700)

    assert stuck.exists() and not gone.exists()
    assert stats["errors"] == 1 and stats["deleted"] == 1
    stamped = {
        r[0] for r in db.execute("SELECT id FROM attachments WHERE file_removed_at IS NOT NULL")
    }
    assert stamped == {gone_id}


@pytest.mark.parametrize(
    "policy",
    [
        {"apply": True},
        {"apply": True, "only_newer_than": ""},
        {"apply": True, "only_newer_than": 0},
        {"apply": True, "only_newer_than": False},
    ],
)
def test_a_missing_or_empty_cutoff_is_report_only(tmp_path, policy):
    """Apply with no cutoff is the backlog deletion; only an explicit null may ask for it."""
    p = tmp_path / "sweep-policy.json"
    p.write_text(json.dumps(policy))
    assert load_policy(p) == SweepPolicy()


def test_an_explicit_null_cutoff_is_the_backlog_deletion(tmp_path):
    p = tmp_path / "sweep-policy.json"
    p.write_text(json.dumps({"apply": True, "only_newer_than": None}))
    assert load_policy(p) == SweepPolicy(apply=True)


def test_policy_problem_names_an_unreadable_file(tmp_path):
    from src.store.file_sweep import policy_problem

    assert policy_problem(tmp_path / "absent.json") is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"apply": False, "only_newer_than": None}))
    assert policy_problem(good) is None
    bad = tmp_path / "bad.json"
    bad.write_text('{"apply": true,}')
    assert policy_problem(bad)


def test_the_cli_reports_unread_files_and_errors(tmp_path, capsys):
    from src.cli import cmd_sweep_files

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    _, att = _att(conn, tmp_path / "att", "AAMk-1", "a.pdf")
    _content(conn, att, "failed", "File not found: /elsewhere/a.pdf")
    conn.close()

    cmd_sweep_files(
        Namespace(
            db=db_path, apply=False, only_newer_than=None, policy=False, root=str(tmp_path / "att")
        )
    )

    out = capsys.readouterr().out
    assert "unread (Phase 1 could not read) : 1" in out
    assert "delete errors" in out
