"""Decisions, key facts and open actions: dated and ordered the same way in every list.

decision_date is NULL on 98% of decisions, and the dossiers sorted by it alone, so the order was
whatever the index yielded and months-old decisions led a person's dossier; key facts had no
order at all; open actions led with deadlines years past. Each row also came back with
`date: null`, so nothing told the caller. The rules live in src/store/ordering.py.
"""

from datetime import datetime, timedelta

import pytest

from src.store.context import get_conversation_context, get_person_context, get_topic_context
from src.store.normalizer import find_or_create_person, find_or_create_topic
from src.store.query import meeting_prep, query_decisions
from src.store.recall import recall
from src.store.schema import create_database


def _ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _day(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


OLD, MID, NEW = _ago(300), _ago(100), _ago(5)  # three emails, oldest to newest
OWN = _day(-50)  # a decision's own date, between MID and NEW


@pytest.fixture
def db(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    for email_id, date in ((1, OLD), (2, MID), (3, NEW)):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary, mailbox_name,"
            " conversation_id) VALUES (?, ?, ?, ?, 'a summary', 'Inbox', ?)",
            (email_id, email_id, date, f"Subject {email_id}", f"thread-{email_id}"),
        )
    person = find_or_create_person(conn, "Alice Example", "alice@example.com")
    topic = find_or_create_topic(conn, "Card Pricing")
    for email_id in (1, 2, 3):
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'sender')",
            (email_id, person),
        )
        conn.execute(
            "INSERT INTO email_topics (email_id, topic_id) VALUES (?, ?)", (email_id, topic)
        )
    # Inserted oldest-first and out of order, so neither rowid nor insertion order is the answer.
    conn.executemany(
        "INSERT INTO decisions (email_id, decision, decided_by, decision_date) VALUES (?, ?, ?, ?)",
        [
            (2, "free-text date", "Bob", "Q3 2026"),  # not a date: dated by its email
            (1, "undated old", "Bob", None),
            (3, "undated new", "Bob", None),
            (1, "own date", "Bob", OWN),  # its own date beats its old email's
        ],
    )
    conn.executemany(
        "INSERT INTO key_facts (email_id, fact) VALUES (?, ?)",
        [(2, "fact mid"), (1, "fact old"), (3, "fact new")],
    )
    conn.executemany(
        "INSERT INTO action_items (email_id, task, owner, deadline, status) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "ancient", "Bob", "2020-01-01", "open"),
            (1, "undated old", "Bob", None, "open"),
            (2, "missed lately", "Bob", _day(-3), "open"),
            (3, "undated new", "Bob", None, "open"),
            (2, "free text", "Bob", "1 day before", "open"),
            (3, "upcoming", "Bob", _day(10), "open"),
            (3, "expired", "Bob", _day(20), "expired"),
        ],
    )
    conn.commit()
    yield conn
    conn.close()


DECISIONS = ["undated new", "own date", "free-text date", "undated old"]
DECISION_DATES = {
    "undated new": (NEW, NEW),
    "own date": (OWN, OLD),
    "free-text date": (MID, MID),
    "undated old": (OLD, OLD),
}
FACTS = ["fact new", "fact mid", "fact old"]
FACT_DATES = {"fact new": NEW, "fact mid": MID, "fact old": OLD}
# upcoming; then undated and free text, newest email first; then overdue, latest miss first
ACTIONS = ["upcoming", "undated new", "free text", "undated old", "missed lately", "ancient"]
ACTION_DATES = {
    "upcoming": NEW,
    "undated new": NEW,
    "free text": MID,
    "undated old": OLD,
    "missed lately": MID,
    "ancient": OLD,
}


def _check_decisions(rows):
    assert [r["decision"] for r in rows] == DECISIONS
    for r in rows:
        assert (r["date"], r["parent_date"]) == DECISION_DATES[r["decision"]], r


def _check_facts(rows):
    assert [r["fact"] for r in rows] == FACTS
    for r in rows:
        assert r["date"] == r["parent_date"] == FACT_DATES[r["fact"]], r


def _check_actions(rows):
    assert [r["task"] for r in rows] == ACTIONS
    for r in rows:
        assert r["date"] == r["parent_date"] == ACTION_DATES[r["task"]], r


def test_the_person_dossier_orders_and_dates_its_rows(db):
    ctx = get_person_context(db, "Alice Example")
    _check_decisions(ctx["decisions"])
    _check_actions(ctx["open_actions"])


def test_the_topic_dossier_orders_and_dates_its_rows(db):
    ctx = get_topic_context(db, "card pricing")
    _check_decisions(ctx["decisions"])
    _check_facts(ctx["key_facts"])
    _check_actions(ctx["open_actions"])


def test_a_capped_dossier_keeps_the_newest(db):
    """The cap takes the head of the ordered list, so it is the newest that survive."""
    ctx = get_topic_context(db, "card pricing", limit=2)
    assert [r["decision"] for r in ctx["decisions"]] == DECISIONS[:2]
    assert [r["fact"] for r in ctx["key_facts"]] == FACTS[:2]
    assert [r["task"] for r in ctx["open_actions"]] == ACTIONS[:2]
    assert (ctx["decisions_total"], ctx["key_facts_total"], ctx["open_actions_total"]) == (4, 3, 6)


def test_meeting_prep_orders_and_dates_its_rows(db):
    prep = meeting_prep(db, ["Alice Example"], topic="card pricing")
    attendee = prep["attendees"][0]
    _check_decisions(attendee["decisions"])
    _check_actions(attendee["open_actions"])
    topic = prep["topic_context"]
    _check_decisions(topic["decisions"])
    _check_facts(topic["key_facts"])
    _check_actions(topic["action_items"])


def test_the_dossiers_recall_attaches_lead_with_the_newest(db):
    out = recall(db, "Alice Example")
    assert out["person_context"] is not None
    assert [r["decision"] for r in out["person_context"]["decisions"]] == DECISIONS
    assert [r["task"] for r in out["person_context"]["open_actions"]] == ACTIONS[:5]


def test_query_decisions_dates_each_row_and_returns_its_parent_date(db):
    rows = query_decisions(db, limit=10)
    _check_decisions(rows)


def test_a_thread_lists_its_decisions_and_actions_in_order(db):
    """A thread reads oldest first: its decisions by date, its actions as everywhere else."""
    db.execute("UPDATE emails SET conversation_id = 'one-thread'")
    ctx = get_conversation_context(db, 3)
    assert [r["decision"] for r in ctx["decisions"]] == DECISIONS[::-1]
    for r in ctx["decisions"]:
        assert (r["date"], r["parent_date"]) == DECISION_DATES[r["decision"]], r
    tasks = [r["task"] for r in ctx["action_items"]]
    assert [t for t in tasks if t != "expired"] == ACTIONS


def test_no_row_is_undated_when_its_parent_has_a_date(db):
    rows = (
        get_person_context(db, "Alice Example")["decisions"]
        + get_topic_context(db, "card pricing")["key_facts"]
        + get_topic_context(db, "card pricing")["open_actions"]
        + query_decisions(db, limit=10)
    )
    assert rows and all(r["date"] and r["parent_date"] for r in rows)
