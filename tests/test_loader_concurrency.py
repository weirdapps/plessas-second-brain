"""Two loaders of one item never collide.

The sync units overlap: on 2026-09-24 the noon catch-up started at 13:17:51 while
the hourly sync that began at 13:14 was still loading. Each loader checked that an
item was not stored, the other stored it, and the insert failed on its unique key,
which ended that sync: eight times for emails since July, once for a conversation.
A loader now checks again under the write lock before it writes, and takes the
lock only then, so a pass over items already stored waits for no one.
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
CONVERSATION = {
    "session_id": "s",
    "started_at": "2026-09-01T09:00:00Z",
    "ended_at": "2026-09-01T10:00:00Z",
    "turn_count": 1,
    "turns": [{"speaker": "user", "content": "a", "timestamp": "2026-09-01T10:00:00Z"}],
}


def _racing(conn, db, check, store):
    """conn, with another loader storing the same item right after conn's first check."""
    other = sqlite3.connect(str(db), timeout=0)
    stored: list[bool] = []

    class Racing:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            result = conn.execute(sql, *args)
            if check in sql and not stored:
                other.execute(store)
                other.commit()
                stored.append(True)
            return result

    return Racing(), stored


def test_two_loaders_of_one_email_never_collide(tmp_path):
    db = tmp_path / "b.db"
    create_database(str(db)).close()
    conn = get_connection(str(db))
    store = (
        "INSERT INTO emails (message_id, date_received, subject, content) "
        "VALUES ('m1', '2026-09-01', 's', 'x')"
    )
    racing, stored = _racing(conn, db, "FROM emails WHERE message_id", store)

    assert not load_single_email(racing, EMAIL, {"summary": "s"})  # the other stored it
    conn.commit()

    assert stored == [True]
    assert conn.execute("SELECT count(*) FROM emails").fetchone()[0] == 1


def test_two_loaders_of_one_conversation_never_collide(tmp_path):
    db = tmp_path / "b.db"
    create_database(str(db)).close()
    conn = get_connection(str(db))
    store = "INSERT INTO conversations (session_id, started_at, created_at) VALUES ('s', '', '')"
    racing, stored = _racing(conn, db, "FROM conversations WHERE session_id", store)

    assert not load_single_conversation(racing, CONVERSATION, {"summary": "x"})
    conn.commit()

    assert stored == [True]
    assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1


def test_the_write_lock_is_held_from_the_check_before_the_insert(tmp_path):
    """Another loader trying to store the email after the check made under the
    lock waits: nothing can come between that check and the insert."""
    db = tmp_path / "b.db"
    create_database(str(db)).close()
    conn = get_connection(str(db))
    other = sqlite3.connect(str(db), timeout=0)
    checks: list[str] = []

    class Racing:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            result = conn.execute(sql, *args)
            if "FROM emails WHERE message_id" in sql:
                checks.append(sql)
                if len(checks) == 2:  # the check under the lock
                    try:
                        other.execute(
                            "INSERT INTO emails (message_id, date_received, subject, content) "
                            "VALUES ('m1', '2026-09-01', 's', 'x')"
                        )
                        checks.append("stored by the other")
                    except sqlite3.OperationalError:
                        checks.append("the other waited")
            return result

    assert load_single_email(Racing(), EMAIL, {"summary": "s"})
    conn.commit()

    assert checks[2] == "the other waited"


def test_what_a_staged_copy_moves():
    from src.store.loader import _moved

    assert _moved("Inbox", "Archive")
    assert _moved(None, "Inbox")  # a folder where none was stored
    assert not _moved("Archive", "Inbox")  # an old copy: the Inbox export takes new mail only
    assert not _moved("Archive", "Archive")
    assert not _moved("Inbox", None)


def test_an_item_already_stored_waits_for_no_writer(tmp_path):
    """A pass over stored items took the write lock at its first check, and failed
    Step 3 behind any writer holding it past busy_timeout (the attachments pass
    holds it across ten model replies)."""
    db = tmp_path / "b.db"
    conn = create_database(str(db))
    assert load_single_email(conn, EMAIL, {"summary": "s"})
    assert load_single_conversation(conn, CONVERSATION, {"summary": "x"})
    conn.commit()
    conn.close()
    writer = sqlite3.connect(str(db))
    writer.execute("BEGIN IMMEDIATE")
    loader = sqlite3.connect(str(db), timeout=0)

    assert not load_single_email(loader, EMAIL, {"summary": "s"})
    assert not load_single_conversation(loader, CONVERSATION, {"summary": "x"})
    assert not loader.in_transaction


def _stage(tmp_path, email):
    staging, extracted = tmp_path / "staging", tmp_path / "extracted"
    staging.mkdir(exist_ok=True)
    extracted.mkdir(exist_ok=True)
    (staging / "batch-00001.json").write_text(json.dumps({"emails": [email]}))
    (extracted / f"{email['message_id']}.json").write_text(json.dumps({"summary": "s"}))
    return str(extracted), str(staging)


def test_a_run_that_loads_nothing_new_keeps_the_mailbox_moves_it_found(tmp_path):
    """The loader committed only when it loaded an email, so a run whose emails
    were all stored already lost the moves it recorded (Inbox to Archive) when the
    connection closed."""
    db = tmp_path / "b.db"
    conn = create_database(str(db))
    assert load_single_email(conn, EMAIL, {"summary": "s"})
    conn.commit()
    conn.close()

    assert load_extractions(str(db), *_stage(tmp_path, {**EMAIL, "mailbox_name": "Archive"})) == 0

    conn = get_connection(str(db))
    assert conn.execute("SELECT mailbox_name FROM emails").fetchone()[0] == "Archive"


def test_a_staged_inbox_copy_never_moves_a_stored_email_back(tmp_path):
    """The Inbox export takes new arrivals only, so a staged Inbox copy of a stored
    email is an old one: a batch stays staged while any email in it is unextracted.
    Loaded every hour, it undid inbox_reconcile's move to Archive."""
    db = tmp_path / "b.db"
    conn = create_database(str(db))
    assert load_single_email(conn, {**EMAIL, "mailbox_name": "Archive"}, {"summary": "s"})
    conn.commit()
    conn.close()

    assert load_extractions(str(db), *_stage(tmp_path, EMAIL)) == 0

    conn = get_connection(str(db))
    assert conn.execute("SELECT mailbox_name FROM emails").fetchone()[0] == "Archive"
