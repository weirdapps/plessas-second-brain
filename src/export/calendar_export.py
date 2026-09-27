"""Calendar export module — wraps outlook-cli list-calendar and get-event."""

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta

from src.export.outlook_cli import OutlookCliAuthRequired, run_outlook_cli

logger = logging.getLogger(__name__)

# outlook-cli's list-calendar answers with one page of Outlook's calendarview: the
# ten earliest events of the window, however many it holds, since it neither asks
# for a page size nor follows the next link. Every month-long chunk came back with
# ten, so the week ahead was never stored. A span that returns exactly a page may
# have been cut short and is never taken as complete.
OUTLOOK_PAGE_SIZE = 10

# A full page over a span this short is not split again but reported as a failure:
# more events than a page cannot be told apart from exactly a page.
MIN_SPLIT_SPAN = timedelta(hours=1)

# outlook-cli calls one listing may make in all. Every full page costs two more,
# so the bound keeps a busy window inside the unit's time budget, and reaching it
# is a failure rather than a quiet stop. A backfill lists a year a month at a time
# and needs the larger one.
DEFAULT_LIST_CALLS = 120
BACKFILL_LIST_CALLS = 3000


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


def _merged(*pages: list[dict]) -> list[dict]:
    """The events of every page once each, by Id, in start order.

    calendarview returns every event that overlaps the window, so one that runs
    across a split or a chunk boundary is listed on both sides.
    """
    seen: set[str] = set()
    merged = []
    for page in pages:
        for raw in page:
            event_id = raw.get("Id")
            if event_id:
                if event_id in seen:
                    continue
                seen.add(event_id)
            merged.append(raw)
    return sorted(merged, key=lambda raw: (raw.get("Start") or {}).get("DateTime") or "")


def list_events(
    since: datetime,
    until: datetime,
    failures: list[str] | None = None,
    max_calls: int = DEFAULT_LIST_CALLS,
) -> list[dict]:
    """
    Fetch calendar events in monthly chunks via outlook-cli.

    A span that comes back as a full page (OUTLOOK_PAGE_SIZE events) is listed
    again as two halves split at its midpoint, recursively, and the answers are
    merged by event Id. A full page over MIN_SPLIT_SPAN or less, and reaching
    ``max_calls``, are failures: the events listed are still returned, but the
    window is not complete.

    Args:
        since: Start of date range (inclusive)
        until: End of date range (inclusive)
        failures: If given, one entry is appended per span that could not be
            fetched or could not be listed completely. A failed chunk used to be
            logged and skipped, so the caller reported a partial window as
            complete; an answer that is not a list is a failure too, not zero
            events.
        max_calls: The most outlook-cli calls this listing may make in all.

    Returns:
        List of raw event dicts from Outlook

    Raises:
        OutlookCliAuthRequired: If outlook-cli requires re-authentication
    """
    calls = 0
    bound_reached = False

    def fail(window: str, reason: str) -> None:
        logger.error(f"Could not list {window} completely: {reason}")
        if failures is not None:
            failures.append(f"{window}: {reason}")

    def list_span(start: datetime, end: datetime) -> list[dict]:
        nonlocal calls, bound_reached
        window = f"{start:%Y-%m-%d %H:%M}..{end:%Y-%m-%d %H:%M}"
        if calls >= max_calls:
            # Reported once: every span after this one is unlisted for the same reason.
            if not bound_reached:
                bound_reached = True
                fail(
                    window, f"stopped after {max_calls} list-calendar calls, the bound for one run"
                )
            return []
        calls += 1
        try:
            result = run_outlook_cli(
                ["list-calendar", "--from", start.isoformat(), "--to", end.isoformat()]
            )
        except OutlookCliAuthRequired:
            # Re-raise auth errors immediately
            raise
        except Exception as e:
            # Log other errors but continue with remaining spans
            fail(window, str(e))
            return []
        if not isinstance(result, list):
            fail(window, f"unexpected answer of type {type(result).__name__}")
            return []
        if len(result) != OUTLOOK_PAGE_SIZE:
            logger.info(f"Fetched {len(result)} events from {window}")
            return result
        if end - start <= MIN_SPLIT_SPAN:
            fail(window, f"a full page of {len(result)} events in a span too short to split")
            return result
        # A warning, because calendar-sync.log is stderr and cli.py configures no
        # logging: an info line would never reach it.
        middle = (start + (end - start) / 2).replace(microsecond=0)
        logger.warning(
            f"{window}: a full page of {len(result)} events, which may be cut short; "
            f"listing it again in two halves split at {middle:%Y-%m-%d %H:%M}"
        )
        # The page itself too, so a half that fails loses nothing it had listed.
        return _merged(list_span(start, middle), list_span(middle, end), result)

    all_events: list[dict] = []
    for chunk_start, chunk_end in chunk_date_range(since, until):
        all_events = _merged(all_events, list_span(chunk_start, chunk_end))
    return all_events


def get_event_body(event_id: str) -> dict | None:
    """
    Fetch a single calendar event with full body via outlook-cli.

    Args:
        event_id: Outlook event ID

    Returns:
        Event dict with body, or None on error

    Raises:
        OutlookCliAuthRequired: If outlook-cli requires re-authentication. It was
            swallowed with everything else, so an expired session read as one
            failed fetch per event instead of as the outage it is.
    """
    try:
        result = run_outlook_cli(["get-event", event_id, "--body", "html"])
        return result
    except OutlookCliAuthRequired:
        raise
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
        # In list-calendar entries too, unlike LastModifiedDateTime: the change detector.
        "change_key": raw.get("@odata.etag"),
        "attendees": attendees,
    }
