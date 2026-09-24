"""Query interface for the second-brain knowledge store.

Provides rich query functions for the /recall skill to search emails, topics,
people, decisions, and action items. All queries return structured results
optimized for CLI display.
"""

import json
import sqlite3
from typing import Any

from src.config import USER_EMAIL_PATTERN
from src.store.greek import register_sql_functions, search_fold
from src.store.normalizer import normalize_topic


def _sanitize_fts5_query(keyword: str) -> str:
    """Sanitize a keyword for safe FTS5 MATCH queries.

    FTS5 has special syntax characters (*, ", OR, AND, NOT, NEAR, -, ^, :)
    that can cause unexpected behavior or errors. This splits the input on
    whitespace and wraps each token in double quotes — defanging operators
    while preserving FTS5's implicit AND semantics across tokens (every token
    must appear somewhere in the row).

    Stopwords are dropped when anything else is left, as the folded buckets
    drop them: a question holds words no row does ('what', 'the', 'για'), and
    requiring them flagged every full-text answer partial.

    Examples:
        "Mylonas"             -> '"Mylonas"'
        "earnings release"    -> '"earnings" "release"'   (both required)
        "ACME Q1 2026"         -> '"ACME" "Q1" "2026"'      (all three required)
        "what about the ACME" -> '"ACME"'
        ""                    -> '""'                     (matches nothing)
    """
    # Fold Greek accents to match how the index stores them (schema v20). Both
    # sides fold, so an accented query still works: folding it yields the same
    # form the index holds. Without this the index would be folded and the query
    # would not, which is the strictly worse version of the bug being fixed.
    from src.store.greek import fold, is_stopword

    words = keyword.split()
    if not words:
        return '""'
    tokens = [fold(w) for w in ([w for w in words if not is_stopword(w)] or words)]
    quoted = []
    for token in tokens:
        # Escape any internal double quotes per FTS5 phrase syntax
        escaped = token.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " ".join(quoted)


def _any_token_fts5_query(keyword: str) -> str | None:
    """An FTS5 expression matching ANY meaningful token, or None if there is none.

    Every token required is right for the short queries people type, and wrong
    for the long, reformulated, mixed-language ones agents write: about half of
    those found nothing. Searches retry with this when the strict form finds
    nothing. None for a single-token query, where "any" would equal "all", and
    when no token survives search_tokens (stopwords, numbers, short words).
    """
    from src.store.greek import fold, search_tokens

    if len(fold(keyword).split()) < 2:
        return None
    tokens = search_tokens(keyword)
    if not tokens:
        return None
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def fts5_query_variants(keyword: str) -> list[tuple[str, bool]]:
    """(MATCH expression, partial) pairs to try in order: every token, then any.

    A caller stops at the first variant that returns rows and flags those rows
    with partial_match when the variant is the any-token one.
    """
    variants = [(_sanitize_fts5_query(keyword), False)]
    loose = _any_token_fts5_query(keyword)
    if loose:
        variants.append((loose, True))
    return variants


def _mark_partial(rows: list[dict], partial: bool) -> list[dict]:
    if partial:
        for row in rows:
            row["partial_match"] = True
    return rows


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


def _has_subject_index(conn: sqlite3.Connection) -> bool:
    """Whether emails_fts indexes the subject (schema v22).

    A replica runs whatever code its checkout holds against whatever database it
    last pulled, and the two do not move together: newer code must still search
    a store the producer has not migrated yet.
    """
    return any(r[1] == "subject_f" for r in conn.execute("PRAGMA table_info(emails_fts)"))


def query_thread(conn: sqlite3.Connection, email_id: int, limit: int = 50) -> list[dict]:
    """Get the conversation thread for an email.

    Given an email_id, finds its conversation_id and returns the emails in that
    conversation ordered chronologically. A thread longer than `limit` gives the
    `limit` emails centred on this one: the oldest could leave it out, and a
    search usually hits a recent one.

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

    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM emails WHERE conversation_id = ? ORDER BY date_received ASC, id ASC",
            (conversation_id,),
        )
    ]
    if 0 <= limit < len(ids):
        at = ids.index(email_id) if email_id in ids else len(ids) - 1
        start = max(0, min(at - limit // 2, len(ids) - limit))
        ids = ids[start : start + limit]

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
        WHERE id IN (SELECT value FROM json_each(?))
        ORDER BY date_received ASC, id ASC
        """,
        (json.dumps(ids),),
    )
    return [dict(r) for r in cursor.fetchall()]


def count_thread(conn: sqlite3.Connection, email_id: int) -> int:
    """How many emails query_thread's thread holds, however many it returned."""
    row = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE conversation_id = "
        "(SELECT conversation_id FROM emails WHERE id = ? AND conversation_id <> '')",
        (email_id,),
    ).fetchone()
    return row[0]


# A person filter picks its plan by how many emails the people it means are on.
# Walking emails newest first until LIMIT suits the mailbox owner, who is on
# nearly every email; collecting and sorting all of theirs took 0.4 s. For
# anyone else the walk can cover the whole mailbox: a name that fits hundreds of
# rarely emailed people took 0.8-1.3 s on the replica. Up to this many links,
# their emails are collected and sorted instead. Someone on many emails, all of
# them old, still gets the walk; reading each email's few links, it costs about
# 50 ms over 100K emails.
DENSE_PERSON_LINKS = 5000

# The people a filter means, as a CTE over a JSON list of their ids.
_IDS_CTE = "WITH ids(id) AS MATERIALIZED (SELECT value FROM json_each(?)) "


def _person_ids(conn: sqlite3.Connection, name_or_email: str) -> list[int]:
    """Every person a name or an address means, each name folded once.

    Folded in a pass of its own over people: inside the email query, joined
    through email_people, SQLite could fold once per link (1.3M rows).
    """
    if not any(ch.isalnum() for ch in search_fold(name_or_email)):
        return []  # '%', '_' and '.' fit everyone
    if "@" in name_or_email:
        sql = "SELECT id FROM people WHERE LOWER(email) = LOWER(?)"
        arg = name_or_email.strip()
    else:
        sql = "SELECT id FROM people WHERE sb_fold(name) LIKE ?"
        arg = f"%{search_fold(name_or_email)}%"
    return [row[0] for row in conn.execute(sql, (arg,))]


def _linked_to_ids(conn: sqlite3.Connection, ids_json: str) -> str:
    """A WHERE clause for emails linked to `ids`, planned by DENSE_PERSON_LINKS."""
    links = conn.execute(
        _IDS_CTE
        + "SELECT COUNT(*) FROM (SELECT 1 FROM email_people WHERE person_id IN ids LIMIT ?)",
        (ids_json, DENSE_PERSON_LINKS + 1),
    ).fetchone()[0]
    if links > DENSE_PERSON_LINKS:
        # The unary + stops SQLite probing every id for every email: it reads the
        # email's few links and looks each one up in ids instead.
        return (
            "EXISTS (SELECT 1 FROM email_people ep "
            "WHERE ep.email_id = e.id AND +ep.person_id IN ids)"
        )
    return "e.id IN (SELECT email_id FROM email_people WHERE person_id IN ids)"


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
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn
    ids = _person_ids(conn, name_or_email)
    if not ids:
        return []
    ids_json = json.dumps(ids)
    # One row per email. person_role is one of the roles they hold on it, sender
    # first: the lowest in sort order used to win, so 'recipient' hid 'sender'.
    query = (
        _IDS_CTE
        + """
        SELECT
            e.id as email_id,
            e.date_received as date,
            e.subject,
            e.summary,
            (SELECT ep.role_in_email FROM email_people ep
              WHERE ep.email_id = e.id AND +ep.person_id IN ids
              ORDER BY ep.role_in_email <> 'sender', ep.role_in_email LIMIT 1) as person_role,
            e.sentiment
        FROM emails e
        WHERE """
        + _linked_to_ids(conn, ids_json)
        + """
        ORDER BY e.date_received DESC
        LIMIT ?
    """
    )
    return [dict(row) for row in conn.execute(query, (ids_json, limit))]


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
    topic_normalized = normalize_topic(topic)
    params = (f"%{topic_normalized}%", limit)

    cursor = conn.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def query_by_keyword(
    conn: sqlite3.Connection,
    keyword: str,
    limit: int = 20,
    search_content_only: bool = False,
) -> list[dict]:
    """Full-text search using FTS5 on email subjects, summaries, content, key facts
    and attachments.

    Args:
        conn: Database connection
        keyword: Search keyword or phrase
        limit: Maximum number of results to return
        search_content_only: When True, only match against the content column

    Returns:
        List of dicts with keys: email_id, date, subject, summary, snippet, source
        where source is 'subject', 'summary', 'content', 'key_fact' or
        'attachment'. When no row carries
        every token, rows matching any meaningful token come back instead, each
        flagged partial_match.
    """
    for expression, partial in fts5_query_variants(keyword):
        results = _keyword_waterfall(conn, expression, limit, search_content_only)
        if results:
            return _mark_partial(results, partial)
    return []


# The thread an email row belongs to, for one row per thread: NULL for an email
# with none (or a blank id), and for News, whose conversation_id is a day per
# pipeline rather than a thread.
_THREAD = "CASE WHEN e.mailbox_name = 'News' THEN NULL ELSE NULLIF(TRIM(e.conversation_id), '') END"

# Leaves out the threads the subject stage returned: their other emails'
# summaries and bodies repeat the words too, and one thread took half the page.
# In the query, not after it: skipped after the LIMIT, they used it up. One
# parameter, a JSON list.
_NOT_SUBJECT_THREAD = f"AND COALESCE({_THREAD}, '') NOT IN (SELECT value FROM json_each(?))"


def _keyword_waterfall(
    conn: sqlite3.Connection,
    safe_keyword: str,
    limit: int,
    search_content_only: bool,
) -> list[dict]:
    """query_by_keyword's source waterfall for one sanitized MATCH expression."""
    results: list[dict] = []
    seen_ids: set[int] = set()
    subject_threads: set[str] = set()

    def take(rows, from_subjects: bool = False) -> None:
        """Append the rows not found already, until the page is full.

        Every source asks for a whole page: of what it returns, only the rows
        taken so far can be taken already, so the rest still fills the page,
        where asking only for the slots left gave them away. One source can
        return an email twice (two of its key facts match).
        """
        for row in rows:
            if len(results) >= limit:
                return
            r = dict(row)
            thread = r.pop("thread", None)
            if r["email_id"] in seen_ids:
                continue
            seen_ids.add(r["email_id"])
            if from_subjects and thread is not None:
                subject_threads.add(thread)
            results.append(r)

    if not search_content_only and _has_subject_index(conn):
        # The subject first: the words someone remembers an email by, and until
        # v22 not indexed at all. snippet() is not needed; the subject is the row.
        # One row per thread: every email of a thread carries its subject, and a
        # long thread filled the page. The thread ranks by its best match and is
        # shown by its newest email, where it stands now; within a thread the
        # ranks differ only by how many RE:/FW: prefixes a subject carries. An
        # email with no thread partitions by its id, an integer, which no
        # conversation_id (always text) can equal.
        query_subjects = f"""
            SELECT email_id, date, subject, summary, snippet, source, thread FROM (
                SELECT
                    e.id as email_id,
                    e.date_received as date,
                    e.subject,
                    e.summary,
                    e.subject as snippet,
                    'subject' as source,
                    {_THREAD} as thread,
                    MIN(emails_fts.rank) OVER t as score,
                    ROW_NUMBER() OVER (t ORDER BY e.date_received DESC, e.id DESC) as nth
                FROM emails_fts
                JOIN emails e ON e.id = emails_fts.rowid
                WHERE emails_fts.subject_f MATCH ?
                WINDOW t AS (PARTITION BY COALESCE({_THREAD}, e.id))
            )
            WHERE nth = 1
            ORDER BY score
            LIMIT ?
        """
        take(conn.execute(query_subjects, (safe_keyword, limit)), from_subjects=True)

    if len(results) < limit and not search_content_only:
        # Search in email summaries
        # Rank by FTS5 BM25 relevance (ORDER BY rank), not recency. rank is only
        # comparable within a single MATCH query, so each source is ranked on its
        # own; the source-priority waterfall (subject -> summary -> content ->
        # key_fact -> attachment) plus seen_ids dedup preserves the cross-source
        # order.
        query_summaries = f"""
            SELECT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                e.summary as snippet,
                'summary' as source,
                {_THREAD} as thread
            FROM emails_fts
            JOIN emails e ON e.id = emails_fts.rowid
            -- Column names carry the _f suffix from schema v20: the FTS indexes
            -- the folded GENERATED columns, and an external-content FTS5's
            -- column names are by definition its content table's column names.
            WHERE emails_fts.summary_f MATCH ? {_NOT_SUBJECT_THREAD}
            ORDER BY rank
            LIMIT ?
        """

        take(
            conn.execute(
                query_summaries, (safe_keyword, json.dumps(sorted(subject_threads)), limit)
            )
        )

    # Search in email content
    if len(results) < limit:
        query_content = f"""
            SELECT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                snippet(emails_fts, 1, '<b>', '</b>', '...', 30) as snippet,
                'content' as source,
                {_THREAD} as thread
            FROM emails e
            JOIN emails_fts ON emails_fts.rowid = e.id
            WHERE emails_fts.content_f MATCH ? {_NOT_SUBJECT_THREAD}
            ORDER BY rank
            LIMIT ?
        """

        take(
            conn.execute(query_content, (safe_keyword, json.dumps(sorted(subject_threads)), limit))
        )

    # Search in key facts (only if we haven't hit the limit and not content-only)
    if len(results) < limit and not search_content_only:
        query_facts = f"""
            SELECT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                kf.fact as snippet,
                'key_fact' as source,
                {_THREAD} as thread
            FROM key_facts_fts
            JOIN key_facts kf ON kf.id = key_facts_fts.rowid
            JOIN emails e ON e.id = kf.email_id
            WHERE key_facts_fts MATCH ? {_NOT_SUBJECT_THREAD}
            ORDER BY rank
            LIMIT ?
        """

        take(conn.execute(query_facts, (safe_keyword, json.dumps(sorted(subject_threads)), limit)))

    # Search in attachment content (PDF/Office text + LLM summary).
    # Surfaces attachment-only matches as the parent email row, with
    # source='attachment' and the matched filename for caller transparency.
    if len(results) < limit and not search_content_only and _has_attachment_fts(conn):
        query_attachments = f"""
            SELECT
                e.id as email_id,
                e.date_received as date,
                e.subject,
                e.summary,
                snippet(attachment_content_fts, 0, '<b>', '</b>', '...', 30) as snippet,
                'attachment' as source,
                a.filename as attachment_filename,
                {_THREAD} as thread
            FROM attachment_content_fts
            JOIN attachment_content ac ON ac.id = attachment_content_fts.rowid
            JOIN attachments a ON a.id = ac.attachment_id
            JOIN emails e ON e.id = a.email_id
            WHERE attachment_content_fts MATCH ? {_NOT_SUBJECT_THREAD}
            ORDER BY rank
            LIMIT ?
        """

        take(
            conn.execute(
                query_attachments, (safe_keyword, json.dumps(sorted(subject_threads)), limit)
            )
        )

    # Results are relevance-ranked within each source (ORDER BY rank) and appended
    # in source-priority order (subject -> summary -> content -> key_fact ->
    # attachment); keep that order rather than re-sorting by date, so BM25
    # relevance is not discarded. (True cross-source ranking via score fusion / RRF
    # is a later change.) take() stops at the requested number.
    return results


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


# A decision's date: its own when it is one, else its parent's. The extractor
# writes free text too ('null', 'Q3 2026'), which sorted after every date and
# fell to the bound that keeps meetings still to come from deciding anything.
_DECISION_DATE = (
    "COALESCE(CASE WHEN d.decision_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
    " THEN d.decision_date END, e.date_received, tt.started_at, ce.start_at, c.started_at)"
)


def query_decisions(
    conn: sqlite3.Connection,
    topic: str | None = None,
    person: str | None = None,
    limit: int = 20,
    days: int | None = None,
    include_news: bool = False,
) -> list[dict]:
    """Find decisions, optionally filtered by topic or person.

    Args:
        conn: Database connection
        topic: Optional topic filter (partial match)
        person: Optional person filter (decided_by field, partial match)
        limit: Maximum number of results to return
        days: Optional lookback window. None means all time, which is what this
            function always did; the MCP tool exposed a `days` argument that
            never reached here.
        include_news: Include decisions extracted from ingested news articles.
            Default False: 15,602 of 104,768 decisions come from market
            commentary, and a decision some company announced is not one this
            user or their colleagues took.

    Returns:
        List of dicts with keys: decision_id, decision, decided_by, date,
        email_subject, topics (comma-separated)
    """
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn
    # Build query with optional filters
    # Same shape as query_action_items: LEFT JOIN every parent and COALESCE for
    # display. The inner JOIN on emails dropped 13,785 decisions whose parent was
    # a Teams thread, a calendar event or a conversation turn, and the 4,674
    # calendar ones had no read path anywhere.
    query = f"""
        SELECT
            d.id as decision_id,
            d.decision,
            d.decided_by,
            {_DECISION_DATE} as date,
            COALESCE(e.subject, tt.title, ce.subject, c.summary) as email_subject,
            CASE
                WHEN e.id IS NOT NULL THEN 'email'
                WHEN tt.id IS NOT NULL THEN 'teams'
                WHEN ce.id IS NOT NULL THEN 'calendar'
                WHEN c.id IS NOT NULL THEN 'conversation'
                ELSE 'orphan'
            END as source,
            GROUP_CONCAT(t.display_name, ', ') as topics
        FROM decisions d
        LEFT JOIN emails e ON d.email_id = e.id
        LEFT JOIN teams_threads tt ON d.teams_thread_id = tt.id
        LEFT JOIN calendar_events ce ON d.event_id = ce.id
        LEFT JOIN conversation_turns ct ON d.conversation_turn_id = ct.id
        LEFT JOIN conversations c ON ct.conversation_id = c.id
        LEFT JOIN email_topics et ON e.id = et.email_id
        LEFT JOIN topics t ON et.topic_id = t.id
    """

    where_clauses = []
    params: list[Any] = []

    if topic:
        where_clauses.append(
            "e.id IN (SELECT email_id FROM email_topics et2 JOIN topics t2 ON et2.topic_id = t2.id WHERE t2.name LIKE ?)"
        )
        params.append(f"%{normalize_topic(topic)}%")

    if person:
        where_clauses.append("sb_fold(d.decided_by) LIKE ?")
        params.append(f"%{search_fold(person)}%")

    if not include_news:
        where_clauses.append("(e.id IS NULL OR e.mailbox_name IS NULL OR e.mailbox_name <> 'News')")

    # Nothing has been decided at a date still to come. A meeting next week
    # returned its agenda as decisions, and dated by the meeting they sorted
    # above every real one: on the replica, the whole first page.
    where_clauses.append(f"{_DECISION_DATE} <= strftime('%Y-%m-%dT%H:%M:%S', 'now')")

    if days is not None:
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        where_clauses.append(f"{_DECISION_DATE} >= ?")
        params.append(cutoff)

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


# An ISO date this store can actually sort and compare. 3,157 open action items
# carry a deadline that is free text the model wrote ("1 day before", "2-3 days
# after the workshop"). Those are not dates, and because they are not NULL either
# they sorted ahead of every real one: the default page of "my open actions" was
# entirely "1 day before" and "2 days befor", with no genuine deadline visible.
_ISO_DATE = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"


def query_action_items(
    conn: sqlite3.Connection,
    owner: str | None = None,
    status: str = "open",
    limit: int = 20,
    include_news: bool = False,
    sources: tuple[str, ...] = ("email", "teams", "calendar", "conversation"),
) -> list[dict]:
    """Find action items, filtered by owner and/or status.

    Args:
        conn: Database connection
        owner: Optional owner filter (partial match)
        status: Status filter (default: 'open')
        limit: Maximum number of results to return
        include_news: Include items extracted from ingested news articles.
            Default False: news contributes 10,314 of the 141,478 open items and
            they are market commentary, not things this user owes anyone.
        sources: Which parent kinds to include. Defaults to all four. This used
            to INNER JOIN emails, which silently dropped every item whose parent
            was a Teams thread, a calendar event or a conversation turn: 10,654
            open items, of which the 2,555 calendar ones had no read path
            anywhere in the codebase.

    Returns:
        List of dicts with keys: action_id, task, owner, deadline, status,
        email_subject, date, source
    """
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn
    # LEFT JOIN every parent, then COALESCE for display. `source` tells the
    # caller which kind it got, because "email_subject" on a calendar item would
    # otherwise be a quiet lie.
    query = """
        SELECT
            a.id as action_id,
            a.task,
            a.owner,
            a.deadline,
            a.status,
            COALESCE(e.subject, tt.title, ce.subject, c.summary) as email_subject,
            COALESCE(e.date_received, tt.started_at, ce.start_at, c.started_at) as date,
            CASE
                WHEN e.id IS NOT NULL THEN 'email'
                WHEN tt.id IS NOT NULL THEN 'teams'
                WHEN ce.id IS NOT NULL THEN 'calendar'
                WHEN c.id IS NOT NULL THEN 'conversation'
                ELSE 'orphan'
            END as source,
            CASE
                WHEN a.deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                     AND a.deadline < date('now') THEN 1 ELSE 0
            END as overdue
        FROM action_items a
        LEFT JOIN emails e ON a.email_id = e.id
        LEFT JOIN teams_threads tt ON a.teams_thread_id = tt.id
        LEFT JOIN calendar_events ce ON a.event_id = ce.id
        LEFT JOIN conversation_turns ct ON a.conversation_turn_id = ct.id
        LEFT JOIN conversations c ON ct.conversation_id = c.id
    """

    where_clauses = []
    params: list[Any] = []

    if status:
        where_clauses.append("a.status = ?")
        params.append(status)

    if owner:
        where_clauses.append("sb_fold(a.owner) LIKE ?")
        params.append(f"%{search_fold(owner)}%")

    if not include_news:
        where_clauses.append("(e.id IS NULL OR e.mailbox_name IS NULL OR e.mailbox_name <> 'News')")

    wanted = set(sources)
    if wanted != {"email", "teams", "calendar", "conversation"}:
        parts = []
        if "email" in wanted:
            parts.append("e.id IS NOT NULL")
        if "teams" in wanted:
            parts.append("tt.id IS NOT NULL")
        if "calendar" in wanted:
            parts.append("ce.id IS NOT NULL")
        if "conversation" in wanted:
            parts.append("c.id IS NOT NULL")
        where_clauses.append("(" + " OR ".join(parts) + ")" if parts else "0")

    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)

    # Actionable first. Three buckets, in this order:
    #   0  upcoming: a real date, today or later, soonest first
    #   1  undated: NULL or free text, most recent parent first
    #   2  overdue: a real date in the past, most recently missed first
    #
    # Nothing is hidden, because an overdue commitment is still a commitment,
    # but most dated open items are overdue (on 2026-09-09, 16,475 of 20,518).
    # Sorted purely by deadline they filled every page and the default view of
    # "what do I owe" contained not one live item.
    query += f"""
        ORDER BY CASE
                     WHEN a.deadline {_ISO_DATE} AND a.deadline >= date('now') THEN 0
                     WHEN a.deadline {_ISO_DATE} THEN 2
                     ELSE 1
                 END ASC,
                 CASE WHEN a.deadline {_ISO_DATE} AND a.deadline >= date('now')
                      THEN a.deadline END ASC,
                 CASE WHEN a.deadline {_ISO_DATE} AND a.deadline < date('now')
                      THEN a.deadline END DESC,
                 date DESC
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
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn
    if not any([person, topic, keyword, start_date, end_date]):
        raise ValueError("At least one filter must be provided")

    cte = ""
    cte_params: list[Any] = []
    where_clauses = []
    params: list[Any] = []

    # Person filter, planned as in query_by_person.
    if person:
        ids = _person_ids(conn, person)
        if not ids:
            return []
        ids_json = json.dumps(ids)
        cte = _IDS_CTE
        cte_params.append(ids_json)
        where_clauses.append(_linked_to_ids(conn, ids_json))

    # Topic filter
    if topic:
        topic_normalized = normalize_topic(topic)
        where_clauses.append(
            "e.id IN (SELECT email_id FROM email_topics et2 JOIN topics t2 ON et2.topic_id = t2.id WHERE t2.name LIKE ?)"
        )
        params.append(f"%{topic_normalized}%")

    # Keyword filter (FTS5): emails, key facts, and attachment content
    if keyword:
        safe_kw = _sanitize_fts5_query(keyword)
        or_branches = [
            "e.id IN (SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?)",
            "e.id IN (SELECT email_id FROM key_facts kf WHERE kf.id IN (SELECT rowid FROM key_facts_fts WHERE fact_f MATCH ?))",
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

    # The newest `limit` matching emails are picked first, then their topics are
    # joined: grouping every match by topic before the LIMIT sorted them all.
    picked = "SELECT e.id FROM emails e"
    if where_clauses:
        picked += " WHERE " + " AND ".join(where_clauses)
    picked += " ORDER BY e.date_received DESC LIMIT ?"
    query = (
        cte
        + """
        SELECT
            e.id as email_id,
            e.date_received as date,
            e.subject,
            e.summary,
            e.sender_name as sender,
            GROUP_CONCAT(t.display_name, ', ') as topics,
            1.0 as relevance_score
        FROM ("""
        + picked
        + """) picked
        JOIN emails e ON e.id = picked.id
        LEFT JOIN email_topics et ON e.id = et.email_id
        LEFT JOIN topics t ON et.topic_id = t.id
        GROUP BY e.id
        ORDER BY e.date_received DESC
    """
    )
    params = cte_params + params + [limit]

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
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    result: dict[str, Any] = {"attendees": [], "topic_context": None}

    from src.store.context import resolve_person

    for person in people:
        dossier: dict[str, Any] = {
            "name": person,
            "emails": [],
            "decisions": [],
            "open_actions": [],
            "topics": [],
            "sentiment_summary": {},
        }

        # One person per attendee, resolved as person_context resolves a name. A
        # folded LIKE in every query merged every namesake into one dossier.
        person_row, match_count, others = resolve_person(conn, person)
        dossier["match_count"] = match_count
        dossier["other_candidates"] = others
        if person_row is None:
            result["attendees"].append(dossier)
            continue
        dossier["resolved_name"] = person_row["name"]
        dossier["resolved_email"] = person_row["email"]
        person_id = person_row["id"]

        # Recent emails
        cursor = conn.execute(
            """
            SELECT DISTINCT e.id as email_id, e.date_received as date,
                e.subject, e.summary, ep.role_in_email as role, e.sentiment
            FROM emails e
            JOIN email_people ep ON e.id = ep.email_id
            WHERE ep.person_id = ? AND e.date_received >= ?
            ORDER BY e.date_received DESC LIMIT ?
        """,
            (person_id, cutoff, limit_per_person),
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
            WHERE ep.person_id = ? AND e.date_received >= ?
            ORDER BY date DESC LIMIT 10
        """,
            (person_id, cutoff),
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
            WHERE ep.person_id = ? AND a.status = 'open'
            ORDER BY a.deadline IS NULL, a.deadline ASC LIMIT 10
        """,
            (person_id,),
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
            WHERE ep.person_id = ? AND e.date_received >= ?
            GROUP BY t.id ORDER BY count DESC LIMIT 5
        """,
            (person_id, cutoff),
        )
        dossier["topics"] = [dict(r) for r in cursor.fetchall()]

        result["attendees"].append(dossier)

    # Topic context (if provided)
    if topic:
        topic_normalized = normalize_topic(topic)
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


def _stale_threads_sql(select: str, days: int, max_days: int) -> tuple[str, tuple]:
    """Threads whose last message the user sent between `days` and `max_days` ago.

    A threshold at or past the window widens it by 30 days: days=45 against
    the 30-day default was an empty answer with a total of 0, read as 'nobody
    owes you a reply'.
    """
    from datetime import datetime, timedelta

    if max_days <= days:
        max_days = days + 30
    now = datetime.now()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    oldest = (now - timedelta(days=max_days)).strftime("%Y-%m-%dT%H:%M:%S")
    sql = f"""
        WITH latest_per_thread AS (
            SELECT conversation_id,
                   MAX(date_received) as last_date
            FROM emails
            WHERE conversation_id IS NOT NULL
            GROUP BY conversation_id
        )
        SELECT {select}
        FROM latest_per_thread lpt
        JOIN emails e ON e.conversation_id = lpt.conversation_id
                     AND e.date_received = lpt.last_date
        WHERE LOWER(e.sender_address) LIKE LOWER(?)
          AND lpt.last_date < ?
          AND lpt.last_date >= ?
    """
    return sql, (f"%{USER_EMAIL_PATTERN}%", cutoff, oldest)


def find_stale_threads(
    conn: sqlite3.Connection, days: int = 5, max_days: int = 30, limit: int = 20
) -> list[dict]:
    """Threads where the user sent last and nobody replied, newest first.

    Bounded both ways. Unbounded and oldest first, it returned every thread the
    user ever sent last: 8,891 rows and 2.2 MB on the replica once
    BRAIN_USER_EMAIL_PATTERN was set, far past the MCP result cap. A thread last
    touched more than `max_days` ago is history, not a reminder. Use
    count_stale_threads() for the total.

    Args:
        conn: Database connection
        days: Days since the user's message before a thread counts as stale
        max_days: Oldest such message still worth a reminder
        limit: Maximum threads to return

    Returns:
        List of dicts with keys: conversation_id, subject, date_received,
        sender_address, days_waiting
    """
    if not USER_EMAIL_PATTERN:
        return []
    sql, params = _stale_threads_sql(
        "e.conversation_id, e.subject, e.date_received, e.sender_address, "
        "CAST(julianday('now') - julianday(e.date_received) AS INTEGER) as days_waiting",
        days,
        max_days,
    )
    rows = conn.execute(sql + " ORDER BY e.date_received DESC LIMIT ?", (*params, limit))
    return [dict(r) for r in rows.fetchall()]


def count_stale_threads(conn: sqlite3.Connection, days: int = 5, max_days: int = 30) -> int:
    """How many threads find_stale_threads would return without its limit."""
    if not USER_EMAIL_PATTERN:
        return 0
    sql, params = _stale_threads_sql("COUNT(*)", days, max_days)
    return conn.execute(sql, params).fetchone()[0]


# Open, past a deadline SQLite can parse, and not from a news article. A NULL
# julianday() means the deadline is free text; those rows sorted to the top with
# days_overdue = NULL and pushed the real answers off the end.
_OVERDUE_WHERE = """
    ai.status = 'open' AND ai.deadline IS NOT NULL
      AND julianday(ai.deadline) IS NOT NULL
      AND ai.deadline < date('now')
      AND (e.id IS NULL OR e.mailbox_name IS NULL OR e.mailbox_name <> 'News')
"""


def count_overdue_actions(conn: sqlite3.Connection) -> int:
    """How many open action items are past a parseable deadline, news excluded."""
    return conn.execute(
        "SELECT COUNT(*) FROM action_items ai LEFT JOIN emails e ON ai.email_id = e.id "
        "WHERE " + _OVERDUE_WHERE
    ).fetchone()[0]


def find_overdue_actions(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Find action items past their deadline, most recently missed first.

    `limit` is not optional in practice. This had no bound until 2026-09-09 and
    the live corpus answers it with 15,618 rows / 7.2 MB, far past the MCP result
    cap, so `stale_threads` (its only caller) failed every single time it ran.
    Use count_overdue_actions() when you want the total.

    Every parent kind, as query_action_items has: this joined emails, which
    dropped every Teams, calendar and conversation item, and it kept news. Most
    overdue first, the list opened on items missed years ago.

    Returns:
        List of dicts with keys: action_id, task, owner, deadline,
        email_subject, date, source, days_overdue
    """
    results = conn.execute(
        """
        SELECT ai.id as action_id, ai.task, ai.owner, ai.deadline,
               COALESCE(e.subject, tt.title, ce.subject, c.summary) as email_subject,
               COALESCE(e.date_received, tt.started_at, ce.start_at, c.started_at) as date,
               CASE
                   WHEN e.id IS NOT NULL THEN 'email'
                   WHEN tt.id IS NOT NULL THEN 'teams'
                   WHEN ce.id IS NOT NULL THEN 'calendar'
                   WHEN c.id IS NOT NULL THEN 'conversation'
                   ELSE 'orphan'
               END as source,
               CAST(julianday('now') - julianday(ai.deadline) AS INTEGER) as days_overdue
        FROM action_items ai
        LEFT JOIN emails e ON ai.email_id = e.id
        LEFT JOIN teams_threads tt ON ai.teams_thread_id = tt.id
        LEFT JOIN calendar_events ce ON ai.event_id = ce.id
        LEFT JOIN conversation_turns ct ON ai.conversation_turn_id = ct.id
        LEFT JOIN conversations c ON ct.conversation_id = c.id
        WHERE """
        + _OVERDUE_WHERE
        + """
        ORDER BY ai.deadline DESC
        LIMIT ?
    """,
        (limit,),
    ).fetchall()
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
    stats["coverage"] = get_coverage(conn)
    stats.update(get_freshness(conn))

    return stats


def get_coverage(conn: sqlite3.Connection) -> dict:
    """The first and last date each source holds, and how many items.

    Sources start at different dates, most of them later than the earliest
    email suggests: 19 old documents made that '2018', while mail starts years
    later. An agent that cannot see where a source starts reads an empty answer
    as 'nothing happened'.
    """

    def span(sql: str) -> dict:
        try:
            first, last, n = conn.execute(sql).fetchone()
        except sqlite3.OperationalError:
            return {"first": None, "last": None, "items": 0}
        return {"first": first, "last": last, "items": n}

    mailboxes = {
        name or "(none)": {"first": first, "last": last, "emails": n}
        for name, first, last, n in conn.execute(
            "SELECT mailbox_name, MIN(date_received), MAX(date_received), COUNT(*) "
            "FROM emails GROUP BY mailbox_name ORDER BY MIN(date_received)"
        )
    }
    return {
        "mailboxes": mailboxes,
        "teams": span("SELECT MIN(started_at), MAX(ended_at), COUNT(*) FROM teams_threads"),
        "calendar": span("SELECT MIN(start_at), MAX(start_at), COUNT(*) FROM calendar_events"),
        "conversations": span(
            "SELECT MIN(started_at), MAX(COALESCE(ended_at, started_at)), COUNT(*) "
            "FROM conversations"
        ),
    }


# How far behind the producer the replica may drift before a caller should be
# told. The Mac pulls hourly during the day and not at all overnight, so a few
# hours is normal and 3 is the first value that is not.
STALE_AFTER_HOURS = 3.0


def get_freshness(conn: sqlite3.Connection) -> dict:
    """How current this database is, and whether a caller should say so.

    This file is usually a REPLICA: the VPS produces it and the Mac rsyncs it
    down on a schedule. Nothing used to expose that. A search tool answering off
    a replica that stopped updating 29 hours ago looks exactly like one answering
    off a live corpus in which nothing happened, and on 2026-09-07 the pull did
    stop, for a day and a half, with no signal to any caller.

    Returns data_as_of / age_hours / stale, plus a `stale_warning` sentence when
    the replica is behind, so it can be surfaced verbatim.
    """
    from datetime import datetime

    out: dict = {"data_as_of": None, "age_hours": None, "stale": False}
    try:
        row = conn.execute(
            "SELECT value FROM sync_metadata WHERE key = 'last_sync_date'"
        ).fetchone()
    except sqlite3.OperationalError:
        return out
    if not row or not row[0]:
        return out

    out["data_as_of"] = row[0]
    try:
        as_of = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    except ValueError:
        return out
    now = datetime.now(as_of.tzinfo) if as_of.tzinfo else datetime.now()
    age_hours = round((now - as_of).total_seconds() / 3600.0, 1)
    out["age_hours"] = age_hours
    if age_hours > STALE_AFTER_HOURS:
        out["stale"] = True
        out["stale_warning"] = (
            f"This corpus was last updated {age_hours}h ago ({row[0]}). Anything "
            "more recent than that is missing. Use outlook_live_search for very "
            "recent mail, and say so if the answer depends on recent items."
        )
    return out


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
        email_subject, email_date, snippet, summary. Rows from the any-token
        fallback carry partial_match.
    """
    # Check if attachment_content_fts exists
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='attachment_content_fts'"
        ).fetchall()
    ]
    if "attachment_content_fts" not in tables:
        return []

    for safe_kw, partial in fts5_query_variants(keyword):
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
        if rows:
            return _mark_partial(
                [
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
                ],
                partial,
            )
    return []
