"""An Archive copy's attachment directory registers against the email the store kept.

A move to Archive mints a new Graph id, the Archive export downloads the message's
attachments again under it, and the loader keeps the email it already held. With
no record of the new id the directory never matched an email: 858 of 885 orphan
directories on 2026-10-07. The loader now records the id as an alias, and the
registrar resolves the directory through it, registering only what the email does
not already have: by filename first, then by content (sha256).
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


def _email(conn, message_id: str, received: str = "2026-10-01T10:00:00Z") -> int:
    cur = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, 's')",
        (message_id, received),
    )
    conn.commit()
    return cur.lastrowid


def _alias(conn, alias: str, email_id: int) -> None:
    conn.execute(
        "INSERT INTO email_aliases (message_id, email_id, recorded_at) VALUES (?, ?, 'now')",
        (alias, email_id),
    )
    conn.commit()


def _file(base: Path, message_id: str, name: str, content: bytes = b"report") -> Path:
    path = base / message_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _rows(conn):
    return conn.execute(
        "SELECT email_id, message_id, filename, file_path FROM attachments ORDER BY id"
    ).fetchall()


def test_a_copys_directory_registers_against_the_stored_email(db, tmp_path):
    email_id = _email(db, "AAMk-inbox")
    _alias(db, "AAMk-archive", email_id)
    path = _file(tmp_path, "AAMk-archive", "report.pdf")

    stats = register_downloaded_attachments(db, tmp_path)

    assert [tuple(r) for r in _rows(db)] == [(email_id, "AAMk-inbox", "report.pdf", str(path))]
    assert stats["registered"] == 1
    assert stats["deferred"] == stats["abandoned"] == 0


def test_a_file_the_email_already_has_is_not_registered_again(db, tmp_path):
    email_id = _email(db, "AAMk-inbox")
    _file(tmp_path, "AAMk-inbox", "report.pdf")
    assert register_downloaded_attachments(db, tmp_path)["registered"] == 1
    _alias(db, "AAMk-archive", email_id)
    _file(tmp_path, "AAMk-archive", "report.pdf")

    stats = register_downloaded_attachments(db, tmp_path)

    assert stats["registered"] == 0
    assert len(_rows(db)) == 1
    assert stats["deferred"] == stats["abandoned"] == 0


def test_the_same_bytes_under_another_name_are_not_registered_again(db, tmp_path):
    email_id = _email(db, "AAMk-inbox")
    _file(tmp_path, "AAMk-inbox", "image001.png", b"logo")
    register_downloaded_attachments(db, tmp_path)
    _alias(db, "AAMk-archive", email_id)
    _file(tmp_path, "AAMk-archive", "image001 (1).png", b"logo")

    assert register_downloaded_attachments(db, tmp_path)["registered"] == 0
    assert register_downloaded_attachments(db, tmp_path)["registered"] == 0
    assert len(_rows(db)) == 1


def test_a_file_only_the_copy_has_is_registered_once(db, tmp_path):
    email_id = _email(db, "AAMk-inbox")
    _file(tmp_path, "AAMk-inbox", "report.pdf")
    register_downloaded_attachments(db, tmp_path)
    _alias(db, "AAMk-archive", email_id)
    _file(tmp_path, "AAMk-archive", "report.pdf")
    _file(tmp_path, "AAMk-archive", "annex.xlsx", b"only here")

    assert register_downloaded_attachments(db, tmp_path)["registered"] == 1
    assert register_downloaded_attachments(db, tmp_path)["registered"] == 0
    assert sorted(r[2] for r in _rows(db)) == ["annex.xlsx", "report.pdf"]


@pytest.mark.parametrize("alias", ["AAMk-0archive", "AAMk-zarchive"])
def test_two_new_directories_of_one_message_register_it_once(db, tmp_path, alias):
    """Whichever directory the pass meets first registers the file."""
    email_id = _email(db, "AAMk-inbox")
    _alias(db, alias, email_id)
    _file(tmp_path, "AAMk-inbox", "report.pdf")
    _file(tmp_path, alias, "report.pdf")

    assert register_downloaded_attachments(db, tmp_path)["registered"] == 1
    assert len(_rows(db)) == 1


def test_the_hourly_window_applies_to_a_copy_as_to_its_email(db, tmp_path):
    email_id = _email(db, "AAMk-inbox", received="2026-01-01T10:00:00Z")
    _alias(db, "AAMk-archive", email_id)
    _file(tmp_path, "AAMk-archive", "report.pdf")

    hourly = register_downloaded_attachments(db, tmp_path, since="2026-09-30T00:00:00Z")
    assert hourly["registered"] == 0 and hourly["deferred"] == 1

    assert register_downloaded_attachments(db, tmp_path)["registered"] == 1
