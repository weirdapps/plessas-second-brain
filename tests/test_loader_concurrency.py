"""Two loaders of one item never collide.

The sync units overlap: on 2026-09-24 the noon catch-up started at 13:17:51 while
the hourly sync that began at 13:14 was still loading. Each loader checked that an
item was not stored, the other stored it, and the insert failed on its unique key,
which ended that sync: eight times for emails since July, once for a conversation.
A loader now takes the write lock before its first check, so the second waits for
the first and then finds the item stored.
"""

import json
import sqlite3

from src.store.loader import load_extractions, load_single_conversation, load_single_email
from src.store.schema import create_database, get_connection

EMAIL = {
    "message_id": "m1",
    "date_received": "2026-09-01T00:00:00Z",
    "subject": "s",
    "sender": {"name": "A", "address": "a@example.com"},
    "mailbox_name": "Inbox",
    "content": "hello",
}


def _racing(conn, db, check, store):
    """conn, with another writer storing the same item right after conn's check."""
    other = sqlite3.connect(str(db), timeout=0)
    blocked: list[bool] = []

    class Racing:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            result = conn.execute(sql, *args)
            if check in sql and not blocked:
                try:
                    other.execute(store)
                    other.commit()
                    blocked.append(False)
                except sqlite3.OperationalError:  # locked: the loader holds the write lock
                    blocked.append(True)
            return result

    return Racing(), blocked


def test_two_loaders_of_one_email_never_collide(tmp_path):
    db = tmp_path / "b.db"
    create_database(str(db)).close()
    conn = get_connection(str(db))
    store = (
        "INSERT INTO emails (message_id, date_received, subject, content) "
        "VALUES ('m1', '2026-09-01', 's', 'x')"
    )
    racing, blocked = _racing(conn, db, "FROM emails WHERE message_id", store)

    assert load_single_email(racing, EMAIL, {"summary": "s"})
    conn.commit()

    assert blocked == [True]
    assert conn.execute("SELECT count(*) FROM emails").fetchone()[0] == 1


def test_two_loaders_of_one_conversation_never_collide(tmp_path):
    db = tmp_path / "b.db"
    create_database(str(db)).close()
    conn = get_connection(str(db))
    meta = {
        "session_id": "s",
        "started_at": "2026-09-01T09:00:00Z",
        "ended_at": "2026-09-01T10:00:00Z",
        "turn_count": 1,
        "turns": [{"speaker": "user", "content": "a", "timestamp": "2026-09-01T10:00:00Z"}],
    }
    store = "INSERT INTO conversations (session_id, started_at, created_at) VALUES ('s', '', '')"
    racing, blocked = _racing(conn, db, "FROM conversations WHERE session_id", store)

    assert load_single_conversation(racing, meta, {"summary": "x"})
    conn.commit()

    assert blocked == [True]
    assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1


def test_a_run_that_loads_nothing_new_keeps_the_mailbox_moves_it_found(tmp_path):
    """The loader committed only when it loaded an email, so a run whose emails
    were all stored already lost the moves it recorded (Inbox to Archive) when the
    connection closed."""
    db = tmp_path / "b.db"
    conn = create_database(str(db))
    assert load_single_email(conn, EMAIL, {"summary": "s"})
    conn.commit()
    conn.close()
    staging, extracted = tmp_path / "staging", tmp_path / "extracted"
    staging.mkdir()
    extracted.mkdir()
    moved = {**EMAIL, "mailbox_name": "Archive"}
    (staging / "batch-00001.json").write_text(json.dumps({"emails": [moved]}))
    (extracted / "m1.json").write_text(json.dumps({"summary": "s"}))

    assert load_extractions(str(db), str(extracted), str(staging)) == 0

    conn = get_connection(str(db))
    assert conn.execute("SELECT mailbox_name FROM emails").fetchone()[0] == "Archive"
