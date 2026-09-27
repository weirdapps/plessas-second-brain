"""recall's actions bucket shows what is outstanding first.

It sorted by deadline ascending, so the oldest dates, already expired, filled
the page, and a free-text deadline ('2026') sorted ahead of every real one. The
open and upcoming items never made it. query_action_items sorts upcoming, then
undated, then overdue, and the recall bucket now does the same, open first.
"""

import pytest

from src.store.recall import _search_actions
from src.store.schema import create_database


@pytest.fixture
def conn():
    c = create_database(":memory:")
    rows = [
        (1, "2024-01-01", "expired"),
        (2, "2025-01-01", "open"),
        (3, "2099-06-01", "open"),
        (4, None, "open"),
        (5, "2026", "open"),
        (6, "2099-01-01", "open"),
        (7, "2025-06-01T10:00:00", "open"),
        (8, "2099-02-01", "done"),
    ]
    c.executemany(
        "INSERT INTO action_items (id, task, deadline, status) "
        "VALUES (?, 'Prepare the okapi rollout plan', ?, ?)",
        rows,
    )
    c.commit()
    return c


def test_open_upcoming_items_come_first_then_undated_then_overdue_then_the_rest(conn):
    ids = [r["id"] for r in _search_actions(conn, "okapi rollout", 10)]

    assert ids[:2] == [6, 3]  # upcoming, soonest first
    assert set(ids[2:4]) == {4, 5}  # undated or free text
    assert ids[4:6] == [7, 2]  # overdue, most recently missed first
    assert set(ids[6:]) == {1, 8}  # not open


def test_a_short_page_holds_the_upcoming_items(conn):
    rows = _search_actions(conn, "okapi rollout", 2)

    assert [(r["deadline"], r["status"]) for r in rows] == [
        ("2099-01-01", "open"),
        ("2099-06-01", "open"),
    ]
