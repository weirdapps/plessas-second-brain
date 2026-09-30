"""Attachments carry the hash of their bytes, so a deleted file can still be recognised.

The sweep deletes a file once its content is stored. From then on the only evidence of what
the file held is this hash: the orphan reaper matches re-downloads against it, and the image
rule matches inline_images on it.
"""

import hashlib
import sqlite3
from argparse import Namespace
from collections.abc import Generator
from pathlib import Path

import pytest

from src.export.outlook_attachments import register_downloaded_attachments
from src.store.file_hashes import hash_attachments
from src.store.schema import create_database


@pytest.fixture
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = create_database(":memory:")
    yield conn
    conn.close()


def _row(db, path: Path, sha: str | None = None) -> int:
    cur = db.execute(
        "INSERT INTO attachments (message_id, filename, file_path, exported_at, sha256)"
        " VALUES (?, ?, ?, ?, ?)",
        ("m1", path.name, str(path), "2026-09-30T00:00:00", sha),
    )
    db.commit()
    return cur.lastrowid


def test_the_registrar_records_the_hash_of_the_bytes(db, tmp_path):
    db.execute("INSERT INTO emails (message_id, date_received) VALUES ('AAMk-1', '2026-09-30')")
    db.commit()
    (tmp_path / "AAMk-1").mkdir()
    (tmp_path / "AAMk-1" / "a.pdf").write_bytes(b"x" * 512)

    register_downloaded_attachments(db, tmp_path)

    got = db.execute("SELECT sha256 FROM attachments").fetchone()[0]
    assert got == hashlib.sha256(b"x" * 512).hexdigest()


def test_the_backfill_hashes_rows_that_have_none(db, tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"hello")
    att = _row(db, f)

    stats = hash_attachments(db)

    assert stats == {"hashed": 1, "missing": 0}
    got = db.execute("SELECT sha256 FROM attachments WHERE id = ?", (att,)).fetchone()[0]
    assert got == hashlib.sha256(b"hello").hexdigest()


def test_the_backfill_resumes_where_it_stopped(db, tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).write_bytes(name.encode())
        _row(db, tmp_path / name)

    assert hash_attachments(db, limit=1)["hashed"] == 1
    assert hash_attachments(db)["hashed"] == 1
    assert hash_attachments(db)["hashed"] == 0


def test_a_row_whose_file_is_gone_stays_unhashed(db, tmp_path):
    _row(db, tmp_path / "gone.pdf")

    assert hash_attachments(db) == {"hashed": 0, "missing": 1}
    assert db.execute("SELECT sha256 FROM attachments").fetchone()[0] is None


def test_ingest_document_records_the_hash_and_keeps_its_id_scheme(tmp_path, monkeypatch):
    from src.extract.attachment_pipeline import ingest_document

    monkeypatch.setattr("src.extract.attachment_pipeline.ATTACHMENTS_DIR", tmp_path / "att")
    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()
    doc = tmp_path / "note.txt"
    doc.write_bytes(b"some text")

    out = ingest_document(str(doc), db_path=str(db_path))

    sha = hashlib.sha256(b"some text").hexdigest()
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT sha256, message_id FROM attachments").fetchone()
    conn.close()
    assert row[0] == sha
    assert row[1] == out["message_id"] == -abs(int(sha[:15], 16))


def test_the_cli_reports_what_is_left(tmp_path, capsys):
    from src.cli import cmd_hash_attachments

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    _row(conn, tmp_path / "gone.pdf")
    conn.close()

    cmd_hash_attachments(Namespace(db=db_path, limit=0))

    out = capsys.readouterr().out
    assert "file missing: 1" in out
    assert "still unhashed: 1" in out


def test_the_backfill_finds_a_file_recorded_under_another_hosts_path(db, tmp_path):
    """Rows from the retired Mac exporter carry Mac paths while the file sits in this tree."""
    (tmp_path / "AAMk-1").mkdir()
    (tmp_path / "AAMk-1" / "a.pdf").write_bytes(b"moved")
    _row(db, Path("/Users/someone/Mail/AAMk-1/a.pdf"))

    stats = hash_attachments(db, root=tmp_path)

    assert stats == {"hashed": 1, "missing": 0}
    got = db.execute("SELECT sha256 FROM attachments").fetchone()[0]
    assert got == hashlib.sha256(b"moved").hexdigest()


def test_ingest_document_records_an_image_as_an_image(tmp_path, monkeypatch):
    """An image recorded as octet-stream never reaches the vision pass."""
    from src.extract.attachment_pipeline import ingest_document

    monkeypatch.setattr("src.extract.attachment_pipeline.ATTACHMENTS_DIR", tmp_path / "att")
    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()
    img = tmp_path / "chart.PNG"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    ingest_document(str(img), db_path=str(db_path))

    conn = sqlite3.connect(db_path)
    mime = conn.execute("SELECT mime_type FROM attachments").fetchone()[0]
    conn.close()
    assert mime == "image/png"
