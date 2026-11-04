"""Thread rollups (issue 179): built by rule from the stored rows, refreshed when a thread
grows, written only on a store with the gated step thread_identity. Every value is invented.
"""

import json

from src.store import thread_rollups as tr
from src.store.loader import load_single_email
from src.store.schema import create_database, subject_to_conversation_id
from tests import factories as f

ALICE = ("Alice Smith", "alice@corp.example")
BOB = ("Bob Jones", "bob@corp.example")


def _outlook(n: int) -> str:
    """An invented Outlook ConversationId."""
    return f"outlook-thread-{n}"


def _row(db, cid):
    return db.execute("SELECT * FROM thread_rollups WHERE conversation_id = ?", (cid,)).fetchone()


def test_a_rollup_holds_the_thread_by_rule(tmp_path):
    db = create_database(str(tmp_path / "brain.db"), gated=True)
    cid = subject_to_conversation_id("Okapi plan")
    first = f.email(
        db, subject="Okapi plan", sender=ALICE, to=(BOB,), date_received="2025-01-01T08:00:00Z"
    )
    second = f.email(
        db,
        subject="RE: Okapi plan",
        sender=BOB,
        to=(ALICE,),
        summary="Bob agrees to the okapi plan",
        date_received="2025-01-02T08:00:00Z",
    )
    f.decision(db, "Adopt the okapi plan", email_id=second)
    hidden = f.decision(db, "Adopt the old plan", email_id=first)
    db.execute("UPDATE decisions SET superseded_by = 1 WHERE id = ?", (hidden,))
    f.action_item(db, "Send the okapi budget", email_id=second, owner="Bob Jones")
    f.action_item(db, "Book the room", email_id=first, owner="Alice Smith", status="done")

    assert tr.refresh(db, cid)

    row = _row(db, cid)
    assert row["emails"] == 2 and row["subject"] == "Okapi plan"
    assert row["first_at"] == "2025-01-01T08:00:00Z" and row["last_at"] == "2025-01-02T08:00:00Z"
    assert row["summary"] == "Bob agrees to the okapi plan" and row["method"] == "rule"
    assert {p["address"] for p in json.loads(row["participants"])} == {ALICE[1], BOB[1]}
    assert [d["decision"] for d in json.loads(row["decisions"])] == ["Adopt the okapi plan"]
    assert [q["text"] for q in json.loads(row["open_questions"])] == ["Send the okapi budget"]


def test_nothing_is_written_on_a_store_without_the_step(tmp_path):
    db = create_database(str(tmp_path / "brain.db"), gated=False)
    f.email(db, subject="Okapi plan")
    assert not tr.refresh(db, subject_to_conversation_id("Okapi plan"))
    assert tr.backfill(db) == 0
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "thread_rollups" not in tables


def test_news_and_blank_subjects_get_no_rollup(tmp_path):
    db = create_database(str(tmp_path / "brain.db"), gated=True)
    assert not tr.refresh(db, "news:monitor:2025-01-01")
    assert not tr.refresh(db, subject_to_conversation_id(""))
    assert not tr.refresh(db, "")


def test_the_loader_refreshes_the_thread_a_new_email_joins(tmp_path):
    db = create_database(str(tmp_path / "brain.db"), gated=True)
    mail = {
        "subject": "Okapi plan",
        "sender": {"name": "Alice", "address": "alice@corp.example"},
        "mailbox_name": "Inbox",
        "content": "hello",
        "conversation_id": _outlook(9),
    }
    load_single_email(
        db, {**mail, "message_id": "m1", "date_received": "2025-01-01T08:00:00Z"}, {"summary": "a"}
    )
    assert _row(db, _outlook(9))["emails"] == 1
    load_single_email(
        db, {**mail, "message_id": "m2", "date_received": "2025-01-02T08:00:00Z"}, {"summary": "b"}
    )
    row = _row(db, _outlook(9))
    assert row["emails"] == 2 and row["summary"] == "b"


def test_backfill_fills_the_newest_threads_first_and_skips_the_current(tmp_path):
    db = create_database(str(tmp_path / "brain.db"), gated=True)
    for n, day in enumerate(("01", "02", "03")):
        f.email(db, subject=f"Okapi {n}", date_received=f"2025-01-{day}T08:00:00Z")

    assert tr.backfill(db, limit=2) == 2
    held = {r[0] for r in db.execute("SELECT subject FROM thread_rollups")}
    assert held == {"Okapi 1", "Okapi 2"}
    assert tr.backfill(db, limit=10) == 1
    assert tr.backfill(db, limit=10) == 0
