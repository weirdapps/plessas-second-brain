"""High-level context retrieval API for external consumers.

Provides rich context functions for the email-handler plugin and /recall skill
to retrieve person, topic, conversation, and decision context from the knowledge store.
"""

import re
import sqlite3
from datetime import datetime, timedelta

from src.store.greek import (
    PHRASE_MATCH,
    register_sql_functions,
    search_fold,
    search_phrase,
    search_words,
)
from src.store.normalizer import normalize_topic
from src.store.ordering import (
    EMAIL_DATE,
    action_order,
    decision_date,
    decision_dates,
    decision_order,
    fact_order,
    parent_dates,
)

# Every list in a context dossier is capped at this many rows unless the caller
# asks for more. These functions are reached from MCP tools, so their return
# value is spent from the model's context window, and until 2026-09-09 the
# topics / decisions / open_actions / key_people / key_facts queries had no LIMIT
# at all. Measured on the live corpus, get_person_context on the top
# correspondent returned 77 MB (175,035 open_actions and 111,744 decisions) and
# get_topic_context('media monitoring') 6.5 MB, so every call on a heavy person
# or a broad topic was rejected outright by the MCP result cap. `recall`, the
# documented front door, auto-injects both and failed on 9 of 12 ordinary
# one-word queries for the same reason. Each capped list ships a `<name>_total`
# sibling so a caller can still tell a complete answer from the head of one.
DEFAULT_CONTEXT_LIMIT = 20


def resolve_person(
    conn: sqlite3.Connection, name_or_email: str, *, local_part: bool = True
) -> tuple[sqlite3.Row | None, int, list[dict]]:
    """The person a name or email means, how many people matched, and the next three.

    A name is matched folded (case, accents, final sigma), because people are
    mostly stored in ALL-CAPS Greek and SQLite's LIKE folds ASCII only. Several
    people usually match a surname, and fetchone() used to pick whichever row came
    first with no word of the others; the most-emailed match wins, and the caller
    is told how many there were. person_context and meeting_prep both resolve
    through here, so one name means one person in both.
    """
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn
    if not any(ch.isalnum() for ch in search_fold(name_or_email)):
        # '%%' matched everyone, so an empty name (a trailing comma in a list of
        # attendees) resolved to the most-emailed person; '%' and '_' are LIKE
        # wildcards, and '.' ends every initial.
        return None, 0, []
    if "@" in name_or_email:
        row = conn.execute(
            "SELECT id, name, email, role, department FROM people WHERE LOWER(email) = LOWER(?)",
            (name_or_email.strip(),),
        ).fetchone()
        return row, (1 if row else 0), []
    # At the start of a word, as a search matches (greek._phrase_pattern, which
    # sb_match applies to a phrase when given no words or tokens): anywhere inside
    # a name, 'AI' resolved to a forename holding it and 'EU' to a bank, and recall
    # attached that dossier to topic queries. The LIKE only rules rows out first.
    phrase = " ".join(search_fold(name_or_email).split())
    # Only the head of the phrase is anchored when its last word is under three
    # letters, because that word is an initial ('Surname T') and _phrase_pattern
    # would make it a whole word; a query of one short word stays whole ('AI').
    head, _, tail = phrase.rpartition(" ")
    anchor = head if head and len(tail) < 3 and tail.isalpha() else phrase
    # And the local part of an address, where one word of four letters or more
    # starts a token or follows a one-letter initial ('jexample'), so a name
    # stored only in Greek is found by the Latin surname its address spells.
    # recall turns that off for the dossier it attaches on its own: there a topic
    # word ('data', 'info') started some mailbox's local part and brought it in.
    local = phrase if local_part and re.fullmatch(r"[a-z0-9]{4,}", phrase) else ""
    where = r"""
        (sb_fold(p.name) LIKE '%' || :phrase || '%' AND sb_match(p.name, :anchor, '', '') > 0)
        OR (:local <> '' AND (LOWER(p.email) LIKE :local || '%@%'
                              OR LOWER(p.email) LIKE '_' || :local || '%@%'
                              OR LOWER(p.email) LIKE '%.' || :local || '%@%'
                              OR LOWER(p.email) LIKE '%-' || :local || '%@%'
                              OR LOWER(p.email) LIKE '%\_' || :local || '%@%' ESCAPE '\'))
    """
    args = {"phrase": phrase, "anchor": anchor, "local": local}
    candidates = conn.execute(
        f"""
        SELECT p.id, p.name, p.email, p.role, p.department
        FROM people p
        WHERE {where}
        ORDER BY (SELECT COUNT(*) FROM email_people ep WHERE ep.person_id = p.id) DESC, p.id
        LIMIT 4
        """,
        args,
    ).fetchall()
    if not candidates:
        return None, 0, []
    match_count = conn.execute(f"SELECT COUNT(*) FROM people p WHERE {where}", args).fetchone()[0]
    others = [{"name": c["name"], "email": c["email"]} for c in candidates[1:]]
    return candidates[0], match_count, others


def get_person_context(
    conn: sqlite3.Connection,
    name_or_email: str,
    days: int = 365,
    limit: int = DEFAULT_CONTEXT_LIMIT,
    *,
    local_part: bool = True,
) -> dict:
    """Return rich context for a person.

    Args:
        conn: Database connection
        name_or_email: Person's name (partial match) or email address
        days: Number of days to look back
        limit: Max rows per list (topics, decisions, open_actions). Each list is
            accompanied by a `<name>_total` giving the unbounded count.
            recent_emails holds at most min(limit, 10), with no total.
        local_part: Also match a name against the start of an address's local
            part (see resolve_person).

    Returns:
        Dict with person info, email_count, recent_emails, topics,
        sentiment_distribution, decisions, open_actions, communication_pattern
    """
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    person_row, match_count, other_candidates = resolve_person(
        conn, name_or_email, local_part=local_part
    )

    if not person_row:
        return {
            "person": None,
            "match_count": 0,
            "other_candidates": [],
            "email_count": 0,
            "recent_emails": [],
            "topics": [],
            "topics_total": 0,
            "sentiment_distribution": {},
            "decisions": [],
            "decisions_total": 0,
            "open_actions": [],
            "open_actions_total": 0,
            "communication_pattern": {},
            "last_met": None,
            "next_meeting": None,
            "meeting_count_30d": None,
            "teams": _no_teams(),
            "whatsapp": _no_whatsapp(),
        }

    person = dict(person_row)
    person_id = person["id"]

    # Email count in period
    email_count = conn.execute(
        """
        SELECT COUNT(DISTINCT e.id) as cnt
        FROM emails e
        JOIN email_people ep ON e.id = ep.email_id
        WHERE ep.person_id = ? AND e.date_received >= ?
    """,
        (person_id, cutoff),
    ).fetchone()["cnt"]

    # One row per email below, however many roles the person holds on it:
    # email_people is keyed by role too, and the model writes free-text roles
    # ('recipient', 'FYI', 'recipient, FYI'), so about 30% of (email, person)
    # pairs have several rows. Joined through it, the top correspondent's
    # decisions_total read 132K against 59K real, the sentiment counts summed to
    # twice email_count, and the capped lists were about half repeats.

    # Recent emails. The role shown is one of theirs, sender first, as in
    # query_by_person.
    recent_emails = [
        dict(r)
        for r in conn.execute(
            """
        SELECT e.date_received as date, e.subject, e.summary,
            (SELECT ep.role_in_email FROM email_people ep
              WHERE ep.email_id = e.id AND ep.person_id = ?
              ORDER BY ep.role_in_email <> 'sender', ep.role_in_email LIMIT 1) as role_in_email
        FROM emails e
        WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
          AND e.date_received >= ?
        ORDER BY e.date_received DESC
        LIMIT ?
    """,
            (person_id, person_id, cutoff, min(limit, 10)),
        ).fetchall()
    ]

    # Topics they're involved in
    topics = [
        dict(r)
        for r in conn.execute(
            """
        SELECT t.display_name as topic, COUNT(*) as count
        FROM topics t
        JOIN email_topics et ON t.id = et.topic_id
        JOIN emails e ON et.email_id = e.id
        WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
          AND e.date_received >= ?
        GROUP BY t.id
        ORDER BY count DESC
        LIMIT ?
    """,
            (person_id, cutoff, limit),
        ).fetchall()
    ]
    topics_total = conn.execute(
        """
        SELECT COUNT(DISTINCT t.id) as cnt
        FROM topics t
        JOIN email_topics et ON t.id = et.topic_id
        JOIN emails e ON et.email_id = e.id
        WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
          AND e.date_received >= ?
    """,
        (person_id, cutoff),
    ).fetchone()["cnt"]

    # Sentiment distribution
    sentiment_rows = conn.execute(
        """
        SELECT e.sentiment, COUNT(*) as count
        FROM emails e
        WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
          AND e.date_received >= ? AND e.sentiment IS NOT NULL
        GROUP BY e.sentiment
    """,
        (person_id, cutoff),
    ).fetchall()
    sentiment_distribution = {r["sentiment"]: r["count"] for r in sentiment_rows}

    # Decisions, newest first by their own date or their email's (src/store/ordering.py)
    decisions = [
        dict(r)
        for r in conn.execute(
            f"""
        SELECT d.decision, d.decided_by, {decision_dates(EMAIL_DATE)}
        FROM decisions d
        JOIN emails e ON d.email_id = e.id
        WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
          AND e.date_received >= ?
        ORDER BY {decision_order(EMAIL_DATE)}
        LIMIT ?
    """,
            (person_id, cutoff, limit),
        ).fetchall()
    ]
    decisions_total = conn.execute(
        """
        SELECT COUNT(*) as cnt
        FROM decisions d
        JOIN emails e ON d.email_id = e.id
        WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
          AND e.date_received >= ?
    """,
        (person_id, cutoff),
    ).fetchone()["cnt"]

    # Open action items, the actionable first
    open_actions = [
        dict(r)
        for r in conn.execute(
            f"""
        SELECT a.task, a.owner, a.deadline, a.status, {parent_dates(EMAIL_DATE)}
        FROM action_items a
        JOIN emails e ON a.email_id = e.id
        WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
          AND a.status = 'open' AND e.date_received >= ?
        ORDER BY {action_order(EMAIL_DATE)}
        LIMIT ?
    """,
            (person_id, cutoff, limit),
        ).fetchall()
    ]
    open_actions_total = conn.execute(
        """
        SELECT COUNT(*) as cnt
        FROM action_items a
        JOIN emails e ON a.email_id = e.id
        WHERE e.id IN (SELECT email_id FROM email_people WHERE person_id = ?)
          AND a.status = 'open' AND e.date_received >= ?
    """,
        (person_id, cutoff),
    ).fetchone()["cnt"]

    # Communication pattern
    pattern_row = conn.execute(
        """
        SELECT
            MIN(e.date_received) as first_email_date,
            MAX(e.date_received) as last_email_date,
            COUNT(DISTINCT e.id) as total
        FROM emails e
        JOIN email_people ep ON e.id = ep.email_id
        WHERE ep.person_id = ?
    """,
        (person_id,),
    ).fetchone()

    communication_pattern = {}
    if pattern_row and pattern_row["first_email_date"]:
        # date_received column mixes naive and tz-aware ISO strings (some rows
        # end in "Z", others have no offset). Strip tzinfo from both sides so
        # the subtraction doesn't raise "can't subtract offset-naive and
        # offset-aware datetimes".
        first = datetime.fromisoformat(pattern_row["first_email_date"]).replace(tzinfo=None)
        last = datetime.fromisoformat(pattern_row["last_email_date"]).replace(tzinfo=None)
        weeks = max((last - first).days / 7, 1)
        communication_pattern = {
            "first_email_date": pattern_row["first_email_date"],
            "last_email_date": pattern_row["last_email_date"],
            "avg_emails_per_week": round(pattern_row["total"] / weeks, 1),
        }

    # Calendar: last met, next meeting, meeting frequency. An attendee row is this
    # person by resolved person_id, else by address, else by folded name when the
    # row resolved to no one (an invite from an address not on record, such as a
    # personal one). Only unresolved rows are folded, about a tenth of them:
    # folding every attendee name in Python cost a scan of every past event for
    # anyone who never attended one. Each IN subquery runs once per statement.
    # The name must have two words or more: a blank name matched every
    # unresolved attendee, and one word ('ΝΙΚΟΣ', 'Info') fits strangers' invites.
    # "Now" is rendered with a 'T' like start_at, not datetime('now')'s space: 'T'
    # sorts above ' ', so every start on the current UTC day read as still to
    # come, and a meeting this morning was next_meeting rather than last_met.
    # The bare column is compared so idx_calendar_start still serves the query.
    # A cancelled event, by Outlook or because Outlook no longer lists it, is
    # not a meeting had or to come.
    calendar_data = {}
    email = person.get("email") or ""
    folded_name = search_fold(person["name"]).strip()
    name_pattern = f"%{folded_name}%" if len(folded_name.split()) >= 2 else ""
    attendee_args = (person["id"], email, email, name_pattern, name_pattern)

    try:
        last_met = conn.execute(
            """SELECT ce.subject, ce.start_at FROM calendar_events ce
               WHERE ce.id IN (
                   SELECT event_id FROM event_attendees
                   WHERE person_id = ?
                      OR (? <> '' AND LOWER(email) = LOWER(?))
                      OR (person_id IS NULL AND ? <> '' AND sb_fold(name) LIKE ?))
                 AND ce.is_cancelled = 0
                 AND ce.start_at < strftime('%Y-%m-%dT%H:%M:%S', 'now')
               ORDER BY ce.start_at DESC LIMIT 1""",
            attendee_args,
        ).fetchone()
        if last_met:
            calendar_data["last_met"] = {"subject": last_met[0], "date": last_met[1]}

        next_meeting = conn.execute(
            """SELECT ce.subject, ce.start_at FROM calendar_events ce
               WHERE ce.id IN (
                   SELECT event_id FROM event_attendees
                   WHERE person_id = ?
                      OR (? <> '' AND LOWER(email) = LOWER(?))
                      OR (person_id IS NULL AND ? <> '' AND sb_fold(name) LIKE ?))
                 AND ce.is_cancelled = 0
                 AND ce.start_at > strftime('%Y-%m-%dT%H:%M:%S', 'now')
               ORDER BY ce.start_at ASC LIMIT 1""",
            attendee_args,
        ).fetchone()
        if next_meeting:
            calendar_data["next_meeting"] = {
                "subject": next_meeting[0],
                "date": next_meeting[1],
            }

        meeting_count = conn.execute(
            """SELECT COUNT(*) FROM calendar_events ce
               WHERE ce.id IN (
                   SELECT event_id FROM event_attendees
                   WHERE person_id = ?
                      OR (? <> '' AND LOWER(email) = LOWER(?))
                      OR (person_id IS NULL AND ? <> '' AND sb_fold(name) LIKE ?))
                 AND ce.is_cancelled = 0
                 AND ce.start_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 days')""",
            attendee_args,
        ).fetchone()[0]
        calendar_data["meeting_count_30d"] = meeting_count
    except Exception:
        pass

    teams = _person_teams(conn, person_id, cutoff, limit)
    whatsapp = _person_whatsapp(conn, person["name"], cutoff, limit)

    del person["id"]

    return {
        "person": person,
        "match_count": match_count,
        "other_candidates": other_candidates,
        "email_count": email_count,
        "recent_emails": recent_emails,
        "topics": topics,
        "topics_total": topics_total,
        "sentiment_distribution": sentiment_distribution,
        "decisions": decisions,
        "decisions_total": decisions_total,
        "open_actions": open_actions,
        "open_actions_total": open_actions_total,
        "communication_pattern": communication_pattern,
        "last_met": calendar_data.get("last_met"),
        "next_meeting": calendar_data.get("next_meeting"),
        "meeting_count_30d": calendar_data.get("meeting_count_30d"),
        "teams": teams,
        "whatsapp": whatsapp,
    }


def _no_whatsapp() -> dict:
    """_person_whatsapp's answer for someone with no WhatsApp activity, a fresh dict each time."""
    return {
        "message_count": 0,
        "last_message_at": None,
        "recent_threads": [],
        "recent_threads_total": 0,
    }


def _person_whatsapp(conn: sqlite3.Connection, name: str | None, cutoff: str, limit: int) -> dict:
    """What the person wrote on WhatsApp since `cutoff`, matched by name.

    WhatsApp knows a sender by phone number and by the name the phone's contacts
    give them, never by email, so there is no person id to join on as Teams has.
    A message counts when its sender's name holds every word of the person's name,
    in any order, folded; and only for a name of two words or more, because a
    first name alone would claim every namesake's messages. sent_at carries a UTC
    'Z' and the cutoff is local, which moves the window's edge by hours, not days.
    """
    words = search_words(name)
    if len(words) < 2:
        return _no_whatsapp()
    args = (cutoff, search_phrase(name), "\x1f".join(words), PHRASE_MATCH)

    def match(p: str = "") -> str:
        return f"{p}is_from_me = 0 AND {p}sent_at >= ? AND sb_match({p}sender_name, ?, ?, '') >= ?"

    try:
        count, last = conn.execute(
            f"SELECT COUNT(*), MAX(sent_at) FROM whatsapp_messages WHERE {match()}", args
        ).fetchone()
        threads = conn.execute(
            f"""
            SELECT t.id AS thread_id, t.title, c.name AS chat, COUNT(*) AS messages,
                   MAX(m.sent_at) AS last_message_at
            FROM whatsapp_messages m
            JOIN whatsapp_threads t ON t.id = m.thread_id
            JOIN whatsapp_chats c ON c.id = t.chat_id
            WHERE {match("m.")}
            GROUP BY t.id
            ORDER BY last_message_at DESC
            LIMIT ?
            """,
            (*args, limit),
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(DISTINCT thread_id) FROM whatsapp_messages WHERE {match()} "
            "AND thread_id IS NOT NULL",
            args,
        ).fetchone()[0]
    except sqlite3.OperationalError:  # a store from before v26
        return _no_whatsapp()
    return {
        "message_count": count,
        "last_message_at": last,
        "recent_threads": [
            {
                "thread_id": r["thread_id"],
                "title": r["title"],
                "chat": r["chat"],
                "messages": r["messages"],
                "last_message_at": r["last_message_at"],
            }
            for r in threads
        ],
        "recent_threads_total": total,
    }


def _no_teams() -> dict:
    """_person_teams' answer for someone with no Teams activity, a fresh dict each time."""
    return {
        "message_count": 0,
        "last_message_at": None,
        "recent_threads": [],
        "recent_threads_total": 0,
    }


def _person_teams(conn: sqlite3.Connection, person_id: int, cutoff: str, limit: int) -> dict:
    """What the person wrote in Teams since `cutoff`, and the threads they wrote in.

    By teams_messages.sender_person_id, which links about 83% of messages to a
    person; system messages are not theirs, nor are call events ('Event/Call'),
    which arrive as ordinary messages from the caller. composed_at carries a UTC
    'Z' and the cutoff is local, which moves the window's edge by hours, not days.
    A chat with neither a team nor a topic (1:1 chats, most group chats) is named
    by its kind.
    """
    try:
        count, last = conn.execute(
            "SELECT COUNT(*), MAX(composed_at) FROM teams_messages "
            "WHERE sender_person_id = ? AND is_system = 0 "
            "AND COALESCE(message_type, '') NOT LIKE 'Event/%' AND composed_at >= ?",
            (person_id, cutoff),
        ).fetchone()
        threads = conn.execute(
            """
            SELECT t.id AS thread_id, t.title, c.topic, c.team_name, c.chat_kind,
                   COUNT(*) AS messages, MAX(m.composed_at) AS last_message_at
            FROM teams_messages m
            JOIN teams_threads t ON t.id = m.thread_id
            JOIN teams_chats c ON c.id = t.chat_id
            WHERE m.sender_person_id = ? AND m.is_system = 0
              AND COALESCE(m.message_type, '') NOT LIKE 'Event/%' AND m.composed_at >= ?
            GROUP BY t.id
            ORDER BY last_message_at DESC
            LIMIT ?
            """,
            (person_id, cutoff, limit),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(DISTINCT thread_id) FROM teams_messages "
            "WHERE sender_person_id = ? AND is_system = 0 "
            "AND COALESCE(message_type, '') NOT LIKE 'Event/%' AND composed_at >= ? "
            "AND thread_id IS NOT NULL",
            (person_id, cutoff),
        ).fetchone()[0]
    except sqlite3.OperationalError:  # a store without the Teams tables
        return _no_teams()
    return {
        "message_count": count,
        "last_message_at": last,
        "recent_threads": [
            {
                "thread_id": r["thread_id"],
                "title": r["title"],
                "chat": " / ".join(p for p in (r["team_name"], r["topic"]) if p) or r["chat_kind"],
                "messages": r["messages"],
                "last_message_at": r["last_message_at"],
            }
            for r in threads
        ],
        "recent_threads_total": total,
    }


def get_topic_context(
    conn: sqlite3.Connection,
    topic: str,
    days: int = 365,
    limit: int = DEFAULT_CONTEXT_LIMIT,
) -> dict:
    """Return context for a topic.

    Args:
        conn: Database connection
        topic: Topic name (partial match)
        days: Number of days to look back
        limit: Max rows per list (key_people, decisions, open_actions,
            key_facts). Each list ships a `<name>_total` with the true count.

    Returns:
        Dict with topic info, email_count, recent_emails, key_people,
        decisions, open_actions, key_facts
    """

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    # Topics are stored normalized (lower case, accents stripped), so the query
    # must be too: an accented query matched nothing. Among partial matches an
    # exact name wins, then the most-used topic, rather than whichever came first.
    topic_normalized = normalize_topic(topic)

    # Find topic. With no letter or digit there is none: '   ', '-' and '.'
    # normalize to '', and LIKE '%%' matched every topic, so the most-used one
    # came back as if it were the answer.
    topic_row = (
        conn.execute(
            """
            SELECT t.id, t.name, t.display_name FROM topics t
            WHERE t.name LIKE ?
            ORDER BY (t.name = ?) DESC,
                     (SELECT COUNT(*) FROM email_topics et WHERE et.topic_id = t.id) DESC, t.id
            LIMIT 1
            """,
            (f"%{topic_normalized}%", topic_normalized),
        ).fetchone()
        if any(ch.isalnum() for ch in topic_normalized)
        else None
    )

    if not topic_row:
        return {
            "topic": None,
            "email_count": 0,
            "recent_emails": [],
            "key_people": [],
            "key_people_total": 0,
            "decisions": [],
            "decisions_total": 0,
            "open_actions": [],
            "open_actions_total": 0,
            "key_facts": [],
            "key_facts_total": 0,
        }

    topic_info = {"name": topic_row["name"], "display_name": topic_row["display_name"]}
    topic_id = topic_row["id"]

    # Email count
    email_count = conn.execute(
        """
        SELECT COUNT(DISTINCT e.id) as cnt
        FROM emails e
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND e.date_received >= ?
    """,
        (topic_id, cutoff),
    ).fetchone()["cnt"]

    # Recent emails
    recent_emails = [
        dict(r)
        for r in conn.execute(
            """
        SELECT e.date_received as date, e.subject, e.summary, e.sender_name as sender
        FROM emails e
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND e.date_received >= ?
        ORDER BY e.date_received DESC
        LIMIT 10
    """,
            (topic_id, cutoff),
        ).fetchall()
    ]

    # Key people
    key_people = [
        dict(r)
        for r in conn.execute(
            """
        SELECT p.name, COUNT(DISTINCT e.id) as email_count
        FROM people p
        JOIN email_people ep ON p.id = ep.person_id
        JOIN email_topics et ON ep.email_id = et.email_id
        JOIN emails e ON ep.email_id = e.id
        WHERE et.topic_id = ? AND e.date_received >= ?
        GROUP BY p.id
        ORDER BY email_count DESC
        LIMIT ?
    """,
            (topic_id, cutoff, limit),
        ).fetchall()
    ]
    key_people_total = conn.execute(
        """
        SELECT COUNT(DISTINCT p.id) as cnt
        FROM people p
        JOIN email_people ep ON p.id = ep.person_id
        JOIN email_topics et ON ep.email_id = et.email_id
        JOIN emails e ON ep.email_id = e.id
        WHERE et.topic_id = ? AND e.date_received >= ?
    """,
        (topic_id, cutoff),
    ).fetchone()["cnt"]

    # Decisions, newest first by their own date or their email's (src/store/ordering.py)
    decisions = [
        dict(r)
        for r in conn.execute(
            f"""
        SELECT d.decision, d.decided_by, {decision_dates(EMAIL_DATE)}
        FROM decisions d
        JOIN emails e ON d.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND e.date_received >= ?
        ORDER BY {decision_order(EMAIL_DATE)}
        LIMIT ?
    """,
            (topic_id, cutoff, limit),
        ).fetchall()
    ]
    decisions_total = conn.execute(
        """
        SELECT COUNT(*) as cnt
        FROM decisions d
        JOIN emails e ON d.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND e.date_received >= ?
    """,
        (topic_id, cutoff),
    ).fetchone()["cnt"]

    # Open action items, the actionable first
    open_actions = [
        dict(r)
        for r in conn.execute(
            f"""
        SELECT a.task, a.owner, a.deadline, a.status, {parent_dates(EMAIL_DATE)}
        FROM action_items a
        JOIN emails e ON a.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND a.status = 'open' AND e.date_received >= ?
        ORDER BY {action_order(EMAIL_DATE)}
        LIMIT ?
    """,
            (topic_id, cutoff, limit),
        ).fetchall()
    ]
    open_actions_total = conn.execute(
        """
        SELECT COUNT(*) as cnt
        FROM action_items a
        JOIN emails e ON a.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND a.status = 'open' AND e.date_received >= ?
    """,
        (topic_id, cutoff),
    ).fetchone()["cnt"]

    # Key facts, newest email first: with no ORDER BY the cap kept whichever came first
    key_facts = [
        dict(r)
        for r in conn.execute(
            f"""
        SELECT kf.fact, {parent_dates(EMAIL_DATE)}
        FROM key_facts kf
        JOIN emails e ON kf.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND e.date_received >= ?
        ORDER BY {fact_order(EMAIL_DATE)}
        LIMIT ?
    """,
            (topic_id, cutoff, limit),
        ).fetchall()
    ]
    key_facts_total = conn.execute(
        """
        SELECT COUNT(*) as cnt
        FROM key_facts kf
        JOIN emails e ON kf.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND e.date_received >= ?
    """,
        (topic_id, cutoff),
    ).fetchone()["cnt"]

    return {
        "topic": topic_info,
        "email_count": email_count,
        "recent_emails": recent_emails,
        "key_people": key_people,
        "key_people_total": key_people_total,
        "decisions": decisions,
        "decisions_total": decisions_total,
        "open_actions": open_actions,
        "open_actions_total": open_actions_total,
        "key_facts": key_facts,
        "key_facts_total": key_facts_total,
    }


def get_conversation_context(conn: sqlite3.Connection, email_id: int) -> dict:
    """Return context for a conversation thread.

    Args:
        conn: Database connection
        email_id: ID of the email

    Returns:
        Dict with email, thread, participants, decisions, action_items
    """

    # Get the email
    email_row = conn.execute(
        """
        SELECT id, date_received as date, subject, summary, sender_name as sender
        FROM emails WHERE id = ?
    """,
        (email_id,),
    ).fetchone()

    if not email_row:
        return {
            "email": None,
            "thread": [],
            "participants": [],
            "decisions": [],
            "action_items": [],
        }

    email = dict(email_row)

    # The thread as email_thread and search know it (query._THREAD): a News day or
    # the hash every blank-subject email without references shares is no thread.
    from src.store.query import query_thread

    thread = [email]
    try:
        rows = query_thread(conn, email_id, limit=-1)
        if rows:
            thread = [
                {
                    "id": r["email_id"],
                    "date": r["date"],
                    "subject": r["subject"],
                    "summary": r["summary"],
                    "sender": r["sender_name"],
                }
                for r in rows
            ]
    except sqlite3.OperationalError:
        pass

    # Get thread email IDs
    thread_ids = [e["id"] for e in thread]

    def _query_by_ids(sql_template: str, ids: list) -> list[dict]:
        """Run a query with an IN clause using parameterized placeholders."""
        ph = ",".join(["?"] * len(ids))
        return [dict(r) for r in conn.execute(sql_template.replace("__PH__", ph), ids).fetchall()]

    # Participants
    participants = _query_by_ids(
        """
        SELECT DISTINCT p.name, p.email, ep.role_in_email
        FROM people p
        JOIN email_people ep ON p.id = ep.person_id
        WHERE ep.email_id IN (__PH__)
    """,
        thread_ids,
    )

    # Decisions, oldest first as the thread reads, by their own date or their email's
    decisions = _query_by_ids(
        f"""
        SELECT d.decision, d.decided_by, {decision_dates(EMAIL_DATE)}
        FROM decisions d
        JOIN emails e ON e.id = d.email_id
        WHERE d.email_id IN (__PH__)
        ORDER BY {decision_date(EMAIL_DATE)} ASC, d.id ASC
    """,
        thread_ids,
    )

    # Action items, the actionable first
    action_items = _query_by_ids(
        f"""
        SELECT a.task, a.owner, a.deadline, a.status, {parent_dates(EMAIL_DATE)}
        FROM action_items a
        JOIN emails e ON e.id = a.email_id
        WHERE a.email_id IN (__PH__)
        ORDER BY {action_order(EMAIL_DATE)}
    """,
        thread_ids,
    )

    return {
        "email": email,
        "thread": thread,
        "participants": participants,
        "decisions": decisions,
        "action_items": action_items,
    }
