"""Query interface for the second-brain knowledge store.

Provides rich query functions for the /recall skill to search emails, topics,
people, decisions, and action items. All queries return structured results
optimized for CLI display.
"""

import sqlite3
from typing import Any

from src.config import USER_EMAIL_PATTERN


def _sanitize_fts5_query(keyword: str) -> str:
    """Sanitize a keyword for safe FTS5 MATCH queries.

    FTS5 has special syntax characters (*, ", OR, AND, NOT, NEAR, -, ^, :)
    that can cause unexpected behavior or errors. This splits the input on
    whitespace and wraps each token in double quotes — defanging operators
    while preserving FTS5's implicit AND semantics across tokens (every token
    must appear somewhere in the row).

    Examples:
        "Mylonas"             -> '"Mylonas"'
        "earnings release"    -> '"earnings" "release"'   (both required)
        "ACME Q1 2026"         -> '"ACME" "Q1" "2026"'      (all three required)
        ""                    -> '""'                     (matches nothing)
    """
    tokens = keyword.split()
    if not tokens:
        return '""'
    quoted = []
    for token in tokens:
        # Escape any internal double quotes per FTS5 phrase syntax
        escaped = token.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " ".join(quoted)


def _has_attachment_fts(conn: sqlite3.Connection) -> bool:
    """Whether attachment_content_fts exists in this DB.

    Older DBs created before the attachment-extraction feature lack it;
    keyword search must skip the attachment branch in that case.
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachment_content_fts'"
        ).fetchone()
        is not None
    )


def query_thread(conn: sqlite3.Connection, email_id: int, limit: int = 50) -> list[dict]:
    """Get the full conversation thread for an email.

    Given an email_id, finds its conversation_id and returns all emails
    in that conversation ordered chronologically.

    Args:
        conn: Database connection
        email_id: ID of any email in the thread
        limit: Maximum number of results to return

    Returns:
        List of dicts with keys: email_id, date, subject, summary,
        sender_name, sender_address, sentiment
    """
    # Find conversation_id for the given email
    cursor = conn.execute("SELECT conversation_id FROM emails WHERE id = ?", (email_id,))
    row = cursor.fetchone()
    if not row or not row["conversation_id"]:
        return []

    conversation_id = row["conversation_id"]

    # Get all emails in this conversation
    cursor = conn.execute(
        """
        SELECT
            id as email_id,
            date_received as date,
            subject,
            summary,
            sender_name,
            sender_address,
            sentiment
        FROM emails
        WHERE conversation_id = ?
        ORDER BY date_received ASC
        LIMIT ?
        """,
        (conversation_id, limit),
    )
    return [dict(r) for r in cursor.fetchall()]


def query_by_person(conn: sqlite3.Connection, name_or_email: str, limit: int = 20) -> list[dict]:
    """Find emails involving a person (as sender, recipient, or mentioned in people_roles).

    Searches by partial name match (case-insensitive) or exact email address match.

    Args:
        conn: Database connection
        name_or_email: Person's name (partial match OK) or email address
        limit: Maximum number of results to return

    Returns:
        List of dicts with keys: email_id, date, subject, summary, person_role, sentiment
    """
    # Determine if searching by email or name
    if "@" in name_or_email:
        # Search by email address
        query = """
            SELECT DISTINCT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                ep.role_in_email as person_role,
                e.sentiment
            FROM emails e
            JOIN email_people ep ON e.id = ep.email_id
            JOIN people p ON ep.person_id = p.id
            WHERE LOWER(p.email) = LOWER(?)
            ORDER BY e.date_received DESC
            LIMIT ?
        """
        params = (name_or_email.strip(), limit)
    else:
        # Search by name (partial match)
        query = """
            SELECT DISTINCT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                ep.role_in_email as person_role,
                e.sentiment
            FROM emails e
            JOIN email_people ep ON e.id = ep.email_id
            JOIN people p ON ep.person_id = p.id
            WHERE p.name LIKE ?
            ORDER BY e.date_received DESC
            LIMIT ?
        """
        params = (f"%{name_or_email}%", limit)

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def query_by_topic(conn: sqlite3.Connection, topic: str, limit: int = 20) -> list[dict]:
    """Find emails tagged with a topic.

    Matches against normalized topic name (partial match OK).

    Args:
        conn: Database connection
        topic: Topic name (partial match OK)
        limit: Maximum number of results to return

    Returns:
        List of dicts with keys: email_id, date, subject, summary, topic
    """
    query = """
        SELECT DISTINCT
            e.id as email_id,
            e.date_received as date,
            e.subject,
            e.summary,
            t.display_name as topic
        FROM emails e
        JOIN email_topics et ON e.id = et.email_id
        JOIN topics t ON et.topic_id = t.id
        WHERE t.name LIKE ?
        ORDER BY e.date_received DESC
        LIMIT ?
    """

    # Normalize search term
    topic_normalized = topic.strip().lower()
    params = (f"%{topic_normalized}%", limit)

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def query_by_keyword(
    conn: sqlite3.Connection,
    keyword: str,
    limit: int = 20,
    search_content_only: bool = False,
) -> list[dict]:
    """Full-text search using FTS5 on email summaries, content, and key facts.

    Args:
        conn: Database connection
        keyword: Search keyword or phrase
        limit: Maximum number of results to return
        search_content_only: When True, only match against the content column

    Returns:
        List of dicts with keys: email_id, date, subject, summary, snippet, source
        where source is 'summary', 'content', or 'key_fact'
    """
    results = []
    seen_ids = set()

    # Sanitize keyword for safe FTS5 matching
    safe_keyword = _sanitize_fts5_query(keyword)

    if not search_content_only:
        # Search in email summaries
        # Rank by FTS5 BM25 relevance (ORDER BY rank), not recency. rank is only
        # comparable within a single MATCH query, so each source is ranked on its
        # own; the source-priority waterfall (summary -> content -> key_fact ->
        # attachment) plus seen_ids dedup preserves the cross-source order.
        query_summaries = """
            SELECT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                e.summary as snippet,
                'summary' as source
            FROM emails_fts
            JOIN emails e ON e.id = emails_fts.rowid
            WHERE emails_fts.summary MATCH ?
            ORDER BY rank
            LIMIT ?
        """

        cursor = conn.execute(query_summaries, (safe_keyword, limit))
        for row in cursor.fetchall():
            r = dict(row)
            seen_ids.add(r["email_id"])
            results.append(r)

    # Search in email content
    remaining = limit - len(results)
    if remaining > 0:
        query_content = """
            SELECT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                snippet(emails_fts, 1, '<b>', '</b>', '...', 30) as snippet,
                'content' as source
            FROM emails e
            JOIN emails_fts ON emails_fts.rowid = e.id
            WHERE emails_fts.content MATCH ?
            ORDER BY rank
            LIMIT ?
        """

        cursor = conn.execute(query_content, (safe_keyword, remaining))
        for row in cursor.fetchall():
            r = dict(row)
            if r["email_id"] not in seen_ids:
                seen_ids.add(r["email_id"])
                results.append(r)

    # Search in key facts (only if we haven't hit the limit and not content-only)
    remaining = limit - len(results)
    if remaining > 0 and not search_content_only:
        query_facts = """
            SELECT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                kf.fact as snippet,
                'key_fact' as source
            FROM key_facts_fts
            JOIN key_facts kf ON kf.id = key_facts_fts.rowid
            JOIN emails e ON e.id = kf.email_id
            WHERE key_facts_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """

        cursor = conn.execute(query_facts, (safe_keyword, remaining))
        for row in cursor.fetchall():
            r = dict(row)
            if r["email_id"] not in seen_ids:
                seen_ids.add(r["email_id"])
                results.append(r)

    # Search in attachment content (PDF/Office text + LLM summary).
    # Surfaces attachment-only matches as the parent email row, with
    # source='attachment' and the matched filename for caller transparency.
    remaining = limit - len(results)
    if remaining > 0 and not search_content_only and _has_attachment_fts(conn):
        query_attachments = """
            SELECT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                snippet(attachment_content_fts, 0, '<b>', '</b>', '...', 30) as snippet,
                'attachment' as source,
                a.filename as attachment_filename
            FROM attachment_content_fts
            JOIN attachment_content ac ON ac.id = attachment_content_fts.rowid
            JOIN attachments a ON a.id = ac.attachment_id
            JOIN emails e ON e.id = a.email_id
            WHERE attachment_content_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """

        cursor = conn.execute(query_attachments, (safe_keyword, remaining))
        for row in cursor.fetchall():
            r = dict(row)
            if r["email_id"] not in seen_ids:
                seen_ids.add(r["email_id"])
                results.append(r)

    # Results are relevance-ranked within each source (ORDER BY rank) and appended
    # in source-priority order (summary -> content -> key_fact -> attachment); keep
    # that order rather than re-sorting by date, so BM25 relevance is not discarded.
    # (True cross-source ranking via score fusion / RRF is a later change.)
    # Limit to requested number
    return results[:limit]


def query_by_date_range(
    conn: sqlite3.Connection, start_date: str, end_date: str, limit: int = 50
) -> list[dict]:
    """Find emails within date range (ISO 8601 strings).

    Args:
        conn: Database connection
        start_date: Start date (ISO 8601 format, e.g., '2026-03-01')
        end_date: End date (ISO 8601 format, e.g., '2026-03-31')
        limit: Maximum number of results to return

    Returns:
        List of dicts with keys: email_id, date, subject, summary, sender
    """
    query = """
        SELECT
            e.id as email_id,
            e.date_received as date,
            e.subject,
            e.summary,
            e.sender_name as sender
        FROM emails e
        WHERE DATE(e.date_received) >= DATE(?) AND DATE(e.date_received) <= DATE(?)
        ORDER BY e.date_received DESC
        LIMIT ?
    """

    cursor = conn.execute(query, (start_date, end_date, limit))
    return [dict(row) for row in cursor.fetchall()]


def query_decisions(
    conn: sqlite3.Connection,
    topic: str | None = None,
    person: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Find decisions, optionally filtered by topic or person.

    Args:
        conn: Database connection
        topic: Optional topic filter (partial match)
        person: Optional person filter (decided_by field, partial match)
        limit: Maximum number of results to return

    Returns:
        List of dicts with keys: decision_id, decision, decided_by, date,
        email_subject, topics (comma-separated)
    """
    # Build query with optional filters
    query = """
        SELECT
            d.id as decision_id,
            d.decision,
            d.decided_by,
            COALESCE(d.decision_date, e.date_received) as date,
            e.subject as email_subject,
            GROUP_CONCAT(t.display_name, ', ') as topics
        FROM decisions d
        JOIN emails e ON d.email_id = e.id
        LEFT JOIN email_topics et ON e.id = et.email_id
        LEFT JOIN topics t ON et.topic_id = t.id
    """

    where_clauses = []
    params: list[Any] = []

    if topic:
        where_clauses.append(
            "e.id IN (SELECT email_id FROM email_topics et2 JOIN topics t2 ON et2.topic_id = t2.id WHERE t2.name LIKE ?)"
        )
        params.append(f"%{topic.strip().lower()}%")

    if person:
        where_clauses.append("d.decided_by LIKE ?")
        params.append(f"%{person}%")

    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)

    query += """
        GROUP BY d.id
        ORDER BY date DESC
        LIMIT ?
    """

    params.append(limit)

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def query_action_items(
    conn: sqlite3.Connection,
    owner: str | None = None,
    status: str = "open",
    limit: int = 20,
) -> list[dict]:
    """Find action items, filtered by owner and/or status.

    Args:
        conn: Database connection
        owner: Optional owner filter (partial match)
        status: Status filter (default: 'open')
        limit: Maximum number of results to return

    Returns:
        List of dicts with keys: action_id, task, owner, deadline, status,
        email_subject, date
    """
    # Build query with filters
    query = """
        SELECT
            a.id as action_id,
            a.task,
            a.owner,
            a.deadline,
            a.status,
            e.subject as email_subject,
            e.date_received as date
        FROM action_items a
        JOIN emails e ON a.email_id = e.id
    """

    where_clauses = []
    params: list[Any] = []

    if status:
        where_clauses.append("a.status = ?")
        params.append(status)

    if owner:
        where_clauses.append("a.owner LIKE ?")
        params.append(f"%{owner}%")

    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)

    query += """
        ORDER BY a.deadline IS NULL, a.deadline ASC, e.date_received DESC
        LIMIT ?
    """

    params.append(limit)

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def query_combined(
    conn: sqlite3.Connection,
    person: str | None = None,
    topic: str | None = None,
    keyword: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Combined query with multiple filters (AND logic).

    At least one filter must be provided.

    Args:
        conn: Database connection
        person: Optional person filter (partial name match or email)
        topic: Optional topic filter (partial match)
        keyword: Optional keyword for full-text search
        start_date: Optional start date (ISO 8601)
        end_date: Optional end date (ISO 8601)
        limit: Maximum number of results to return

    Returns:
        List of dicts with keys: email_id, date, subject, summary, sender,
        topics (comma-separated), relevance_score

    Raises:
        ValueError: If no filters are provided
    """
    if not any([person, topic, keyword, start_date, end_date]):
        raise ValueError("At least one filter must be provided")

    # Build query with all filters
    query = """
        SELECT DISTINCT
            e.id as email_id,
            e.date_received as date,
            e.subject,
            e.summary,
            e.sender_name as sender,
            GROUP_CONCAT(t.display_name, ', ') as topics,
            1.0 as relevance_score
        FROM emails e
        LEFT JOIN email_topics et ON e.id = et.email_id
        LEFT JOIN topics t ON et.topic_id = t.id
    """

    joins = []
    where_clauses = []
    params: list[Any] = []

    # Person filter
    if person:
        joins.append("JOIN email_people ep ON e.id = ep.email_id")
        joins.append("JOIN people p ON ep.person_id = p.id")
        if "@" in person:
            where_clauses.append("LOWER(p.email) = LOWER(?)")
            params.append(person.strip())
        else:
            where_clauses.append("p.name LIKE ?")
            params.append(f"%{person}%")

    # Topic filter
    if topic:
        topic_normalized = topic.strip().lower()
        where_clauses.append(
            "e.id IN (SELECT email_id FROM email_topics et2 JOIN topics t2 ON et2.topic_id = t2.id WHERE t2.name LIKE ?)"
        )
        params.append(f"%{topic_normalized}%")

    # Keyword filter (FTS5) — emails, key facts, and attachment content
    if keyword:
        safe_kw = _sanitize_fts5_query(keyword)
        or_branches = [
            "e.id IN (SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?)",
            "e.id IN (SELECT email_id FROM key_facts kf WHERE kf.id IN (SELECT rowid FROM key_facts_fts WHERE fact MATCH ?))",
        ]
        params.extend([safe_kw, safe_kw])
        if _has_attachment_fts(conn):
            or_branches.append(
                "e.id IN (SELECT a.email_id FROM attachments a "
                "JOIN attachment_content ac ON ac.attachment_id = a.id "
                "WHERE ac.id IN (SELECT rowid FROM attachment_content_fts WHERE attachment_content_fts MATCH ?))"
            )
            params.append(safe_kw)
        where_clauses.append("(" + " OR ".join(or_branches) + ")")

    # Date range filter
    if start_date:
        where_clauses.append("DATE(e.date_received) >= DATE(?)")
        params.append(start_date)

    if end_date:
        where_clauses.append("DATE(e.date_received) <= DATE(?)")
        params.append(end_date)

    # Add joins
    if joins:
        query += " " + " ".join(joins)

    # Add where clause
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)

    query += """
        GROUP BY e.id
        ORDER BY e.date_received DESC
        LIMIT ?
    """

    params.append(limit)

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def meeting_prep(
    conn: sqlite3.Connection,
    people: list[str],
    topic: str | None = None,
    days: int = 365,
    limit_per_person: int = 10,
) -> dict:
    """Generate a meeting prep dossier for attendees.

    Args:
        conn: Database connection
        people: List of attendee names or emails
        topic: Optional meeting topic to focus on
        days: How far back to look (default: 90 days)
        limit_per_person: Max emails per person (default: 10)

    Returns:
        Dict with keys:
        - attendees: list of per-person dossiers
        - topic_context: topic-related context (if topic provided)
    """
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    result: dict[str, Any] = {"attendees": [], "topic_context": None}

    for person in people:
        dossier: dict[str, Any] = {
            "name": person,
            "emails": [],
            "decisions": [],
            "open_actions": [],
            "topics": [],
            "sentiment_summary": {},
        }

        # Build person subquery (avoids SQL string concatenation)
        if "@" in person:
            person_param = person.strip()
        else:
            person_param = f"%{person}%"

        # Recent emails
        cursor = conn.execute(
            """
            SELECT DISTINCT e.id as email_id, e.date_received as date,
                e.subject, e.summary, ep.role_in_email as role, e.sentiment
            FROM emails e
            JOIN email_people ep ON e.id = ep.email_id
            WHERE ep.person_id IN (SELECT id FROM people WHERE
                CASE WHEN ? THEN LOWER(email) = LOWER(?) ELSE name LIKE ? END)
                AND e.date_received >= ?
            ORDER BY e.date_received DESC LIMIT ?
        """,
            ("@" in person, person_param, person_param, cutoff, limit_per_person),
        )
        dossier["emails"] = [dict(r) for r in cursor.fetchall()]

        # Sentiment distribution
        for email in dossier["emails"]:
            s = email.get("sentiment", "unknown") or "unknown"
            dossier["sentiment_summary"][s] = dossier["sentiment_summary"].get(s, 0) + 1

        # Decisions involving this person
        cursor = conn.execute(
            """
            SELECT d.decision, d.decided_by,
                COALESCE(d.decision_date, e.date_received) as date,
                e.subject as email_subject
            FROM decisions d
            JOIN emails e ON d.email_id = e.id
            JOIN email_people ep ON e.id = ep.email_id
            WHERE ep.person_id IN (SELECT id FROM people WHERE
                CASE WHEN ? THEN LOWER(email) = LOWER(?) ELSE name LIKE ? END)
                AND e.date_received >= ?
            ORDER BY date DESC LIMIT 10
        """,
            ("@" in person, person_param, person_param, cutoff),
        )
        dossier["decisions"] = [dict(r) for r in cursor.fetchall()]

        # Open action items
        cursor = conn.execute(
            """
            SELECT a.task, a.owner, a.deadline, a.status,
                e.subject as email_subject
            FROM action_items a
            JOIN emails e ON a.email_id = e.id
            JOIN email_people ep ON e.id = ep.email_id
            WHERE ep.person_id IN (SELECT id FROM people WHERE
                CASE WHEN ? THEN LOWER(email) = LOWER(?) ELSE name LIKE ? END)
                AND a.status = 'open'
            ORDER BY a.deadline IS NULL, a.deadline ASC LIMIT 10
        """,
            ("@" in person, person_param, person_param),
        )
        dossier["open_actions"] = [dict(r) for r in cursor.fetchall()]

        # Top topics
        cursor = conn.execute(
            """
            SELECT t.display_name as topic, COUNT(*) as count
            FROM email_topics et
            JOIN topics t ON et.topic_id = t.id
            JOIN emails e ON et.email_id = e.id
            JOIN email_people ep ON e.id = ep.email_id
            WHERE ep.person_id IN (SELECT id FROM people WHERE
                CASE WHEN ? THEN LOWER(email) = LOWER(?) ELSE name LIKE ? END)
                AND e.date_received >= ?
            GROUP BY t.id ORDER BY count DESC LIMIT 5
        """,
            ("@" in person, person_param, person_param, cutoff),
        )
        dossier["topics"] = [dict(r) for r in cursor.fetchall()]

        result["attendees"].append(dossier)

    # Topic context (if provided)
    if topic:
        topic_normalized = topic.strip().lower()
        topic_ctx: dict[str, Any] = {
            "topic": topic,
            "decisions": [],
            "key_facts": [],
            "action_items": [],
        }

        cursor = conn.execute(
            """
            SELECT d.decision, d.decided_by,
                   COALESCE(d.decision_date, e.date_received) as date,
                   e.subject as email_subject
            FROM decisions d
            JOIN emails e ON d.email_id = e.id
            JOIN email_topics et ON e.id = et.email_id
            JOIN topics t ON et.topic_id = t.id
            WHERE t.name LIKE ? AND e.date_received >= ?
            ORDER BY date DESC LIMIT 20
        """,
            (f"%{topic_normalized}%", cutoff),
        )
        topic_ctx["decisions"] = [dict(r) for r in cursor.fetchall()]

        cursor = conn.execute(
            """
            SELECT kf.fact, e.date_received as date, e.subject
            FROM key_facts kf
            JOIN emails e ON kf.email_id = e.id
            JOIN email_topics et ON e.id = et.email_id
            JOIN topics t ON et.topic_id = t.id
            WHERE t.name LIKE ? AND e.date_received >= ?
            ORDER BY e.date_received DESC LIMIT 20
        """,
            (f"%{topic_normalized}%", cutoff),
        )
        topic_ctx["key_facts"] = [dict(r) for r in cursor.fetchall()]

        cursor = conn.execute(
            """
            SELECT a.task, a.owner, a.deadline, a.status
            FROM action_items a
            JOIN emails e ON a.email_id = e.id
            JOIN email_topics et ON e.id = et.email_id
            JOIN topics t ON et.topic_id = t.id
            WHERE t.name LIKE ? AND a.status = 'open'
            ORDER BY a.deadline IS NULL, a.deadline ASC LIMIT 20
        """,
            (f"%{topic_normalized}%",),
        )
        topic_ctx["action_items"] = [dict(r) for r in cursor.fetchall()]

        result["topic_context"] = topic_ctx

    return result


def find_stale_threads(conn: sqlite3.Connection, days: int = 5) -> list[dict]:
    """Find conversation threads where user is last sender with no reply.

    Args:
        conn: Database connection
        days: Number of days since last activity to consider stale

    Returns:
        List of dicts with keys: conversation_id, subject, date_received,
        sender_address, days_waiting
    """
    from datetime import datetime, timedelta

    if not USER_EMAIL_PATTERN:
        return []

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    results = conn.execute(
        """
        WITH latest_per_thread AS (
            SELECT conversation_id,
                   MAX(date_received) as last_date
            FROM emails
            WHERE conversation_id IS NOT NULL
            GROUP BY conversation_id
        )
        SELECT e.conversation_id, e.subject, e.date_received, e.sender_address,
               CAST(julianday('now') - julianday(e.date_received) AS INTEGER) as days_waiting
        FROM latest_per_thread lpt
        JOIN emails e ON e.conversation_id = lpt.conversation_id
                     AND e.date_received = lpt.last_date
        WHERE LOWER(e.sender_address) LIKE LOWER(?)
          AND lpt.last_date < ?
        ORDER BY days_waiting DESC
    """,
        (f"%{USER_EMAIL_PATTERN}%", cutoff),
    ).fetchall()
    return [dict(r) for r in results]


def find_overdue_actions(conn: sqlite3.Connection) -> list[dict]:
    """Find action items past their deadline.

    Returns:
        List of dicts with keys: action_id, task, owner, deadline,
        email_subject, date, days_overdue
    """
    results = conn.execute("""
        SELECT ai.id as action_id, ai.task, ai.owner, ai.deadline,
               e.subject as email_subject, e.date_received as date,
               CAST(julianday('now') - julianday(ai.deadline) AS INTEGER) as days_overdue
        FROM action_items ai
        JOIN emails e ON ai.email_id = e.id
        WHERE ai.status = 'open' AND ai.deadline IS NOT NULL
          AND ai.deadline < date('now')
        ORDER BY days_overdue DESC
    """).fetchall()
    return [dict(r) for r in results]


def get_stats(conn: sqlite3.Connection) -> dict:
    """Return database statistics.

    Returns:
        Dict with keys: total_emails, total_news_articles, total_documents,
        total_topics, total_people, total_decisions, total_action_items,
        earliest_email, latest_email
    """
    stats = {}

    # Count emails. News articles and ingested standalone documents share the
    # `emails` table, so an unfiltered COUNT(*) is not a mail count: it read
    # 74,424 against the health check's 65,363 on the same DB. Same predicate as
    # scripts/health_check.py's `real_mail`, and the two extra keys keep the
    # split visible instead of hiding 9,061 rows.
    cursor = conn.execute(
        "SELECT COUNT(*) as count FROM emails "
        "WHERE message_id > 0 AND (mailbox_name IS NULL OR mailbox_name <> 'News')"
    )
    stats["total_emails"] = cursor.fetchone()["count"]

    cursor = conn.execute("SELECT COUNT(*) as count FROM emails WHERE mailbox_name = 'News'")
    stats["total_news_articles"] = cursor.fetchone()["count"]

    cursor = conn.execute("SELECT COUNT(*) as count FROM emails WHERE message_id <= 0")
    stats["total_documents"] = cursor.fetchone()["count"]

    # Count topics
    cursor = conn.execute("SELECT COUNT(*) as count FROM topics")
    stats["total_topics"] = cursor.fetchone()["count"]

    # Count people
    cursor = conn.execute("SELECT COUNT(*) as count FROM people")
    stats["total_people"] = cursor.fetchone()["count"]

    # Count decisions
    cursor = conn.execute("SELECT COUNT(*) as count FROM decisions")
    stats["total_decisions"] = cursor.fetchone()["count"]

    # Count action items
    cursor = conn.execute("SELECT COUNT(*) as count FROM action_items")
    stats["total_action_items"] = cursor.fetchone()["count"]

    # Get date range
    cursor = conn.execute(
        "SELECT MIN(date_received) as earliest, MAX(date_received) as latest FROM emails"
    )
    row = cursor.fetchone()
    stats["earliest_email"] = row["earliest"]
    stats["latest_email"] = row["latest"]

    return stats


def search_attachments(
    conn: sqlite3.Connection,
    keyword: str,
    limit: int = 20,
) -> list[dict]:
    """Search attachment content using FTS5.

    Args:
        conn: Database connection
        keyword: Search term
        limit: Maximum results

    Returns:
        List of dicts with: attachment_id, filename, mime_type,
        email_subject, email_date, snippet, summary
    """
    safe_kw = _sanitize_fts5_query(keyword)

    # Check if attachment_content_fts exists
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='attachment_content_fts'"
        ).fetchall()
    ]
    if "attachment_content_fts" not in tables:
        return []

    rows = conn.execute(
        """
        SELECT ac.attachment_id, a.filename, a.mime_type,
               e.subject, e.date_received,
               snippet(attachment_content_fts, 0, '>>>', '<<<', '...', 40) as text_snippet,
               ac.summary
        FROM attachment_content_fts
        JOIN attachment_content ac ON ac.id = attachment_content_fts.rowid
        JOIN attachments a ON a.id = ac.attachment_id
        LEFT JOIN emails e ON e.id = a.email_id
        WHERE attachment_content_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """,
        (safe_kw, limit),
    ).fetchall()

    return [
        {
            "attachment_id": r[0],
            "filename": r[1],
            "mime_type": r[2],
            "email_subject": r[3],
            "email_date": r[4],
            "snippet": r[5],
            "summary": r[6],
        }
        for r in rows
    ]
