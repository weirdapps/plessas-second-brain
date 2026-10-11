"""Calendar event loader with UPSERT, attendee resolution, and proxy logic."""

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.redact import redact_secrets

logger = logging.getLogger(__name__)

# The calendar_events.llm_status vocabulary. Defined here, next to the only writer, so
# there is one spelling of each value; schema.migrate_add_calendar_llm_status explains what
# each one means and why there are four rather than the attachment table's three.
# 'pending' is load-bearing beyond bookkeeping: it is the ONLY value that makes
# cmd_calendar_sync's change detector re-offer an event whose etag has not moved.
LLM_STATUSES = ("extracted", "pending", "failed", "skipped")


def load_proxy_emails(canonical_path: str) -> set[str] | None:
    """
    Load proxy-organizer emails from canonical_people.json.

    Args:
        canonical_path: Path to canonical_people.json

    Returns:
        Set of lowercase proxy emails, or empty set if file doesn't exist, or None
        if it exists but cannot be parsed, so a caller can tell a broken file from
        an absent one.
    """
    try:
        path = Path(canonical_path)
        if not path.exists():
            # It was missing on every host and nothing said so, so meetings the
            # PA books were never recognised as the owner's.
            logger.warning("%s not found; proxy-organised events will not count as self", path)
            return set()

        data = json.loads(path.read_text())
        proxy_emails = {
            person["email"].lower()
            for person in data
            if person.get("is_proxy_for_self") is True and person.get("email")
        }
        return proxy_emails
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        # This branch said nothing, so a merge conflict or a stray comma in the
        # hand-edited file read exactly like a file with no proxies, and
        # calendar-sync then rewrote every event the PA books as not the owner's.
        logger.warning(
            "%s unreadable (%s); proxy-organised events will not count as self",
            canonical_path,
            exc,
        )
        return None


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_date_or_none(value) -> str | None:
    """Keep a decision_date only if it starts with a YYYY-MM-DD date.

    A free-text value ('end of October', the string 'null') sorts above every
    real date and passes every days filter; NULL lets the readers fall back to
    the meeting's own start, as query_decisions' COALESCE already does.
    """
    if isinstance(value, str) and _ISO_DATE.match(value.strip()):
        return value.strip()
    return None


def _is_self_email(email: str, user_email_pattern: str) -> bool:
    """Whether ``email`` is the owner's, by case-insensitive substring.

    An empty pattern matches nobody. '' is a substring of every string, so with
    BRAIN_USER_EMAIL_PATTERN unset on the producer every event was stored as
    self-organized and every one of 31,588 attendees as the owner.
    """
    return bool(user_email_pattern) and user_email_pattern.lower() in (email or "").lower()


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
    if _is_self_email(organizer_email, user_email_pattern):
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
    extraction_hash: str | None = None,
    keep_extraction: bool = False,
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
        extraction_hash: event_prompt_hash() of the prompt behind an 'extracted'
            row; stored with it, kept by any other status.
        keep_extraction: the etag moved but the prompt did not (same hash), so the
            extraction on record still stands: refresh the facts and the etag, keep
            the summary, its stamp, the decisions and the action items. Only with
            llm_status='extracted'.

    Returns:
        event_id
    """
    if llm_status not in LLM_STATUSES:
        raise ValueError(f"llm_status must be one of {LLM_STATUSES}, got {llm_status!r}")
    if keep_extraction and llm_status != "extracted":
        raise ValueError("keep_extraction only applies to an 'extracted' row")

    # Compute is_self_organized
    is_self_organized = _is_self_organized(
        event.get("organizer_email", ""), user_email_pattern, proxy_emails
    )

    # Current UTC timestamp
    now_utc = datetime.now(UTC).isoformat()

    # Determine body_extracted_at
    body_extracted_at = now_utc if extraction.get("body_summary") else None
    body_summary = extraction.get("body_summary")
    if keep_extraction:
        # Written back as they are, because the UPSERT replaces both on 'extracted'.
        prior = conn.execute(
            "SELECT body_summary, body_extracted_at FROM calendar_events "
            "WHERE outlook_event_id = ?",
            (event["outlook_event_id"],),
        ).fetchone()
        if prior is not None:
            body_summary, body_extracted_at = prior[0], prior[1]

    # UPSERT event. The summary and its stamp are replaced only by a new
    # extraction, as the decisions and actions below are. Any other status comes
    # with an empty extraction: a failure or a deferral knows nothing about the
    # meeting, and 'skipped' only knows the body is now too short to summarise,
    # while the decisions it keeps came from the body the summary did. Writing ''
    # over it lost the summary for good on a 'failed', which is not re-offered.
    conn.execute(
        """
        INSERT INTO calendar_events (
            outlook_event_id, subject, organizer_email, organizer_name,
            start_at, end_at, location, is_recurring, recurrence_master_id,
            response_status, is_cancelled, is_self_organized,
            created_at, modified_at, ingested_at, body_extracted_at, body_summary,
            llm_status, change_key, extraction_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            body_extracted_at = CASE WHEN excluded.llm_status = 'extracted'
                THEN excluded.body_extracted_at ELSE calendar_events.body_extracted_at END,
            body_summary = CASE WHEN excluded.llm_status = 'extracted'
                THEN excluded.body_summary ELSE calendar_events.body_summary END,
            llm_status = excluded.llm_status,
            change_key = excluded.change_key,
            extraction_hash = CASE WHEN excluded.llm_status = 'extracted'
                THEN excluded.extraction_hash ELSE calendar_events.extraction_hash END
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
            body_summary,
            llm_status,
            event.get("change_key"),
            extraction_hash,
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
        is_self = _is_self_email(attendee["email"], user_email_pattern)
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

    # A successful extraction replaces the event's previous one, the way the
    # attendee rows above are replaced. Appending is what stacked a full new set
    # on every re-extraction (916 decisions on one meeting by 2026-09-23). Any
    # other status arrives with an empty extraction that says nothing about the
    # event, so the previous decisions and actions are kept, as they are when the
    # extraction on record is kept (keep_extraction).
    if llm_status == "extracted" and not keep_extraction:
        conn.execute("DELETE FROM decisions WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM action_items WHERE event_id = ?", (event_id,))

    # Insert decisions. Their texts and the action items' are masked as the loader
    # masks an email's (src/store/loader.py).
    for decision in extraction.get("decisions", []):
        conn.execute(
            """
            INSERT INTO decisions (decision, decided_by, decision_date, event_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                redact_secrets(decision.get("decision")),
                decision.get("decided_by"),
                _iso_date_or_none(decision.get("decision_date")),
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
                redact_secrets(action.get("task")),
                action.get("owner"),
                action.get("deadline"),
                "open",
                event_id,
            ),
        )

    conn.commit()
    return event_id


def refresh_self_flags(
    conn: sqlite3.Connection,
    user_email_pattern: str,
    proxy_emails: set[str] | None = None,
) -> int:
    """Recompute is_self_organized and is_self on every stored row. Returns rows changed.

    load_event only sets the flags when an event is upserted, and an unchanged
    event is never upserted again, so rows written under a wrong or empty
    pattern stay wrong. Cheap enough to run on every calendar-sync (a thousand
    events, a few tens of thousands of attendees), which also makes a changed
    pattern take effect everywhere at once.
    """
    changed = 0
    for event_id, organizer, flag in conn.execute(
        "SELECT id, organizer_email, is_self_organized FROM calendar_events"
    ).fetchall():
        want = 1 if _is_self_organized(organizer or "", user_email_pattern, proxy_emails) else 0
        if want != flag:
            conn.execute(
                "UPDATE calendar_events SET is_self_organized = ? WHERE id = ?", (want, event_id)
            )
            changed += 1
    for rowid, email, flag in conn.execute(
        "SELECT rowid, email, is_self FROM event_attendees"
    ).fetchall():
        want = 1 if _is_self_email(email, user_email_pattern) else 0
        if want != flag:
            conn.execute("UPDATE event_attendees SET is_self = ? WHERE rowid = ?", (want, rowid))
            changed += 1
    conn.commit()
    return changed


def cancel_unlisted(
    conn: sqlite3.Connection, listed_ids: set[str], window_start: str, window_end: str
) -> int:
    """Mark cancelled the stored events in a window that Outlook no longer lists.

    Returns how many were marked. Only for a window listed completely: an event
    deleted or re-created in Outlook was never removed, so the readers kept
    reporting meetings that no longer exist. The rows are kept, marked, which is
    how the readers already treat Outlook's own cancellations.

    ``window_start`` and ``window_end`` are UTC in start_at's shape, a
    half-open range. When more than half of the window's live events would go
    at once, nothing is marked: that is far likelier a bad answer from Outlook
    than a cleared calendar, and marking would hide real meetings.

    The etag is cleared with the flag, so an event that is listed again is not
    taken as unchanged but fetched, and the upsert sets is_cancelled from
    Outlook's own answer.
    """
    live = conn.execute(
        "SELECT id, outlook_event_id FROM calendar_events "
        "WHERE is_cancelled = 0 AND start_at >= ? AND start_at < ?",
        (window_start, window_end),
    ).fetchall()
    gone = [row_id for row_id, event_id in live if event_id not in listed_ids]
    if not gone:
        return 0
    if 2 * len(gone) > len(live):
        logger.warning(
            "%d of %d stored events in %s..%s are no longer listed; not marking them "
            "cancelled, since more than half of a window going at once is likelier a "
            "bad listing",
            len(gone),
            len(live),
            window_start,
            window_end,
        )
        return 0
    conn.executemany(
        "UPDATE calendar_events SET is_cancelled = 1, change_key = NULL WHERE id = ?",
        [(row_id,) for row_id in gone],
    )
    conn.commit()
    return len(gone)


def dedupe_event_children(conn: sqlite3.Connection) -> tuple[int, int]:
    """Delete exact duplicate calendar decisions and action items, keeping the first.

    Returns (decisions_removed, action_items_removed). Cleans what the old
    append-only load_event stacked up, and stays as a guard: an exact duplicate
    within one event is never information. Scoped to event_id rows, so an email
    or Teams decision with the same text is untouched.
    """
    decisions = conn.execute(
        "DELETE FROM decisions WHERE event_id IS NOT NULL AND id NOT IN "
        "(SELECT MIN(id) FROM decisions WHERE event_id IS NOT NULL GROUP BY event_id, "
        "decision, COALESCE(decided_by, ''), COALESCE(decision_date, ''))"
    ).rowcount
    actions = conn.execute(
        "DELETE FROM action_items WHERE event_id IS NOT NULL AND id NOT IN "
        "(SELECT MIN(id) FROM action_items WHERE event_id IS NOT NULL GROUP BY event_id, "
        "task, COALESCE(owner, ''), COALESCE(deadline, ''))"
    ).rowcount
    conn.commit()
    return decisions, actions
