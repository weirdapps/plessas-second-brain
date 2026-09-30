"""Slow work never holds the store's write lock.

Phase 1 extracts, Phase 2 waits on the model and reextract re-reads files, each for seconds to
minutes per item now that zips and long texts are read in full. A write transaction left open
across that work blocked every other writer (the hourly sync, the Teams and WhatsApp syncs)
until SQLite gave up with "database is locked". Each item is committed on its own.
"""

import sqlite3

from src.extract import attachment_pipeline
from src.extract import reextract as rx
from src.extract.attachment_pipeline import run_phase1, run_phase2
from src.store.schema import create_database

WORDS = "enough words to pass the noise filter in this small test document, and a few more. "


def _db(tmp_path, with_content=False, text=WORDS):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 's')"
    )
    for i in range(2):
        f = tmp_path / "att" / f"d{i}" / f"f{i}.txt"
        f.parent.mkdir(parents=True)
        f.write_text(WORDS)
        conn.execute(
            "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
            " file_path, exported_at) VALUES (1, 7, ?, 'text/plain', 1, ?, '2026-09-01')",
            (f.name, str(f)),
        )
        if with_content:
            conn.execute(
                "INSERT INTO attachment_content (attachment_id, extracted_text,"
                " extraction_method, extraction_status, extracted_at, llm_status)"
                " VALUES (?, ?, 'direct_read', 'extracted', '2026-09-01', 'pending')",
                (i + 1, text),
            )
    conn.commit()
    conn.close()
    return path


def _write_meanwhile(path):
    """What another unit does in the middle of the run. No wait: a held lock fails at once."""
    other = sqlite3.connect(path, timeout=0)
    try:
        other.execute("INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('probe', 'x')")
        other.commit()
    finally:
        other.close()


def _read(path, calls):
    def extract(file_path, mime, *a, **k):
        calls.append(file_path)
        if len(calls) == 2:
            _write_meanwhile(path)
        return {"text": WORDS * 3000, "method": "direct_read", "status": "extracted", "error": None}

    return extract


def test_phase1_commits_each_row(tmp_path, monkeypatch):
    path = _db(tmp_path)
    monkeypatch.setattr(attachment_pipeline, "extract_text_from_file", _read(path, []))

    assert run_phase1(str(path))["processed"] == 2


def test_phase2_commits_each_result(tmp_path, monkeypatch):
    path = _db(tmp_path, with_content=True)
    calls: list = []

    def worker(row, *a, **k):
        calls.append(row)
        if len(calls) == 2:
            _write_meanwhile(path)
        return (row[0], row[5], {"summary": "s", "language": "en"}, None, False)

    monkeypatch.setattr(attachment_pipeline, "_extract_one_attachment", worker)

    assert run_phase2(str(path))["extracted"] == 2


def test_reextract_commits_each_row(tmp_path, monkeypatch):
    path = _db(tmp_path, with_content=True, text="x" * 100_000)
    monkeypatch.setattr(rx, "extract_text_from_file", _read(path, []))
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], {"summary": "s"}, None, False),
    )
    monkeypatch.setattr("src.store.embeddings.remove_vectors", lambda ids: 0)

    assert rx.reextract(str(path), {"capped"}, root=str(tmp_path / "att"))["reread"] == 2
