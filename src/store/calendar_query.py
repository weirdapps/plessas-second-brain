"""Calendar reads the tools share: who is at a meeting, and the meetings shared with people.

Events average 48 attendees and reach 500, Outlook's limit, and one daily call's
distribution list was 41% of every attendee row (data-coverage-05).
query_calendar_events listed the first ten, which could leave out the organiser
and the owner while strangers were shown; meeting_prep listed no meeting at all.
A calendar answer now lists a dozen attendees per event, the owner and the
organiser always among them.
"""

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from src.store.greek import search_fold

# Attendees a calendar answer lists per event unless asked for more or fewer;
# `attendees_total` counts them all.
ATTENDEES_SHOWN = 12
# The most it lists when asked: Outlook's own limit on an event's attendees.
ATTENDEES_MAX = 500
# Meetings meeting_prep names on each side of now.
MEETINGS_SHOWN = 3

_ATHENS = ZoneInfo("Europe/Athens")


def athens_time(value: str | None) -> str | None:
    """A stored time in Athens, with its offset; None if it does not parse."""
    try:
        moment = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    # Every stored time is UTC, the few written without the 'Z' included.
    return moment.replace(tzinfo=moment.tzinfo or UTC).astimezone(_ATHENS).isoformat()


def capped_attendees(
    conn: sqlite3.Connection,
    event_id: int,
    *,
    limit: int = ATTENDEES_SHOWN,
    person: str = "",
    emails: Iterable[str] = (),
) -> tuple[list[dict], int]:
    """At most `limit` attendees of an event, and how many it has.

    Kept whatever else is cut: the owner and the organiser. Then those `person`
    matches (part of a name, case and accent blind, or of an address) or whose
    address is in `emails`, then the rest in the order Outlook lists them. They
    come back in that order, the matched ones first, so a cut list still shows
    the people it was asked about.
    """
    rows = conn.execute(
        "SELECT name, email, response_status, is_self, is_organizer FROM event_attendees "
        "WHERE event_id = ? ORDER BY id",
        (event_id,),
    ).fetchall()
    folded, address = search_fold(person), person.strip().lower()
    wanted = {email.lower() for email in emails if email}

    def matched(row) -> bool:
        email = (row["email"] or "").lower()
        if email in wanted:
            return True
        return bool(person) and (folded in search_fold(row["name"]) or address in email)

    order = sorted(
        range(len(rows)),
        key=lambda i: (not matched(rows[i]), not rows[i]["is_self"], not rows[i]["is_organizer"]),
    )
    owner_and_organiser = [i for i in order if rows[i]["is_self"] or rows[i]["is_organizer"]]
    kept = set(owner_and_organiser[: max(limit, 0)])
    for i in order:
        if len(kept) >= limit:
            break
        kept.add(i)
    return [
        {"name": rows[i]["name"], "email": rows[i]["email"], "status": rows[i]["response_status"]}
        for i in order
        if i in kept
    ], len(rows)


def shared_meetings(
    conn: sqlite3.Connection,
    emails: Iterable[str],
    *,
    days: int = 365,
    limit: int = MEETINGS_SHOWN,
    attendees: int = ATTENDEES_SHOWN,
) -> dict[str, list[dict]]:
    """The next and the last meetings any of `emails` attends, up to `limit` each.

    The ones most of them attend come first, then the soonest, or the latest of
    those already held, which go back `days`. A cancelled meeting is not one.
    Each lists at most `attendees` attendees, those people kept among them.
    """
    wanted = sorted({email.lower() for email in emails if email})
    if not wanted:
        return {"next": [], "last": []}
    marks = ", ".join("?" * len(wanted))
    # Rendered with a 'T' like start_at: 'T' sorts above datetime('now')'s space.
    now = datetime.now(UTC)
    present, cutoff = (
        moment.strftime("%Y-%m-%dT%H:%M:%S") for moment in (now, now - timedelta(days=days))
    )
    query = f"""
        SELECT ce.id, ce.subject, ce.start_at, ce.end_at, ce.location, ce.organizer_name,
               ce.organizer_email, ce.body_summary, ce.is_recurring,
               (SELECT COUNT(DISTINCT LOWER(a.email)) FROM event_attendees a
                 WHERE a.event_id = ce.id AND LOWER(a.email) IN ({marks})) AS present
        FROM calendar_events ce
        WHERE ce.is_cancelled = 0
          AND ce.id IN (SELECT event_id FROM event_attendees WHERE LOWER(email) IN ({marks}))
    """
    upcoming = " AND ce.start_at >= ? ORDER BY present DESC, ce.start_at LIMIT ?"
    held = (
        " AND ce.start_at < ? AND ce.start_at >= ? ORDER BY present DESC, ce.start_at DESC LIMIT ?"
    )
    next_rows = conn.execute(query + upcoming, (*wanted, *wanted, present, limit)).fetchall()
    last_rows = conn.execute(query + held, (*wanted, *wanted, present, cutoff, limit)).fetchall()

    def meeting(row) -> dict:
        listed, total = capped_attendees(conn, row["id"], limit=attendees, emails=wanted)
        return {
            "id": row["id"],
            "subject": row["subject"],
            "start_at": row["start_at"],
            "start_local": athens_time(row["start_at"]),
            "end_local": athens_time(row["end_at"]),
            "location": row["location"],
            "organizer": row["organizer_name"] or row["organizer_email"],
            "is_recurring": bool(row["is_recurring"]),
            "body_summary": row["body_summary"],
            "attendees_total": total,
            "attendees": listed,
        }

    return {"next": [meeting(r) for r in next_rows], "last": [meeting(r) for r in last_rows]}
