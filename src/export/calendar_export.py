"""Calendar export module — wraps outlook-cli list-calendar and get-event."""

import logging
from collections.abc import Iterator
from datetime import datetime

from src.export.outlook_cli import OutlookCliAuthRequired, run_outlook_cli

logger = logging.getLogger(__name__)


def chunk_date_range(since: datetime, until: datetime) -> Iterator[tuple[datetime, datetime]]:
    """
    Split a date range into monthly chunks.

    Each chunk is (start, end) where end is the first of the next month
    (or `until` if smaller). Used to paginate calendar API calls.

    Args:
        since: Start of date range (inclusive)
        until: End of date range (inclusive)

    Yields:
        Tuples of (chunk_start, chunk_end)
    """
    current = since
    while current < until:
        # Calculate first of next month
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1, day=1)
        else:
            next_month = current.replace(month=current.month + 1, day=1)

        # Use the smaller of next_month or until
        chunk_end = min(next_month, until)

        yield (current, chunk_end)

        # If we've reached until, stop
        if chunk_end >= until:
            break

        current = chunk_end


def list_events(since: datetime, until: datetime, failures: list[str] | None = None) -> list[dict]:
    """
    Fetch calendar events in monthly chunks via outlook-cli.

    Args:
        since: Start of date range (inclusive)
        until: End of date range (inclusive)
        failures: If given, one entry is appended per chunk that could not be
            fetched. A failed chunk used to be logged and skipped, so the caller
            reported a partial window as complete; an answer that is not a list
            is a failure too, not zero events.

    Returns:
        List of raw event dicts from Outlook

    Raises:
        OutlookCliAuthRequired: If outlook-cli requires re-authentication
    """
    all_events = []

    for chunk_start, chunk_end in chunk_date_range(since, until):
        # Format as ISO strings for outlook-cli
        from_iso = chunk_start.isoformat()
        to_iso = chunk_end.isoformat()
        window = f"{chunk_start.date()}..{chunk_end.date()}"

        try:
            result = run_outlook_cli(["list-calendar", "--from", from_iso, "--to", to_iso])
        except OutlookCliAuthRequired:
            # Re-raise auth errors immediately
            raise
        except Exception as e:
            # Log other errors but continue with remaining chunks
            logger.error(f"Error fetching events {window}: {e}")
            if failures is not None:
                failures.append(f"{window}: {e}")
            continue
        if not isinstance(result, list):
            logger.error(f"Unexpected list-calendar answer for {window}: {type(result).__name__}")
            if failures is not None:
                failures.append(f"{window}: unexpected answer of type {type(result).__name__}")
            continue
        all_events.extend(result)
        logger.info(f"Fetched {len(result)} events from {chunk_start.date()} to {chunk_end.date()}")

    return all_events


def get_event_body(event_id: str) -> dict | None:
    """
    Fetch a single calendar event with full body via outlook-cli.

    Args:
        event_id: Outlook event ID

    Returns:
        Event dict with body, or None on error
    """
    try:
        result = run_outlook_cli(["get-event", event_id, "--body", "html"])
        return result
    except Exception as e:
        logger.error(f"Error fetching event body for {event_id}: {e}")
        return None


def parse_event(raw: dict) -> dict:
    """
    Normalize an Outlook calendar event into internal shape.

    Args:
        raw: Raw event dict from outlook-cli

    Returns:
        Normalized event dict with standardized field names
    """
    # Extract attendees list
    attendees = []
    for att in raw.get("Attendees", []):
        email_addr = att.get("EmailAddress", {})
        status = att.get("Status", {})
        attendees.append(
            {
                "email": email_addr.get("Address", ""),
                "name": email_addr.get("Name", ""),
                "response_status": status.get("Response", "None"),
            }
        )

    # Extract organizer info
    organizer = raw.get("Organizer", {}).get("EmailAddress", {})

    # Extract start/end times
    start = raw.get("Start", {})
    end = raw.get("End", {})

    # Extract location
    location = raw.get("Location", {})

    # Extract response status
    response_status = raw.get("ResponseStatus", {})

    return {
        "outlook_event_id": raw.get("Id", ""),
        "subject": raw.get("Subject", ""),
        "organizer_email": organizer.get("Address", ""),
        "organizer_name": organizer.get("Name", ""),
        "start_at": start.get("DateTime"),
        "end_at": end.get("DateTime"),
        "location": location.get("DisplayName", ""),
        "is_recurring": raw.get("IsRecurring", False),
        "recurrence_master_id": raw.get("SeriesMasterId"),
        "response_status": response_status.get("Response", "None"),
        "is_cancelled": raw.get("IsCancelled", False),
        "created_at": raw.get("CreatedDateTime"),
        "modified_at": raw.get("LastModifiedDateTime"),
        "attendees": attendees,
    }
