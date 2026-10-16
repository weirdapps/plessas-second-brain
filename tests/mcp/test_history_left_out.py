"""The tools return current items and say what the current-state filter left out.

A consolidation pass marks a decision or an action a duplicate, superseded or done; the
tools leave those out unless include_history, and `history_left_out` counts them, so an
agent can tell "not there" from "left out". Every value is invented.
"""

import pytest

from src import mcp_coverage, mcp_server
from src.store.schema import get_connection
from tests import seeded_store

LEFT_ONE = {"rows": 1, "switch": "include_history=true"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The seeded store plus a copy of each of its okapi decisions and actions, marked a
    duplicate, and an overdue action marked a duplicate of another."""
    mcp_coverage.clear()
    path = seeded_store.build(tmp_path / "brain.db")
    conn = get_connection(str(path))
    decision = conn.execute("SELECT id, email_id, decision FROM decisions LIMIT 1").fetchone()
    copy = conn.execute(
        "INSERT INTO decisions (email_id, decision, decided_by, decision_date, canonical_id)"
        " VALUES (?, ?, 'Alice Smith', date('now', '-1 day'), ?)",
        (decision["email_id"], decision["decision"], decision["id"]),
    ).lastrowid
    action = conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status)"
        " VALUES (1, 'Send the okapi deck', 'Alice Smith', date('now', '-3 days'), 'open')"
    ).lastrowid
    conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status, canonical_id)"
        " VALUES (2, 'Send the okapi deck', 'Alice Smith', date('now', '-3 days'), 'open', ?)",
        (action,),
    )
    conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status, closed_by,"
        " closed_at) VALUES (1, 'Book the okapi room', 'Alice Smith', date('now', '+3 days'),"
        " 'done', 'email:2', date('now'))"
    )
    conn.commit()
    conn.close()
    seeded_store.serve(path, monkeypatch)
    return {"decision_copy": copy, "action": action}


def test_query_decisions_leaves_a_duplicate_out_and_counts_it(store):
    out = mcp_server.query_decisions()

    assert store["decision_copy"] not in [r["decision_id"] for r in out["result"]]
    assert out["history_left_out"] == LEFT_ONE
    full = mcp_server.query_decisions(include_history=True)
    assert store["decision_copy"] in [r["decision_id"] for r in full["result"]]
    assert "history_left_out" not in full


def test_query_actions_counts_a_duplicate_and_a_done_action_as_left_out(store):
    out = mcp_server.query_actions()

    assert out["history_left_out"] == {"rows": 2, "switch": "include_history=true"}
    full = mcp_server.query_actions(include_history=True)
    assert {r["status"] for r in full["result"]} >= {"open", "done"}
    done = mcp_server.query_actions(status="done")
    assert [r["task"] for r in done["result"]] == ["Book the okapi room"]


def test_stale_threads_counts_the_overdue_duplicate(store):
    out = mcp_server.stale_threads()

    assert out["history_left_out"] == {"overdue_actions": LEFT_ONE}
    full = mcp_server.stale_threads(include_history=True)
    assert full["overdue_actions_total"] == out["overdue_actions_total"] + 1


def test_person_and_topic_dossiers_say_what_they_left_out(store):
    person = mcp_server.person_context("alice@example.com")
    topic = mcp_server.topic_context("okapi")

    assert person["history_left_out"]["decisions"] == LEFT_ONE
    assert topic["history_left_out"]["decisions"] == LEFT_ONE
    assert "history_left_out" not in mcp_server.person_context(
        "alice@example.com", include_history=True
    )


def test_an_empty_list_says_its_matches_were_history(store, tmp_path):
    conn = get_connection(str(tmp_path / "brain.db"))
    conn.execute(
        "UPDATE decisions SET superseded_by = ? WHERE canonical_id IS NULL",
        (store["decision_copy"],),
    )
    conn.execute("UPDATE decisions SET canonical_id = NULL, invalid_at = date('now')")
    conn.commit()
    conn.close()

    out = mcp_server.query_decisions()

    assert out["result"] == []
    assert "include_history=true" in out["reason"]
