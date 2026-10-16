"""An action can be marked done (schema v35): status 'done', with what closed it and when.

Before, only age closed an action ('expired'), so "open" meant "extracted in about the last
90 days": 0 of 202,363 actions on the replica of 2026-10-16 had ever been completed. A done
action is never deduplicated or expired, and never reopened by them.
"""

import pytest

from src.store.action_lifecycle import (
    complete_action,
    dedup_exact_open_actions,
    expire_stale_actions,
    expire_undated_actions,
)
from src.store.schema import create_database

OLD = "2020-01-01T10:00:00"


@pytest.fixture
def conn(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    conn.execute("INSERT INTO emails (id, message_id, date_received) VALUES (1, 'm1', ?)", (OLD,))
    yield conn
    conn.close()


def _add(conn, task="Send the deck", deadline=None, status="open"):
    return conn.execute(
        "INSERT INTO action_items (email_id, task, owner, deadline, status)"
        " VALUES (1, ?, 'Ann', ?, ?)",
        (task, deadline, status),
    ).lastrowid


def _state(conn, action_id):
    return tuple(
        conn.execute(
            "SELECT status, closed_by, closed_at FROM action_items WHERE id = ?", (action_id,)
        ).fetchone()
    )


def test_an_open_action_is_marked_done_with_what_closed_it_and_when(conn):
    action_id = _add(conn)

    assert complete_action(conn, action_id, "email:42", "2026-10-16T09:00:00+00:00") is True

    assert _state(conn, action_id) == ("done", "email:42", "2026-10-16T09:00:00+00:00")


def test_the_time_defaults_to_now(conn):
    action_id = _add(conn)

    complete_action(conn, action_id, "owner")

    status, closed_by, closed_at = _state(conn, action_id)
    assert (status, closed_by) == ("done", "owner")
    assert closed_at and closed_at[:4].isdigit()


def test_an_expired_action_can_be_marked_done(conn):
    action_id = _add(conn, status="expired")

    assert complete_action(conn, action_id, "teams_thread:7") is True
    assert _state(conn, action_id)[0] == "done"


def test_a_done_action_is_not_closed_again(conn):
    action_id = _add(conn)
    complete_action(conn, action_id, "email:1", "2026-10-01T00:00:00+00:00")

    assert complete_action(conn, action_id, "email:2") is False
    assert _state(conn, action_id) == ("done", "email:1", "2026-10-01T00:00:00+00:00")


@pytest.mark.parametrize("closed_by", ["", "  ", None])
def test_what_closed_it_must_be_said(conn, closed_by):
    action_id = _add(conn)

    with pytest.raises(ValueError, match="closed_by"):
        complete_action(conn, action_id, closed_by)


def test_a_done_action_is_left_alone_by_the_dedup_and_the_age_out(conn):
    done = _add(conn, deadline="2020-01-01")
    twin = _add(conn, deadline="2020-01-01")
    undated = _add(conn, task="Book a room")
    complete_action(conn, done, "owner")
    complete_action(conn, undated, "owner")

    dedup_exact_open_actions(conn)
    expire_stale_actions(conn, days=1)
    expire_undated_actions(conn, days=1)

    assert _state(conn, done)[0] == "done"
    assert _state(conn, undated)[0] == "done"
    assert _state(conn, twin)[0] == "expired"
