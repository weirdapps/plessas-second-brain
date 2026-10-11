"""stats.coverage says how completely each month of the calendar is held.

It gave the calendar's first and last date only, and agents are told to check it
before concluding that something did not happen. The first load kept one page of
ten events per month, so four months of 2025 held ten events each, against more
than two hundred in a full month, and read as covered (data-coverage-02).
"""

from datetime import date

import pytest

from src.store.calendar_coverage import density, mark_month_started, record_listed_month
from src.store.query import get_coverage
from src.store.schema import create_database


@pytest.fixture
def conn():
    conn = create_database(":memory:")
    yield conn
    conn.close()


def _hold(conn, month: str, n: int, *, cancelled: int = 0) -> None:
    """n events in `month` (YYYY-MM), on its first days, stored as Graph sends them."""
    conn.executemany(
        "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at, "
        "is_cancelled, ingested_at) VALUES (?, 'Meeting', ?, ?, ?, '2026-01-01')",
        [
            (
                f"{month}-{cancelled}-{k}",
                f"{month}-{1 + k % 28:02d}T09:00:00.0000000Z",
                f"{month}-{1 + k % 28:02d}T10:00:00.0000000Z",
                cancelled,
            )
            for k in range(n)
        ],
    )
    conn.commit()


def _calendar(conn, counts: dict[str, int]) -> None:
    for month, n in counts.items():
        _hold(conn, month, n)


_YEAR = {
    "2025-05": 10,
    "2025-06": 10,
    "2025-07": 60,
    "2025-08": 60,
    "2025-09": 12,
    "2025-10": 60,
    "2025-11": 60,
    "2025-12": 60,
}


def test_the_months_the_first_load_capped_are_flagged(conn):
    _calendar(conn, _YEAR)

    held = density(conn, today=date(2026, 1, 10))

    assert held["per_month"] == _YEAR
    assert held["expected_per_month"] == 60
    assert held["incomplete"] == {"2025-05": "capped", "2025-06": "capped", "2025-09": "sparse"}
    assert held["complete_from"] == "2025-10"
    assert held["note"]


def test_a_quiet_month_is_not_taken_for_a_gap(conn):
    """August at half a normal month is a holiday, not a hole."""
    _calendar(conn, {**_YEAR, "2025-08": 30})

    assert "2025-08" not in density(conn, today=date(2026, 1, 10))["incomplete"]


def test_a_month_a_backfill_listed_completely_is_not_flagged(conn):
    _calendar(conn, _YEAR)
    record_listed_month(conn, "2025-09", "2026-01-09T10:00:00+00:00")

    held = density(conn, today=date(2026, 1, 10))

    assert held["incomplete"] == {"2025-05": "capped", "2025-06": "capped"}
    assert held["complete_from"] == "2025-07"


def test_a_month_a_backfill_began_and_did_not_finish_is_partial_until_one_does(conn):
    """Half filled when its run stopped, it can hold enough events to pass."""
    _calendar(conn, {**_YEAR, "2025-06": 40})
    mark_month_started(conn, "2025-06", "2026-01-09T10:00:00+00:00")

    assert density(conn, today=date(2026, 1, 10))["incomplete"]["2025-06"] == "partial"

    record_listed_month(conn, "2025-06", "2026-01-09T11:00:00+00:00")

    assert "2025-06" not in density(conn, today=date(2026, 1, 10))["incomplete"]


def test_a_month_holding_nothing_between_two_held_is_reported_and_flagged(conn):
    _calendar(conn, {k: v for k, v in _YEAR.items() if k != "2025-10"})

    held = density(conn, today=date(2026, 1, 10))

    assert held["per_month"]["2025-10"] == 0
    assert held["incomplete"]["2025-10"] == "sparse"
    assert held["complete_from"] == "2025-11"


def test_the_current_month_and_those_ahead_are_never_flagged(conn):
    """They are still being listed: the sync reaches 30 days ahead."""
    _calendar(conn, {**_YEAR, "2026-01": 3, "2026-02": 1})

    held = density(conn, today=date(2026, 1, 10))

    assert held["per_month"]["2026-02"] == 1
    assert not {"2026-01", "2026-02"} & set(held["incomplete"])


def test_with_too_few_full_months_nothing_is_flagged(conn):
    _calendar(conn, {"2025-11": 10, "2025-12": 60})

    held = density(conn, today=date(2026, 1, 10))

    assert held["expected_per_month"] is None
    assert held["incomplete"] == {}
    assert "note" not in held


def test_cancelled_meetings_do_not_count(conn):
    _calendar(conn, _YEAR)
    _hold(conn, "2025-05", 50, cancelled=1)

    held = density(conn, today=date(2026, 1, 10))

    assert held["per_month"]["2025-05"] == 10
    assert held["incomplete"]["2025-05"] == "capped"


def test_an_empty_calendar_has_no_months(conn):
    held = density(conn, today=date(2026, 1, 10))

    assert held == {
        "per_month": {},
        "expected_per_month": None,
        "incomplete": {},
        "complete_from": None,
    }


def test_coverage_carries_the_density_beside_the_span(conn):
    _calendar(conn, _YEAR)

    calendar = get_coverage(conn)["calendar"]

    assert calendar["first"].startswith("2025-05-01")
    assert calendar["items"] == sum(_YEAR.values())
    assert calendar["per_month"] == _YEAR
    assert set(calendar["incomplete"]) == {"2025-05", "2025-06", "2025-09"}
    assert calendar["complete_from"] == "2025-10"
    assert calendar["note"]


def test_a_complete_calendar_carries_no_note(conn):
    _calendar(conn, dict.fromkeys(_YEAR, 60))

    calendar = get_coverage(conn)["calendar"]

    assert calendar["incomplete"] == {}
    assert calendar["complete_from"] == "2025-05"
    assert "note" not in calendar
