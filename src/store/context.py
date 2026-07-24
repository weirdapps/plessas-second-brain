"""High-level context retrieval API for external consumers.

Provides rich context functions for the email-handler plugin and /recall skill
to retrieve person, topic, conversation, and decision context from the knowledge store.
"""

import sqlite3
from datetime import datetime, timedelta


def get_person_context(conn: sqlite3.Connection, name_or_email: str, days: int = 365) -> dict:
    """Return rich context for a person.

    Args:
        conn: Database connection
        name_or_email: Person's name (partial match) or email address
        days: Number of days to look back

    Returns:
        Dict with person info, email_count, recent_emails, topics,
        sentiment_distribution, decisions, open_actions, communication_pattern
    """

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    # Find person
    if "@" in name_or_email:
        person_row = conn.execute(
            "SELECT id, name, email, role, department FROM people WHERE LOWER(email) = LOWER(?)",
            (name_or_email.strip(),),
        ).fetchone()
    else:
        person_row = conn.execute(
            "SELECT id, name, email, role, department FROM people WHERE name LIKE ?",
            (f"%{name_or_email}%",),
        ).fetchone()

    if not person_row:
        return {
            "person": None,
            "email_count": 0,
            "recent_emails": [],
            "topics": [],
            "sentiment_distribution": {},
            "decisions": [],
            "open_actions": [],
            "communication_pattern": {},
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

    # Recent emails
    recent_emails = [
        dict(r)
        for r in conn.execute(
            """
        SELECT e.date_received as date, e.subject, e.summary, ep.role_in_email
        FROM emails e
        JOIN email_people ep ON e.id = ep.email_id
        WHERE ep.person_id = ? AND e.date_received >= ?
        ORDER BY e.date_received DESC
        LIMIT 10
    """,
            (person_id, cutoff),
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
        JOIN email_people ep ON et.email_id = ep.email_id
        JOIN emails e ON et.email_id = e.id
        WHERE ep.person_id = ? AND e.date_received >= ?
        GROUP BY t.id
        ORDER BY count DESC
    """,
            (person_id, cutoff),
        ).fetchall()
    ]

    # Sentiment distribution
    sentiment_rows = conn.execute(
        """
        SELECT e.sentiment, COUNT(*) as count
        FROM emails e
        JOIN email_people ep ON e.id = ep.email_id
        WHERE ep.person_id = ? AND e.date_received >= ? AND e.sentiment IS NOT NULL
        GROUP BY e.sentiment
    """,
        (person_id, cutoff),
    ).fetchall()
    sentiment_distribution = {r["sentiment"]: r["count"] for r in sentiment_rows}

    # Decisions
    decisions = [
        dict(r)
        for r in conn.execute(
            """
        SELECT d.decision, d.decided_by, d.decision_date as date
        FROM decisions d
        JOIN emails e ON d.email_id = e.id
        JOIN email_people ep ON e.id = ep.email_id
        WHERE ep.person_id = ? AND e.date_received >= ?
        ORDER BY d.decision_date DESC
    """,
            (person_id, cutoff),
        ).fetchall()
    ]

    # Open action items
    open_actions = [
        dict(r)
        for r in conn.execute(
            """
        SELECT a.task, a.owner, a.deadline, a.status
        FROM action_items a
        JOIN emails e ON a.email_id = e.id
        JOIN email_people ep ON e.id = ep.email_id
        WHERE ep.person_id = ? AND a.status = 'open' AND e.date_received >= ?
        ORDER BY a.deadline IS NULL, a.deadline ASC
    """,
            (person_id, cutoff),
        ).fetchall()
    ]

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

    # Calendar: last met, next meeting, meeting frequency
    calendar_data = {}
    name_lower = f"%{person['name'].lower()}%"
    email_lower = f"%{person['email'].lower()}%" if person.get("email") else "%@invalid%"

    try:
        last_met = conn.execute(
            """SELECT ce.subject, ce.start_at FROM calendar_events ce
               JOIN event_attendees ea ON ea.event_id = ce.id
               WHERE (LOWER(ea.name) LIKE ? OR LOWER(ea.email) LIKE ?)
                 AND ce.start_at < datetime('now')
               ORDER BY ce.start_at DESC LIMIT 1""",
            (name_lower, email_lower),
        ).fetchone()
        if last_met:
            calendar_data["last_met"] = {"subject": last_met[0], "date": last_met[1]}

        next_meeting = conn.execute(
            """SELECT ce.subject, ce.start_at FROM calendar_events ce
               JOIN event_attendees ea ON ea.event_id = ce.id
               WHERE (LOWER(ea.name) LIKE ? OR LOWER(ea.email) LIKE ?)
                 AND ce.start_at > datetime('now')
               ORDER BY ce.start_at ASC LIMIT 1""",
            (name_lower, email_lower),
        ).fetchone()
        if next_meeting:
            calendar_data["next_meeting"] = {
                "subject": next_meeting[0],
                "date": next_meeting[1],
            }

        meeting_count = conn.execute(
            """SELECT COUNT(*) FROM calendar_events ce
               JOIN event_attendees ea ON ea.event_id = ce.id
               WHERE (LOWER(ea.name) LIKE ? OR LOWER(ea.email) LIKE ?)
                 AND ce.start_at >= datetime('now', '-30 days')""",
            (name_lower, email_lower),
        ).fetchone()[0]
        calendar_data["meeting_count_30d"] = meeting_count
    except Exception:
        pass

    del person["id"]

    return {
        "person": person,
        "email_count": email_count,
        "recent_emails": recent_emails,
        "topics": topics,
        "sentiment_distribution": sentiment_distribution,
        "decisions": decisions,
        "open_actions": open_actions,
        "communication_pattern": communication_pattern,
        "last_met": calendar_data.get("last_met"),
        "next_meeting": calendar_data.get("next_meeting"),
        "meeting_count_30d": calendar_data.get("meeting_count_30d"),
    }


def get_topic_context(conn: sqlite3.Connection, topic: str, days: int = 365) -> dict:
    """Return context for a topic.

    Args:
        conn: Database connection
        topic: Topic name (partial match)
        days: Number of days to look back

    Returns:
        Dict with topic info, email_count, recent_emails, key_people,
        decisions, open_actions, key_facts
    """

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    topic_normalized = topic.strip().lower()

    # Find topic
    topic_row = conn.execute(
        "SELECT id, name, display_name FROM topics WHERE name LIKE ?",
        (f"%{topic_normalized}%",),
    ).fetchone()

    if not topic_row:
        return {
            "topic": None,
            "email_count": 0,
            "recent_emails": [],
            "key_people": [],
            "decisions": [],
            "open_actions": [],
            "key_facts": [],
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
    """,
            (topic_id, cutoff),
        ).fetchall()
    ]

    # Decisions
    decisions = [
        dict(r)
        for r in conn.execute(
            """
        SELECT d.decision, d.decided_by, d.decision_date as date
        FROM decisions d
        JOIN emails e ON d.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND e.date_received >= ?
        ORDER BY d.decision_date DESC
    """,
            (topic_id, cutoff),
        ).fetchall()
    ]

    # Open action items
    open_actions = [
        dict(r)
        for r in conn.execute(
            """
        SELECT a.task, a.owner, a.deadline, a.status
        FROM action_items a
        JOIN emails e ON a.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND a.status = 'open' AND e.date_received >= ?
        ORDER BY a.deadline IS NULL, a.deadline ASC
    """,
            (topic_id, cutoff),
        ).fetchall()
    ]

    # Key facts
    key_facts = [
        dict(r)
        for r in conn.execute(
            """
        SELECT kf.fact
        FROM key_facts kf
        JOIN emails e ON kf.email_id = e.id
        JOIN email_topics et ON e.id = et.email_id
        WHERE et.topic_id = ? AND e.date_received >= ?
    """,
            (topic_id, cutoff),
        ).fetchall()
    ]

    return {
        "topic": topic_info,
        "email_count": email_count,
        "recent_emails": recent_emails,
        "key_people": key_people,
        "decisions": decisions,
        "open_actions": open_actions,
        "key_facts": key_facts,
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

    # Try to get thread via conversation_id if column exists
    thread = [email]
    try:
        conv_row = conn.execute(
            "SELECT conversation_id FROM emails WHERE id = ?", (email_id,)
        ).fetchone()
        if conv_row and conv_row["conversation_id"]:
            thread = [
                dict(r)
                for r in conn.execute(
                    """
                SELECT id, date_received as date, subject, summary, sender_name as sender
                FROM emails WHERE conversation_id = ?
                ORDER BY date_received ASC
            """,
                    (conv_row["conversation_id"],),
                ).fetchall()
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

    # Decisions
    decisions = _query_by_ids(
        """
        SELECT d.decision, d.decided_by, d.decision_date as date
        FROM decisions d
        WHERE d.email_id IN (__PH__)
        ORDER BY d.decision_date ASC
    """,
        thread_ids,
    )

    # Action items
    action_items = _query_by_ids(
        """
        SELECT a.task, a.owner, a.deadline, a.status
        FROM action_items a
        WHERE a.email_id IN (__PH__)
        ORDER BY a.deadline IS NULL, a.deadline ASC
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


def get_recent_decisions(conn: sqlite3.Connection, days: int = 365, limit: int = 20) -> list[dict]:
    """Return recent decisions with context.

    Args:
        conn: Database connection
        days: Number of days to look back
        limit: Maximum number of results

    Returns:
        List of dicts with decision, decided_by, date, email_subject, email_date, topics
    """

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    results = [
        dict(r)
        for r in conn.execute(
            """
        SELECT
            d.decision,
            d.decided_by,
            COALESCE(d.decision_date, e.date_received) as date,
            e.subject as email_subject,
            e.date_received as email_date,
            GROUP_CONCAT(t.display_name, ', ') as topics
        FROM decisions d
        JOIN emails e ON d.email_id = e.id
        LEFT JOIN email_topics et ON e.id = et.email_id
        LEFT JOIN topics t ON et.topic_id = t.id
        WHERE e.date_received >= ?
        GROUP BY d.id
        ORDER BY date DESC
        LIMIT ?
    """,
            (cutoff, limit),
        ).fetchall()
    ]

    return results
