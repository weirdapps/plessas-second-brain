"""The loader notes the Graph id of every copy it drops as a duplicate.

A message moved to Archive gets a new Graph id, and the Archive export stages it
under that id. The loader kept the email it held (same RFC822 Message-ID) and
dropped the copy without noting the id, so the attachment directory downloaded
under it never matched an email. Extraction now skips such a copy outright
(src/extract/local.py), so the load notes it from the staged batch alone.
"""

import json
import sqlite3

from src.store.loader import load_extractions, load_single_email
from src.store.schema import create_database, get_connection

ORIGINAL = {
    "message_id": "AAMk-inbox-1",
    "internet_message_id": "<one@example.com>",
    "date_received": "2026-05-04T08:00:00Z",
    "subject": "quarterly figures",
    "sender": {"name": "A", "address": "a@example.com"},
    "mailbox_name": "Inbox",
    "content": "hello",
}
COPY = {**ORIGINAL, "message_id": "AAMk-archive-1", "mailbox_name": "Archive"}


def _aliases(conn):
    return conn.execute("SELECT message_id, email_id FROM email_aliases").fetchall()


def _stage(tmp_path, emails, extracted_ids=()):
    staging, extracted = tmp_path / "staging", tmp_path / "extracted"
    staging.mkdir(exist_ok=True)
    extracted.mkdir(exist_ok=True)
    (staging / "batch-50001.json").write_text(json.dumps({"emails": emails}))
    for message_id in extracted_ids:
        (extracted / f"{message_id}.json").write_text(json.dumps({"summary": "s"}))
    return str(extracted), str(staging)


def test_a_copy_dropped_by_its_rfc822_id_is_recorded_as_an_alias(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    assert load_single_email(conn, ORIGINAL, {"summary": "s"})
    email_id = conn.execute("SELECT id FROM emails").fetchone()[0]

    assert not load_single_email(conn, COPY, {"summary": "s"})

    assert [tuple(r) for r in _aliases(conn)] == [("AAMk-archive-1", email_id)]
    assert conn.execute("SELECT mailbox_name FROM emails").fetchone()[0] == "Archive"
    assert conn.execute("SELECT count(*) FROM emails").fetchone()[0] == 1


def test_an_email_is_never_its_own_alias(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    assert load_single_email(conn, ORIGINAL, {"summary": "s"})

    assert not load_single_email(conn, ORIGINAL, {"summary": "s"})

    assert _aliases(conn) == []


def test_a_known_alias_is_not_written_again(tmp_path):
    """An item already stored waits for no writer (test_loader_concurrency), and
    a copy whose alias is on record is such an item."""
    db = tmp_path / "b.db"
    conn = create_database(str(db))
    assert load_single_email(conn, ORIGINAL, {"summary": "s"})
    assert not load_single_email(conn, COPY, {"summary": "s"})
    conn.commit()
    conn.close()
    writer = sqlite3.connect(str(db))
    writer.execute("BEGIN IMMEDIATE")
    loader = sqlite3.connect(str(db), timeout=0)

    assert not load_single_email(loader, COPY, {"summary": "s"})
    assert not loader.in_transaction


def test_a_copy_extraction_skipped_is_resolved_by_the_load(tmp_path):
    db = tmp_path / "b.db"
    conn = create_database(str(db))
    assert load_single_email(conn, ORIGINAL, {"summary": "s"})
    conn.commit()
    conn.close()
    extracted, staging = _stage(tmp_path, [COPY])  # no extraction for the copy

    assert load_extractions(str(db), extracted, staging) == 0

    conn = get_connection(str(db))
    assert [r[0] for r in _aliases(conn)] == ["AAMk-archive-1"]
    assert conn.execute("SELECT mailbox_name FROM emails").fetchone()[0] == "Archive"
    assert not list((tmp_path / "staging").glob("batch-*.json")), "the batch drains"


def test_a_copy_staged_before_its_original_finds_it_in_the_same_load(tmp_path):
    db = tmp_path / "b.db"
    create_database(str(db)).close()
    extracted, staging = _stage(tmp_path, [COPY, ORIGINAL], extracted_ids=["AAMk-inbox-1"])

    assert load_extractions(str(db), extracted, staging) == 1

    conn = get_connection(str(db))
    assert [r[0] for r in _aliases(conn)] == ["AAMk-archive-1"]
    assert conn.execute("SELECT count(*) FROM emails").fetchone()[0] == 1


def test_a_new_email_waiting_for_its_extraction_is_left_alone(tmp_path):
    db = tmp_path / "b.db"
    create_database(str(db)).close()
    extracted, staging = _stage(tmp_path, [ORIGINAL])

    assert load_extractions(str(db), extracted, staging) == 0

    conn = get_connection(str(db))
    assert conn.execute("SELECT count(*) FROM emails").fetchone()[0] == 0
    assert _aliases(conn) == []
    assert list((tmp_path / "staging").glob("batch-*.json")), "kept until it loads"
