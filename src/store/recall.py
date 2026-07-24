"""Unified recall — single 'ask once, get everything' entry point.

Composes keyword search across every text-bearing index in the brain
(emails+attachments+standalone docs, conversations, decisions, actions,
inline images) and auto-pulls person/topic context when the query matches
a known person name/email or topic. Used by both the /recall skill and
by other agents through the MCP `recall` tool.
"""

import sqlite3

from src.store.context import get_person_context, get_topic_context
from src.store.conversation_query import search_conversations_keyword
from src.store.fusion import reciprocal_rank_fusion
from src.store.query import _sanitize_fts5_query, query_by_keyword
from src.store.teams_query import search_teams as _search_teams_q


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _search_decisions(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.id, d.email_id, d.decision, d.decided_by, d.decision_date,
               e.subject as email_subject
        FROM decisions d
        LEFT JOIN emails e ON e.id = d.email_id
        WHERE d.decision LIKE ?
        ORDER BY d.decision_date DESC
        LIMIT ?
        """,
        (f"%{keyword}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _search_actions(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.email_id, a.task, a.owner, a.deadline, a.status,
               e.subject as email_subject
        FROM action_items a
        LEFT JOIN emails e ON e.id = a.email_id
        WHERE a.task LIKE ?
        ORDER BY CASE WHEN a.deadline IS NULL THEN 1 ELSE 0 END, a.deadline ASC
        LIMIT ?
        """,
        (f"%{keyword}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _search_commitments(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    if not _table_exists(conn, "commitments"):
        return []
    rows = conn.execute(
        """
        SELECT c.id, c.email_id, c.commitment, c.by_person, c.to_person,
               e.subject as email_subject
        FROM commitments c
        LEFT JOIN emails e ON e.id = c.email_id
        WHERE c.commitment LIKE ?
        LIMIT ?
        """,
        (f"%{keyword}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _search_inline_images(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    if not _table_exists(conn, "inline_images"):
        return []
    rows = conn.execute(
        """
        SELECT sha256, classification, vision_description, width, height
        FROM inline_images
        WHERE vision_description LIKE ?
        LIMIT ?
        """,
        (f"%{keyword}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _search_teams(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    """Search teams_threads_fts + teams_messages_fts via store.teams_query.search_teams.

    Returns empty list if the teams_threads_fts table doesn't exist (pre-v12 DB).
    """
    if not _table_exists(conn, "teams_threads_fts"):
        return []
    return _search_teams_q(conn, keyword, kind="both", limit=limit)


def _search_calendar_events(conn: sqlite3.Connection, query: str, limit: int) -> list[dict]:
    """FTS search over calendar events."""
    if not _table_exists(conn, "calendar_events_fts"):
        return []
    try:
        rows = conn.execute(
            """SELECT ce.id, ce.subject, ce.start_at, ce.body_summary, ce.organizer_name
               FROM calendar_events_fts f
               JOIN calendar_events ce ON ce.id = f.rowid
               WHERE calendar_events_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _maybe_person_context(conn: sqlite3.Connection, query: str, days: int) -> dict | None:
    """Return person_context if the query plausibly matches a known person."""
    # Cheap pre-check: is there any person whose name/email contains the query?
    if "@" in query:
        hit = conn.execute(
            "SELECT 1 FROM people WHERE LOWER(email) = LOWER(?)", (query.strip(),)
        ).fetchone()
    else:
        hit = conn.execute("SELECT 1 FROM people WHERE name LIKE ?", (f"%{query}%",)).fetchone()
    if not hit:
        return None
    ctx = get_person_context(conn, query, days=days)
    return ctx if ctx.get("person") else None


def _maybe_topic_context(conn: sqlite3.Connection, query: str, days: int) -> dict | None:
    """Return topic_context if the query plausibly matches a known topic."""
    hit = conn.execute(
        "SELECT 1 FROM topics WHERE name LIKE ?", (f"%{query.strip().lower()}%",)
    ).fetchone()
    if not hit:
        return None
    ctx = get_topic_context(conn, query, days=days)
    return ctx if ctx.get("topic") else None


def _hybrid_emails(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
    semantic_candidates,
) -> list[dict]:
    """RRF-fuse keyword email hits with semantic email candidates.

    Over-fetches keyword hits so fusion has a candidate pool, merges the keyword
    and semantic rankings with Reciprocal Rank Fusion, then caps to `limit`.
    Semantic-only emails are hydrated as email rows tagged source='semantic'. Any
    semantic failure (missing index, embed error, no ADC) degrades gracefully to
    keyword-only, so recall never breaks.
    """
    pool = max(limit * 4, limit)
    keyword_hits = query_by_keyword(conn, query, limit=pool)
    try:
        sem_ids = semantic_candidates(conn, query, pool)
    except Exception:
        return keyword_hits[:limit]
    if not sem_ids:
        return keyword_hits[:limit]

    kw_ids = [h["email_id"] for h in keyword_hits]
    fused = reciprocal_rank_fusion([kw_ids, sem_ids])
    kw_by_id = {h["email_id"]: h for h in keyword_hits}

    out: list[dict] = []
    for email_id, _score in fused:
        if len(out) >= limit:
            break
        hit = kw_by_id.get(email_id)
        if hit is not None:
            out.append(hit)
            continue
        row = conn.execute(
            """
            SELECT id as email_id, date_received as date, subject,
                   summary, summary as snippet
            FROM emails WHERE id = ?
            """,
            (email_id,),
        ).fetchone()
        if row:
            hit = dict(row)
            hit["source"] = "semantic"
            out.append(hit)
    return out


def recall(
    conn: sqlite3.Connection,
    query: str,
    limit_per_kind: int = 5,
    days: int = 365,
    semantic_candidates=None,
) -> dict:
    """Unified search across every text-bearing index in the brain.

    Args:
        conn: Database connection
        query: Free-text query
        limit_per_kind: Max results per category (default 5)
        days: Lookback window for person/topic context (default 365)
        semantic_candidates: Optional callable (conn, query, limit) -> ranked
            email ids. When provided, the emails bucket becomes a keyword+semantic
            RRF fusion; when None (default) it stays keyword-only. Injected by the
            MCP layer so recall itself carries no embedding dependency.

    Returns:
        Dict with categorized hits across emails (incl. attachments + standalone
        docs), conversations, decisions, actions, inline_images, and teams
        threads, plus optional person_context and topic_context populated when
        the query matches a known person or topic. Always includes every kind
        key (empty list if no matches) so callers don't have to handle missing
        keys.
    """
    # Emails (incl. attachments + standalone docs — query_by_keyword spans
    # emails_fts, key_facts_fts, and attachment_content_fts in one call). When a
    # semantic candidate provider is injected (the MCP runtime does this), fuse the
    # keyword and semantic rankings with RRF; otherwise stay keyword-only.
    if semantic_candidates is None:
        emails = query_by_keyword(conn, query, limit=limit_per_kind)
    else:
        emails = _hybrid_emails(conn, query, limit_per_kind, semantic_candidates)

    # Conversations — search_conversations_keyword takes a raw FTS5 query, so
    # sanitize first to mirror the safety of every other path here.
    safe_kw = _sanitize_fts5_query(query)
    if _table_exists(conn, "conversation_turns_fts"):
        conversations = search_conversations_keyword(conn, safe_kw, limit=limit_per_kind)
    else:
        conversations = []

    decisions = _search_decisions(conn, query, limit_per_kind)
    actions = _search_actions(conn, query, limit_per_kind)
    commitments = _search_commitments(conn, query, limit_per_kind)
    inline_images = _search_inline_images(conn, query, limit_per_kind)
    teams = _search_teams(conn, query, limit_per_kind)
    calendar_events = _search_calendar_events(conn, query, limit_per_kind)

    person_context = _maybe_person_context(conn, query, days=days)
    topic_context = _maybe_topic_context(conn, query, days=days)

    text_kinds = {
        "emails": emails,
        "conversations": conversations,
        "decisions": decisions,
        "actions": actions,
        "commitments": commitments,
        "inline_images": inline_images,
        "teams": teams,
        "calendar_events": calendar_events,
    }
    total_hits = sum(len(v) for v in text_kinds.values())
    kinds_with_results = [k for k, v in text_kinds.items() if v]

    return {
        "query": query,
        **text_kinds,
        "person_context": person_context,
        "topic_context": topic_context,
        "summary": {
            "total_hits": total_hits,
            "kinds_with_results": kinds_with_results,
            "has_person_context": person_context is not None,
            "has_topic_context": topic_context is not None,
        },
    }
