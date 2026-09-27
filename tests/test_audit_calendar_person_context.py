"""person_context's calendar lines compare times, not a 'T' against a space."""

from datetime import UTC, datetime, timedelta

import pytest

from src.store.context import get_person_context
from src.store.schema import create_database


def _graph(moment: datetime) -> str:
    """A start as calendar_events holds it: Graph's UTC DateTime."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


@pytest.fixture
def conn():
    c = create_database(":memory:")
    c.execute("INSERT INTO people (id, name, email) VALUES (1, 'Dana Duarte', 'dana@example.com')")
    yield c
    c.close()


def _meeting(conn, event_id: int, subject: str, start: str) -> None:
    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "ingested_at) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00')",
        (event_id, f"ev{event_id}", subject, start, start),
    )
    conn.execute(
        "INSERT INTO event_attendees (event_id, person_id, email, name, response_status) "
        "VALUES (?, 1, 'dana@example.com', 'Dana Duarte', 'Accepted')",
        (event_id,),
    )
    conn.commit()


def test_a_meeting_earlier_today_is_last_met_not_next_meeting(conn):
    """teams-calendar-7 / db-integrity-2: datetime('now') renders a space where
    start_at has a 'T', which sorts above it, so every start on the current UTC
    day read as still to come, however many hours ago it was."""
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _meeting(conn, 1, "Last week", _graph(now - timedelta(days=7)))
    _meeting(conn, 2, "This morning", _graph(midnight))
    _meeting(conn, 3, "Tomorrow", _graph(midnight + timedelta(days=1, hours=9)))

    ctx = get_person_context(conn, "dana@example.com")

    assert ctx["last_met"]["subject"] == "This morning"
    assert ctx["next_meeting"]["subject"] == "Tomorrow"


def test_the_thirty_day_count_ends_at_the_same_time_of_day(conn):
    """The same trap moved the -30 days boundary to the start of that day."""
    now = datetime.now(UTC)
    boundary_day = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    _meeting(conn, 1, "Thirty days and some hours ago", _graph(boundary_day))
    _meeting(conn, 2, "Last week", _graph(now - timedelta(days=7)))

    assert get_person_context(conn, "dana@example.com")["meeting_count_30d"] == 1
