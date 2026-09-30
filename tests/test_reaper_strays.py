"""Unregistered files inside a registered directory: stored duplicates go, the rest stay.

Nothing else owned them: the sweep takes registered files and the reaper whole unregistered
directories. On the producer all 232 found on 2026-09-30 were byte copies of stored content.
"""

import os
import time

import tests.test_orphan_reaper as tor
from src.store.file_hashes import sha256_of_file
from src.store.schema import create_database

reap = tor._REAPER.reap_orphan_attachments


def _store(tmp_path):
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES ('AAMk-1', ?, 's')", (now,)
    )
    folder = tmp_path / "att" / "AAMk-1"
    folder.mkdir(parents=True)
    kept = folder / "deck.pdf"
    kept.write_bytes(b"%PDF the stored deck")
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, sha256)"
        " VALUES (1, 'AAMk-1', 'deck.pdf', 'application/pdf', 20, ?, ?, ?)",
        (str(kept), now, sha256_of_file(kept)),
    )
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, llm_status)"
        " VALUES (1, 'deck', 'pdfplumber', 'extracted', ?, 'extracted')",
        (now,),
    )
    conn.commit()
    conn.close()
    return db, tmp_path / "att", folder, kept


def _stray(folder, name, data, days_old=2):
    f = folder / name
    f.write_bytes(data)
    t = time.time() - days_old * 86400
    os.utime(f, (t, t))
    return f


def test_a_stored_duplicate_beside_a_registered_file_is_deleted(tmp_path):
    db, root, folder, kept = _store(tmp_path)
    copy = _stray(folder, "tmpcopy.pdf", kept.read_bytes())

    stats = reap(db, root, apply=True)

    assert not copy.exists()
    assert kept.exists()
    assert stats["strays_deleted"] == 1


def test_a_unique_stray_is_kept_and_counted(tmp_path):
    db, root, folder, _kept = _store(tmp_path)
    unique = _stray(folder, "other.pdf", b"%PDF something else")

    stats = reap(db, root, apply=True)

    assert unique.exists()
    assert stats["strays_kept"] == 1


def test_a_fresh_duplicate_waits(tmp_path):
    db, root, folder, kept = _store(tmp_path)
    copy = _stray(folder, "tmpcopy.pdf", kept.read_bytes(), days_old=0)

    assert reap(db, root, apply=True)["strays_deleted"] == 0
    assert copy.exists()


def test_a_dry_run_counts_and_deletes_nothing(tmp_path):
    db, root, folder, kept = _store(tmp_path)
    copy = _stray(folder, "tmpcopy.pdf", kept.read_bytes())

    assert reap(db, root, apply=False)["strays_deleted"] == 1
    assert copy.exists()


def test_adopt_only_leaves_strays_alone(tmp_path):
    db, root, folder, kept = _store(tmp_path)
    copy = _stray(folder, "tmpcopy.pdf", kept.read_bytes())

    assert reap(db, root, apply=True, adopt_only=True)["strays_deleted"] == 0
    assert copy.exists()
