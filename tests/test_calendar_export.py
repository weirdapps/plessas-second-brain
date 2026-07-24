"""Tests for calendar export module."""

from datetime import UTC, datetime

from src.export.calendar_export import chunk_date_range, parse_event


def test_chunk_date_range_single_month():
    """Single month range produces one chunk."""
    since = datetime(2026, 5, 1, tzinfo=UTC)
    until = datetime(2026, 5, 31, tzinfo=UTC)

    chunks = list(chunk_date_range(since, until))

    assert len(chunks) == 1
    assert chunks[0][0] == since
    assert chunks[0][1] == until


def test_chunk_date_range_three_months():
    """Multi-month range produces appropriate chunks."""
    since = datetime(2026, 3, 15, tzinfo=UTC)
    until = datetime(2026, 6, 10, tzinfo=UTC)

    chunks = list(chunk_date_range(since, until))

    # March 15 → June 10 spans 4 monthly chunks
    assert len(chunks) == 4
    # First chunk: March 15 → April 1
    assert chunks[0][0] == datetime(2026, 3, 15, tzinfo=UTC)
    assert chunks[0][1] == datetime(2026, 4, 1, tzinfo=UTC)
    # Second chunk: April 1 → May 1
    assert chunks[1][0] == datetime(2026, 4, 1, tzinfo=UTC)
    assert chunks[1][1] == datetime(2026, 5, 1, tzinfo=UTC)
    # Third chunk: May 1 → June 1
    assert chunks[2][0] == datetime(2026, 5, 1, tzinfo=UTC)
    assert chunks[2][1] == datetime(2026, 6, 1, tzinfo=UTC)
    # Fourth chunk: June 1 → June 10 (final until date)
    assert chunks[3][0] == datetime(2026, 6, 1, tzinfo=UTC)
    assert chunks[3][1] == until


def test_parse_event_extracts_fields():
    """Parse event extracts all required fields from raw Outlook event."""
    raw = {
        "Id": "evt-123",
        "Subject": "Team Sync",
        "Organizer": {
            "EmailAddress": {
                "Address": "organizer@example.com",
                "Name": "Jane Organizer",
            }
        },
        "Start": {"DateTime": "2026-05-15T10:00:00Z"},
        "End": {"DateTime": "2026-05-15T11:00:00Z"},
        "Location": {"DisplayName": "Room 101"},
        "IsRecurring": False,
        "ResponseStatus": {"Response": "Accepted"},
        "IsCancelled": False,
        "CreatedDateTime": "2026-05-01T08:00:00Z",
        "LastModifiedDateTime": "2026-05-02T09:00:00Z",
        "Attendees": [
            {
                "EmailAddress": {
                    "Address": "attendee1@example.com",
                    "Name": "John Attendee",
                },
                "Status": {"Response": "Accepted"},
            },
            {
                "EmailAddress": {
                    "Address": "attendee2@example.com",
                    "Name": "Mary Attendee",
                },
                "Status": {"Response": "TentativelyAccepted"},
            },
        ],
    }

    parsed = parse_event(raw)

    assert parsed["outlook_event_id"] == "evt-123"
    assert parsed["subject"] == "Team Sync"
    assert parsed["organizer_email"] == "organizer@example.com"
    assert parsed["organizer_name"] == "Jane Organizer"
    assert parsed["start_at"] == "2026-05-15T10:00:00Z"
    assert parsed["end_at"] == "2026-05-15T11:00:00Z"
    assert parsed["location"] == "Room 101"
    assert parsed["is_recurring"] is False
    assert parsed["recurrence_master_id"] is None
    assert parsed["response_status"] == "Accepted"
    assert parsed["is_cancelled"] is False
    assert parsed["created_at"] == "2026-05-01T08:00:00Z"
    assert parsed["modified_at"] == "2026-05-02T09:00:00Z"
    assert len(parsed["attendees"]) == 2
    assert parsed["attendees"][0] == {
        "email": "attendee1@example.com",
        "name": "John Attendee",
        "response_status": "Accepted",
    }
    assert parsed["attendees"][1] == {
        "email": "attendee2@example.com",
        "name": "Mary Attendee",
        "response_status": "TentativelyAccepted",
    }


def test_parse_event_handles_missing_fields():
    """Parse event handles missing optional fields gracefully."""
    raw = {
        "Id": "evt-456",
        "Subject": "Minimal Event",
        "Organizer": {"EmailAddress": {"Address": "org@example.com", "Name": "Org Name"}},
        "Start": {"DateTime": "2026-05-20T14:00:00Z"},
        "End": {"DateTime": "2026-05-20T15:00:00Z"},
        "Location": {"DisplayName": ""},
        "IsRecurring": False,
        "ResponseStatus": {"Response": "None"},
        # Missing: SeriesMasterId, IsCancelled, CreatedDateTime,
        # LastModifiedDateTime, Attendees
    }

    parsed = parse_event(raw)

    assert parsed["outlook_event_id"] == "evt-456"
    assert parsed["subject"] == "Minimal Event"
    assert parsed["location"] == ""
    assert parsed["recurrence_master_id"] is None
    assert parsed["is_cancelled"] is False
    assert parsed["created_at"] is None
    assert parsed["modified_at"] is None
    assert parsed["attendees"] == []


def test_parse_event_with_recurrence():
    """Parse event correctly extracts recurrence master ID."""
    raw = {
        "Id": "evt-rec-1",
        "Subject": "Weekly Standup",
        "Organizer": {"EmailAddress": {"Address": "scrum@example.com", "Name": "Scrum Master"}},
        "Start": {"DateTime": "2026-05-19T09:00:00Z"},
        "End": {"DateTime": "2026-05-19T09:30:00Z"},
        "Location": {"DisplayName": "Teams"},
        "IsRecurring": True,
        "SeriesMasterId": "series-abc-123",
        "ResponseStatus": {"Response": "Accepted"},
    }

    parsed = parse_event(raw)

    assert parsed["is_recurring"] is True
    assert parsed["recurrence_master_id"] == "series-abc-123"
