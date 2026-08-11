"""Calendar event loader with UPSERT, attendee resolution, and proxy logic."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# The calendar_events.llm_status vocabulary. Defined here, next to the only writer, so
# there is one spelling of each value; schema.migrate_add_calendar_llm_status explains what
# each one means and why there are four rather than the attachment table's three.
# 'pending' is load-bearing beyond bookkeeping: it is the ONLY value that makes
# cmd_calendar_sync's change detector re-offer an event whose modified_at has not moved.
LLM_STATUSES = ("extracted", "pending", "failed", "skipped")


def load_proxy_emails(canonical_path: str) -> set[str]:
    """
    Load proxy-organizer emails from canonical_people.json.

    Args:
        canonical_path: Path to canonical_people.json

    Returns:
        Set of lowercase proxy emails, or empty set if file doesn't exist
    """
    try:
        path = Path(canonical_path)
        if not path.exists():
            return set()

        data = json.loads(path.read_text())
        proxy_emails = {
            person["email"].lower()
            for person in data
            if person.get("is_proxy_for_self") is True and person.get("email")
        }
        return proxy_emails
    except (json.JSONDecodeError, KeyError, TypeError):
        return set()


def _is_self_organized(
    organizer_email: str,
    user_email_pattern: str,
    proxy_emails: set[str] | None = None,
) -> bool:
    """
    Check if event is self-organized (user or proxy).

    Args:
        organizer_email: Event organizer email
        user_email_pattern: User email pattern (case-insensitive substring match)
        proxy_emails: Optional set of proxy emails (lowercase)

    Returns:
        True if user_email_pattern matches organizer or organizer is in proxy set
    """
    # Direct user match (case-insensitive)
    if user_email_pattern.lower() in organizer_email.lower():
        return True

    # Proxy match
    if proxy_emails and organizer_email.lower() in proxy_emails:
        return True

    return False


def _resolve_person_id(conn: sqlite3.Connection, email: str) -> int | None:
    """
    Resolve person_id from email (case-insensitive).

    Args:
        conn: Database connection
        email: Person email

    Returns:
        person_id or None if not found
    """
    row = conn.execute(
        "SELECT id FROM people WHERE LOWER(email) = LOWER(?) LIMIT 1", (email,)
    ).fetchone()
    return row[0] if row else None


def load_event(
    conn: sqlite3.Connection,
    event: dict,
    extraction: dict,
    user_email_pattern: str = "",
    proxy_emails: set[str] | None = None,
    *,
    llm_status: str,
) -> int:
    """
    UPSERT calendar event + attendees + decisions + action items.

    The event's FACTS are written whatever ``llm_status`` says. Time, title, organiser and
    attendees come from the Graph API rather than from a model, so they are good even when
    the extraction that was supposed to accompany them failed, and withholding them would
    lose data that was never in doubt. What ``llm_status`` governs is whether the ABSENCE of
    an extraction is a finished state or a debt.

    Args:
        conn: Database connection
        event: Event dict with outlook_event_id, subject, organizer_email, etc.
        extraction: Extraction dict with body_summary, decisions, action_items
        user_email_pattern: User email pattern for is_self_organized check
        proxy_emails: Optional set of proxy emails for is_self_organized check
        llm_status: One of LLM_STATUSES. REQUIRED, and keyword-only, deliberately. A
            default would be a default answer to "did the extraction succeed?", and a
            caller that forgot to pass it would silently record a failure as a success —
            which is the exact bug this column was added to fix.

    Returns:
        event_id
    """
    if llm_status not in LLM_STATUSES:
        raise ValueError(f"llm_status must be one of {LLM_STATUSES}, got {llm_status!r}")

    # Compute is_self_organized
    is_self_organized = _is_self_organized(
        event.get("organizer_email", ""), user_email_pattern, proxy_emails
    )

    # Current UTC timestamp
    now_utc = datetime.now(UTC).isoformat()

    # Determine body_extracted_at
    body_extracted_at = now_utc if extraction.get("body_summary") else None

    # UPSERT event
    conn.execute(
        """
        INSERT INTO calendar_events (
            outlook_event_id, subject, organizer_email, organizer_name,
            start_at, end_at, location, is_recurring, recurrence_master_id,
            response_status, is_cancelled, is_self_organized,
            created_at, modified_at, ingested_at, body_extracted_at, body_summary,
            llm_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(outlook_event_id) DO UPDATE SET
            subject = excluded.subject,
            organizer_email = excluded.organizer_email,
            organizer_name = excluded.organizer_name,
            start_at = excluded.start_at,
            end_at = excluded.end_at,
            location = excluded.location,
            is_recurring = excluded.is_recurring,
            recurrence_master_id = excluded.recurrence_master_id,
            response_status = excluded.response_status,
            is_cancelled = excluded.is_cancelled,
            is_self_organized = excluded.is_self_organized,
            created_at = excluded.created_at,
            modified_at = excluded.modified_at,
            ingested_at = excluded.ingested_at,
            body_extracted_at = excluded.body_extracted_at,
            body_summary = excluded.body_summary,
            llm_status = excluded.llm_status
        """,
        (
            event["outlook_event_id"],
            event.get("subject"),
            event.get("organizer_email"),
            event.get("organizer_name"),
            event.get("start_at"),
            event.get("end_at"),
            event.get("location"),
            1 if event.get("is_recurring") else 0,
            event.get("recurrence_master_id"),
            event.get("response_status"),
            1 if event.get("is_cancelled") else 0,
            1 if is_self_organized else 0,
            event.get("created_at"),
            event.get("modified_at"),
            now_utc,
            body_extracted_at,
            extraction.get("body_summary"),
            llm_status,
        ),
    )

    # Get event_id
    event_id = conn.execute(
        "SELECT id FROM calendar_events WHERE outlook_event_id = ?",
        (event["outlook_event_id"],),
    ).fetchone()[0]

    # Delete existing attendees (replace on UPSERT)
    conn.execute("DELETE FROM event_attendees WHERE event_id = ?", (event_id,))

    # Insert attendees
    for attendee in event.get("attendees", []):
        person_id = _resolve_person_id(conn, attendee["email"])
        is_self = user_email_pattern.lower() in attendee["email"].lower()
        is_organizer = attendee["email"].lower() == event.get("organizer_email", "").lower()

        conn.execute(
            """
            INSERT INTO event_attendees (
                event_id, person_id, email, name,
                response_status, is_self, is_organizer
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                person_id,
                attendee["email"],
                attendee.get("name"),
                attendee.get("response_status"),
                1 if is_self else 0,
                1 if is_organizer else 0,
            ),
        )

    # Insert decisions
    for decision in extraction.get("decisions", []):
        conn.execute(
            """
            INSERT INTO decisions (decision, decided_by, decision_date, event_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                decision.get("decision"),
                decision.get("decided_by"),
                decision.get("decision_date"),
                event_id,
            ),
        )

    # Insert action items
    for action in extraction.get("action_items", []):
        conn.execute(
            """
            INSERT INTO action_items (task, owner, deadline, status, event_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                action.get("task"),
                action.get("owner"),
                action.get("deadline"),
                "open",
                event_id,
            ),
        )

    conn.commit()
    return event_id
