"""Where each source starts and ends, for an answer that found nothing.

An empty answer read as "it does not exist": of the 105 brain calls that followed an
empty result in six weeks, 87 moved on to another topic, and no empty result said
why. Every empty list a tool returns now carries `coverage`: for each source the call
searched, the first and last date the store holds and how many items, read as `stats`
reads them (src.store.query.get_coverage), so "nothing found" reads as "nothing in X
between A and B".

The spans are read once per store file and kept for 15 minutes: they move by the
hour, and the GROUP BY over emails that gives them took over a second on a 6 GB
replica read cold.
"""

import os
import sqlite3
import time
from collections.abc import Iterable

TTL_SECONDS = 900.0

# The sources a call can search, by the names stats.coverage gives them.
MAIL = "mailboxes"
ATTACHMENTS = "attachments"
TEAMS = "teams"
WHATSAPP = "whatsapp"
CALENDAR = "calendar"
CONVERSATIONS = "conversations"
EVERY_SOURCE = (MAIL, ATTACHMENTS, TEAMS, WHATSAPP, CALENDAR, CONVERSATIONS)
# Where a decision, action or commitment can come from.
EXTRACTED_FROM = (MAIL, TEAMS, WHATSAPP, CALENDAR, CONVERSATIONS)

# The source behind each row kind (src/store/envelope.py KINDS); an image is found in mail.
SOURCE_OF_KIND = {
    "email": MAIL,
    "news": MAIL,
    "document": MAIL,
    "attachment": ATTACHMENTS,
    "teams": TEAMS,
    "whatsapp": WHATSAPP,
    "calendar": CALENDAR,
    "conversation": CONVERSATIONS,
    "image": MAIL,
}
# The calendar's month by month detail stays in stats; an empty answer keeps the span
# and the months not held in full.
_CALENDAR_DETAIL = ("per_month", "expected_per_month")

_cache: dict[tuple, tuple[float, dict]] = {}


def _store_key(conn: sqlite3.Connection) -> tuple | None:
    """The store file behind `conn` as it stands now, or None for one in memory."""
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row else ""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    # A pull replaces the file, so a new copy is read at once whatever the clock says.
    return (path, st.st_ino, st.st_mtime_ns, st.st_size)


def attachments_span(conn: sqlite3.Connection) -> dict:
    """The attachments the store holds, dated by the email each came with."""
    try:
        first, last, n = conn.execute(
            "SELECT MIN(e.date_received), MAX(e.date_received), COUNT(*) "
            "FROM attachments a JOIN emails e ON e.id = a.email_id"
        ).fetchone()
    except sqlite3.OperationalError:
        return {"first": None, "last": None, "items": 0}
    return {"first": first, "last": last, "items": n}


def store_coverage(conn: sqlite3.Connection) -> dict:
    """stats' coverage plus the attachments' span, cached per store file."""
    from src.store.query import get_coverage

    key = _store_key(conn)
    now = time.monotonic()
    if key is not None:
        held = _cache.get(key)
        if held and now - held[0] < TTL_SECONDS:
            return held[1]
    try:
        spans = {**get_coverage(conn), ATTACHMENTS: attachments_span(conn)}
    except sqlite3.OperationalError:
        # A store too old or too partial to say (no emails table): the empty answer
        # still goes out, with no span rather than a failure.
        return {}
    if key is not None:
        _cache.clear()  # one store per process; an old copy's entry is dead weight
        _cache[key] = (now, spans)
    return spans


def coverage_for(conn: sqlite3.Connection, sources: Iterable[str]) -> dict:
    """{source: span} for the sources named, in the order given."""
    spans = store_coverage(conn)
    out: dict = {}
    for source in dict.fromkeys(sources):
        span = spans.get(source)
        if span is None:
            continue
        if source == CALENDAR:
            span = {k: v for k, v in span.items() if k not in _CALENDAR_DETAIL}
        out[source] = span
    return out


def sources_of_kinds(kinds: Iterable[str] | None, default: Iterable[str]) -> tuple[str, ...]:
    """The sources rows of `kinds` come from; `default` when no kinds are named."""
    if kinds is None:
        return tuple(default)
    return tuple(dict.fromkeys(SOURCE_OF_KIND[k] for k in kinds if k in SOURCE_OF_KIND))


# A table sql_query reads, to the source whose span an empty answer gives.
_TABLE_SOURCES = (
    ("attachment", ATTACHMENTS),
    ("email", MAIL),
    ("key_facts", MAIL),
    ("teams", TEAMS),
    ("whatsapp", WHATSAPP),
    ("calendar", CALENDAR),
    ("event_attendees", CALENDAR),
    ("conversation", CONVERSATIONS),
)


def sources_of_tables(tables: Iterable[str]) -> tuple[str, ...]:
    """The sources behind the tables a query names: emails_fts is mail, and so on."""
    found = []
    for table in tables:
        name = table.lower()
        for prefix, source in _TABLE_SOURCES:
            if name.startswith(prefix):
                found.append(source)
                break
        else:
            if name in ("decisions", "action_items", "commitments"):
                found.extend(EXTRACTED_FROM)
    return tuple(dict.fromkeys(found))


def clear() -> None:
    """Forget every span read, so the next empty answer reads them again."""
    _cache.clear()
