"""An Archive copy of a stored email, end to end: never extracted, noted, its files claimed.

The three halves live in three modules (extraction skips the copy, the load notes
its alias, the registrar resolves its attachment directory through the alias), and
each assumes the others: a copy extraction skips but the load does not resolve
would sit staged for ever.
"""

import json

import pytest

from src.export.outlook_attachments import register_downloaded_attachments
from src.extract import local
from src.store.loader import load_extractions, load_single_email
from src.store.schema import create_database, get_connection

ORIGINAL = {
    "message_id": "AAMk-inbox-7",
    "internet_message_id": "<seven@example.com>",
    "date_received": "2026-10-05T08:00:00Z",
    "subject": "Board pack",
    "sender": {"name": "A", "address": "a@example.com"},
    "mailbox_name": "Inbox",
    "content": "attached",
}
COPY = {**ORIGINAL, "message_id": "AAMk-archive-7", "mailbox_name": "Archive"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    assert load_single_email(conn, ORIGINAL, {"summary": "the board pack"})
    conn.commit()
    conn.close()
    staging, extracted, attachments = (tmp_path / d for d in ("staging", "extracted", "att"))
    staging.mkdir()
    (staging / "batch-50001.json").write_text(json.dumps({"emails": [COPY]}))
    for message_id, name, content in (
        ("AAMk-inbox-7", "pack.pdf", b"pack"),
        ("AAMk-archive-7", "pack.pdf", b"pack"),
        ("AAMk-archive-7", "minutes.docx", b"only in the copy's download"),
    ):
        (attachments / message_id).mkdir(parents=True, exist_ok=True)
        (attachments / message_id / name).write_bytes(content)
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(local, "STAGING_DIR", staging)
    asked: list[str] = []
    monkeypatch.setattr(
        local,
        "extract_inline",
        lambda email, key, engine="claude": (
            asked.append(email["message_id"])
            or (email["message_id"], {"summary": "s"}, False, None)
        ),
    )
    return db, staging, extracted, attachments, asked


def test_an_archive_copy_costs_no_model_call_and_its_files_join_the_email(world):
    db, staging, extracted, attachments, asked = world
    conn = get_connection(str(db))
    register_downloaded_attachments(conn, attachments)  # the original's own files
    conn.close()

    local.run_extraction(workers=1, deadline_s=600.0, db_path=db)
    load_extractions(str(db), str(extracted), str(staging))
    conn = get_connection(str(db))
    stats = register_downloaded_attachments(conn, attachments)

    assert asked == []
    email_id, mailbox = conn.execute("SELECT id, mailbox_name FROM emails").fetchone()
    assert mailbox == "Archive"
    assert conn.execute("SELECT count(*) FROM emails").fetchone()[0] == 1
    assert [tuple(r) for r in conn.execute("SELECT message_id, email_id FROM email_aliases")] == [
        ("AAMk-archive-7", email_id)
    ]
    rows = conn.execute(
        "SELECT filename, message_id, email_id FROM attachments ORDER BY filename"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("minutes.docx", "AAMk-inbox-7", email_id),
        ("pack.pdf", "AAMk-inbox-7", email_id),
    ]
    assert stats["registered"] == 1 and stats["deferred"] == stats["abandoned"] == 0
    assert not list(staging.glob("batch-*.json")), "the copy's batch drains"
    conn.close()
