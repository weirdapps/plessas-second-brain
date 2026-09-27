"""An overdue action has an ISO date in the past, nothing else.

_OVERDUE_WHERE accepted any deadline julianday() could read, and it reads a bare
year ('2026') as Julian day 2026 and '10:00' as today, so those counted as
overdue, with days_overdue near 2.46 million.
"""

import pytest

from src.store.query import count_overdue_actions, find_overdue_actions
from src.store.schema import create_database


@pytest.fixture
def conn():
    c = create_database(":memory:")
    c.executemany(
        "INSERT INTO action_items (id, task, deadline, status) VALUES (?, 'Send the plan', ?, 'open')",
        [
            (1, "2026"),
            (2, "10:00"),
            (3, "2020-01-01"),
            (4, "2020-01-02T09:30:00"),
            (5, "2099-01-01"),
        ],
    )
    c.commit()
    return c


def test_only_iso_dates_in_the_past_are_overdue(conn):
    rows = find_overdue_actions(conn, limit=10)

    assert [r["action_id"] for r in rows] == [4, 3]
    assert count_overdue_actions(conn) == 2


def test_days_overdue_reads_the_date_part(conn):
    rows = {r["action_id"]: r["days_overdue"] for r in find_overdue_actions(conn, limit=10)}

    assert 0 < rows[4] < rows[3]
    assert all(days < 100_000 for days in rows.values())
