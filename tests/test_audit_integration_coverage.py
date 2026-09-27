"""stats.coverage counts the meetings that are still on (teams-calendar-2 follow-up).

A meeting Outlook no longer lists is kept as is_cancelled=1 rather than deleted.
The coverage span still counted it, so a phantom meeting could set the calendar's
last date and inflate its item count, the span agents are told to check before
concluding that something did not happen.
"""

from src.store.query import get_coverage
from src.store.schema import create_database


def test_a_cancelled_meeting_does_not_extend_the_calendar_span():
    conn = create_database(":memory:")
    conn.executemany(
        "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at, is_cancelled, "
        "ingested_at) VALUES (?, ?, ?, ?, ?, '2026-09-27T10:00:00Z')",
        [
            ("live", "Weekly", "2026-10-01T07:30:00.0000000Z", "2026-10-01T08:00:00.0000000Z", 0),
            ("gone", "Phantom", "2026-11-20T09:00:00.0000000Z", "2026-11-20T10:00:00.0000000Z", 1),
        ],
    )

    calendar = get_coverage(conn)["calendar"]

    assert calendar["last"] == "2026-10-01T07:30:00.0000000Z"
    assert calendar["items"] == 1
