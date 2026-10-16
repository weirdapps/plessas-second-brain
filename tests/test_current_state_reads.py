"""The reads return current items unless asked for history, and say what they left out.

A consolidation pass (src/store/consolidate.py) marks an item a duplicate, superseded or
invalid; person_context, topic_context, query_decisions, query_actions, the overdue half of
stale_threads and meeting_prep leave those out by default. Every value is invented.
"""

import pytest

from src.store import query
from src.store.context import get_person_context, get_topic_context
from src.store.schema import create_database
from tests import factories as f

ALICE = ("Alice Smith", "alice@example.com")
BOB = ("Bob Jones", "bob@example.com")


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    first = f.email(
        conn,
        subject="Okapi plan",
        sender=ALICE,
        to=(BOB,),
        topics=("okapi",),
        conversation_id="c1",
        date_received=f.ago(days=3),
    )
    second = f.email(
        conn,
        subject="Re: Okapi plan",
        sender=BOB,
        to=(ALICE,),
        topics=("okapi",),
        conversation_id="c1",
        date_received=f.ago(days=2),
    )
    ids = {
        "keep": f.decision(conn, "Adopt the okapi plan", email_id=first),
        "dup": f.decision(conn, "Adopt the okapi plan", email_id=second),
        "open": f.action_item(
            conn, "Send the okapi deck", email_id=first, deadline=f.ago(days=1)[:10]
        ),
        "copy": f.action_item(
            conn, "Send the okapi deck", email_id=second, deadline=f.ago(days=1)[:10]
        ),
        "fact_old": conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, 'okapi budget 5')", (first,)
        ).lastrowid,
        "fact_new": conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, 'okapi budget 7')", (second,)
        ).lastrowid,
    }
    conn.execute("UPDATE decisions SET canonical_id = ? WHERE id = ?", (ids["keep"], ids["dup"]))
    conn.execute(
        "UPDATE action_items SET canonical_id = ? WHERE id = ?", (ids["open"], ids["copy"])
    )
    conn.execute(
        "UPDATE key_facts SET superseded_by = ?, invalid_at = ? WHERE id = ?",
        (ids["fact_new"], f.ago(days=2), ids["fact_old"]),
    )
    conn.commit()
    yield conn, ids
    conn.close()


def test_person_context_lists_current_items_and_says_what_it_left_out(store):
    conn, ids = store

    out = get_person_context(conn, "alice@example.com")

    assert out["decisions_total"] == 1 and len(out["decisions"]) == 1
    assert out["open_actions_total"] == 1
    assert out["history_left_out"] == {
        "decisions": {"rows": 1, "switch": "include_history=true"},
        "open_actions": {"rows": 1, "switch": "include_history=true"},
    }
    everything = get_person_context(conn, "alice@example.com", include_history=True)
    assert everything["decisions_total"] == 2 and "history_left_out" not in everything


def test_topic_context_hides_a_superseded_fact(store):
    conn, ids = store

    out = get_topic_context(conn, "okapi")

    assert [r["fact"] for r in out["key_facts"]] == ["okapi budget 7"]
    assert out["history_left_out"]["key_facts"]["rows"] == 1
    assert out["topic"]["family_total"] == 1
    assert len(get_topic_context(conn, "okapi", include_history=True)["key_facts"]) == 2


def test_query_decisions_and_actions_leave_history_out(store):
    conn, ids = store

    decisions = query.query_decisions(conn, topic="okapi")
    actions = query.query_action_items(conn)

    assert [r["decision_id"] for r in decisions] == [ids["keep"]]
    assert [r["action_id"] for r in actions] == [ids["open"]]
    assert len(query.query_decisions(conn, include_history=True)) == 2
    assert len(query.query_action_items(conn, include_history=True)) == 2


def test_overdue_actions_leave_a_duplicate_out(store):
    conn, ids = store
    conn.execute("UPDATE action_items SET deadline = ?", (f.ago(days=5)[:10],))

    assert [r["action_id"] for r in query.find_overdue_actions(conn)] == [ids["open"]]
    assert query.count_overdue_actions(conn) == 1
    assert query.count_overdue_actions(conn, include_history=True) == 2


def test_meeting_prep_counts_what_each_dossier_left_out(store):
    conn, ids = store

    prep = query.meeting_prep(conn, ["alice@example.com"], topic="okapi")

    (dossier,) = prep["attendees"]
    assert len(dossier["decisions"]) == 1
    assert dossier["history_left_out"]["decisions"]["rows"] == 1
    assert [r["fact"] for r in prep["topic_context"]["key_facts"]] == ["okapi budget 7"]
    full = query.meeting_prep(conn, ["alice@example.com"], include_history=True)
    assert len(full["attendees"][0]["decisions"]) == 2


def test_a_done_action_is_current_but_not_open(store):
    conn, ids = store
    from src.store.action_lifecycle import complete_action

    complete_action(conn, ids["open"], "email:1", "2026-10-01T00:00:00+00:00")
    conn.commit()

    assert query.query_action_items(conn) == []
    done = query.query_action_items(conn, status="done")
    assert [r["action_id"] for r in done] == [ids["open"]]


def test_a_store_before_v35_reads_every_item(tmp_path):
    """A replica's file from before the pull has no state columns: nothing is filtered."""
    from src.store.schema import ITEM_STATE_COLUMNS

    conn = create_database(str(tmp_path / "old.db"))
    email_id = f.email(conn, subject="Okapi plan", sender=ALICE, to=(BOB,))
    f.decision(conn, "Adopt the okapi plan", email_id=email_id)
    for (index,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND (name GLOB 'idx_*_state_canonical' OR name GLOB 'idx_*_state_superseded')"
    ).fetchall():
        conn.execute(f"DROP INDEX {index}")
    for table, columns in ITEM_STATE_COLUMNS.items():
        for name, _decl in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    conn.commit()

    assert len(query.query_decisions(conn)) == 1
    out = get_person_context(conn, "alice@example.com")
    assert out["decisions_total"] == 1 and "history_left_out" not in out
