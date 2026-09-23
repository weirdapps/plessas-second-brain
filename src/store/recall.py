"""Unified recall — single 'ask once, get everything' entry point.

Composes keyword search across every text-bearing index in the brain
(emails+attachments+standalone docs, conversations, decisions, actions,
inline images) and auto-pulls person/topic context when the query matches
a known person name/email or topic. Used by both the /recall skill and
by other agents through the MCP `recall` tool.
"""

import logging
import sqlite3

from src.store.context import get_person_context, get_topic_context
from src.store.conversation_query import search_conversations_keyword
from src.store.fusion import reciprocal_rank_fusion
from src.store.greek import (
    PHRASE_MATCH,
    STOPWORDS,
    register_sql_functions,
    search_fold,
    search_phrase,
    search_tokens,
)
from src.store.normalizer import normalize_topic
from src.store.query import fts5_query_variants, query_by_keyword, search_attachments
from src.store.teams_query import search_teams as _search_teams_q

logger = logging.getLogger(__name__)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _folded_bucket(conn: sqlite3.Connection, sql: str, keyword: str, limit: int) -> list[dict]:
    """Run a LIKE-style bucket in one pass: the whole query, else any of its tokens.

    ``sql`` takes (phrases, joined tokens, limit), selects sb_match(...) AS score
    and orders by it first. Matching is on folded text (case, accents, final
    sigma), at the start of a word, which LIKE alone does not do. A row holding
    the phrase, or every word of the query in any order, is a whole match, and
    whole matches come back alone when there are any. The any-order test counts
    tokens, so it applies only when the tokens are every word but the stopwords:
    a year or a short word is no token, and a row without it is not whole.
    Otherwise rows holding some of the words come back, most first, flagged
    partial_match: the whole-query form alone emptied these buckets for the long
    queries agents write. A one-word query gets no token pass, since "any" would
    equal "all".
    """
    register_sql_functions(conn)  # the MCP image search calls this directly
    stripped = search_phrase(keyword)
    if not stripped:
        return []
    typed = " ".join(search_fold(keyword).split())
    phrases = "\x1e".join(dict.fromkeys(f for f in (stripped, typed) if f))
    tokens = search_tokens(keyword) if len(stripped.split()) >= 2 else []
    every_word = bool(tokens) and set(tokens) == set(stripped.split()) - STOPWORDS
    rows = [dict(r) for r in conn.execute(sql, (phrases, "\x1f".join(tokens), limit))]
    whole = [
        r for r in rows if r["score"] >= PHRASE_MATCH or (every_word and r["score"] == len(tokens))
    ]
    out = whole or [{**r, "partial_match": True} for r in rows]
    for row in out:
        del row["score"]
    return out


# Each bucket scores every row once, in a MATERIALIZED CTE. Selected from a plain
# subquery, SQLite flattened it and ran sb_match again for the sort key of every
# matching row, and the score is a Python call.


def _search_decisions(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    # Dated, titled and sourced like query_decisions: a decision without its own
    # date takes its parent's, so a meeting's decisions no longer sink below every
    # dated one, and the row says which meeting or thread it came from.
    return _folded_bucket(
        conn,
        """
        WITH scored AS MATERIALIZED (
            SELECT id, sb_match(decision, ?, ?) AS score FROM decisions
        )
        SELECT d.id, d.email_id, d.event_id, d.teams_thread_id, d.decision, d.decided_by,
               d.decision_date,
               COALESCE(d.decision_date, e.date_received, tt.started_at, ce.start_at,
                        c.started_at) AS date,
               COALESCE(e.subject, tt.title, ce.subject, c.summary) AS email_subject,
               CASE
                   WHEN e.id IS NOT NULL THEN 'email'
                   WHEN tt.id IS NOT NULL THEN 'teams'
                   WHEN ce.id IS NOT NULL THEN 'calendar'
                   WHEN c.id IS NOT NULL THEN 'conversation'
                   ELSE 'orphan'
               END AS source,
               s.score
        FROM scored s
        JOIN decisions d ON d.id = s.id
        LEFT JOIN emails e ON e.id = d.email_id
        LEFT JOIN teams_threads tt ON tt.id = d.teams_thread_id
        LEFT JOIN calendar_events ce ON ce.id = d.event_id
        LEFT JOIN conversation_turns ct ON ct.id = d.conversation_turn_id
        LEFT JOIN conversations c ON c.id = ct.conversation_id
        WHERE s.score > 0
        ORDER BY s.score DESC, date DESC
        LIMIT ?
        """,
        keyword,
        limit,
    )


def _search_actions(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    return _folded_bucket(
        conn,
        """
        WITH scored AS MATERIALIZED (
            SELECT id, sb_match(task, ?, ?) AS score FROM action_items
        )
        SELECT a.id, a.email_id, a.task, a.owner, a.deadline, a.status,
               e.subject as email_subject, s.score
        FROM scored s
        JOIN action_items a ON a.id = s.id
        LEFT JOIN emails e ON e.id = a.email_id
        WHERE s.score > 0
        ORDER BY s.score DESC, CASE WHEN a.deadline IS NULL THEN 1 ELSE 0 END, a.deadline ASC
        LIMIT ?
        """,
        keyword,
        limit,
    )


def _search_commitments(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    if not _table_exists(conn, "commitments"):
        return []
    return _folded_bucket(
        conn,
        """
        WITH scored AS MATERIALIZED (
            SELECT id, sb_match(commitment, ?, ?) AS score FROM commitments
        )
        SELECT c.id, c.email_id, c.commitment, c.by_person, c.to_person,
               e.subject as email_subject, s.score
        FROM scored s
        JOIN commitments c ON c.id = s.id
        LEFT JOIN emails e ON e.id = c.email_id
        WHERE s.score > 0
        ORDER BY s.score DESC
        LIMIT ?
        """,
        keyword,
        limit,
    )


def _search_inline_images(conn: sqlite3.Connection, keyword: str, limit: int) -> list[dict]:
    if not _table_exists(conn, "inline_images"):
        return []
    return _folded_bucket(
        conn,
        """
        WITH scored AS MATERIALIZED (
            SELECT sha256, sb_match(vision_description, ?, ?) AS score FROM inline_images
        )
        SELECT i.sha256, i.classification, i.vision_description, i.width, i.height, s.score
        FROM scored s
        JOIN inline_images i ON i.sha256 = s.sha256
        WHERE s.score > 0
        ORDER BY s.score DESC
        LIMIT ?
        """,
        keyword,
        limit,
    )


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
        for expression, partial in fts5_query_variants(query):
            rows = conn.execute(
                """SELECT ce.id, ce.subject, ce.start_at, ce.body_summary, ce.organizer_name
                   FROM calendar_events_fts f
                   JOIN calendar_events ce ON ce.id = f.rowid
                   WHERE calendar_events_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (expression, limit),
            ).fetchall()
            if rows:
                return [{**dict(r), "partial_match": True} if partial else dict(r) for r in rows]
        return []
    except sqlite3.OperationalError as e:
        # Was `except Exception: return []`, which turned an unsanitized-query
        # crash into a confident empty calendar bucket. The sanitizer above is
        # the actual fix; this stays narrow and loud so the next tokenizer change
        # is visible instead of silently zeroing a whole result kind.
        logger.warning("recall calendar bucket skipped: %s", e)
        return []


# recall auto-injects a person and a topic dossier as a HINT beside the buckets,
# so they get a tighter cap than a direct person_context/topic_context call. With
# no cap at all these two lines were >95% of every oversized recall payload.
_CONTEXT_HINT_LIMIT = 5


def _maybe_person_context(conn: sqlite3.Connection, query: str, days: int) -> dict | None:
    """Return person_context if the query plausibly matches a known person."""
    # Cheap pre-check: is there any person whose name/email contains the query?
    if "@" in query:
        hit = conn.execute(
            "SELECT 1 FROM people WHERE LOWER(email) = LOWER(?)", (query.strip(),)
        ).fetchone()
    else:
        hit = conn.execute(
            "SELECT 1 FROM people WHERE sb_fold(name) LIKE ?", (f"%{search_fold(query)}%",)
        ).fetchone()
    if not hit:
        return None
    ctx = get_person_context(conn, query, days=days, limit=_CONTEXT_HINT_LIMIT)
    return ctx if ctx.get("person") else None


def _maybe_topic_context(conn: sqlite3.Connection, query: str, days: int) -> dict | None:
    """Return topic_context if the query plausibly matches a known topic."""
    hit = conn.execute(
        "SELECT 1 FROM topics WHERE name LIKE ?", (f"%{normalize_topic(query)}%",)
    ).fetchone()
    if not hit:
        return None
    ctx = get_topic_context(conn, query, days=days, limit=_CONTEXT_HINT_LIMIT)
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
        Dict with categorized hits across emails (incl. standalone docs),
        attachments, conversations, decisions, actions, inline_images, and teams
        threads, plus optional person_context and topic_context populated when
        the query matches a known person or topic. Always includes every kind
        key (empty list if no matches) so callers don't have to handle missing
        keys.
    """
    register_sql_functions(conn)  # sb_fold et al., whoever opened conn
    # Emails (incl. attachments + standalone docs — query_by_keyword spans
    # emails_fts, key_facts_fts, and attachment_content_fts in one call). When a
    # semantic candidate provider is injected (the MCP runtime does this), fuse the
    # keyword and semantic rankings with RRF; otherwise stay keyword-only.
    if semantic_candidates is None:
        emails = query_by_keyword(conn, query, limit=limit_per_kind)
    else:
        emails = _hybrid_emails(conn, query, limit_per_kind, semantic_candidates)

    # Conversations. search_conversations_keyword sanitizes the raw query itself
    # and falls back to any-token like every other bucket. It used to be handed
    # an already-sanitized string, which it quoted a second time.
    if _table_exists(conn, "conversation_turns_fts"):
        conversations = search_conversations_keyword(conn, query, limit=limit_per_kind)
    else:
        conversations = []

    # Attachments as their own kind. They DO feed the emails bucket via
    # query_by_keyword, but last in its source waterfall and deduped by
    # email_id — so an attachment whose parent email also matches is invisible,
    # which is the common case (a one-line "see attached" carrying the real
    # substance). This bucket surfaces the extracted text and LLM summary
    # directly, with the filename the caller needs to cite.
    attachments = search_attachments(conn, query, limit=limit_per_kind)

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
        "attachments": attachments,
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
    # Kinds whose every row came from the any-token fallback: nothing there held
    # the whole query, which a caller reading only the summary could not tell.
    # Semantic rows in the fused email bucket carry no flag either way, so they
    # count as neither whole nor partial.
    partial_kinds = [
        k
        for k, v in text_kinds.items()
        if v
        and any(r.get("partial_match") for r in v)
        and all(r.get("partial_match") or r.get("source") == "semantic" for r in v)
    ]

    return {
        "query": query,
        **text_kinds,
        "person_context": person_context,
        "topic_context": topic_context,
        "summary": {
            "total_hits": total_hits,
            "kinds_with_results": kinds_with_results,
            "partial_kinds": partial_kinds,
            "has_person_context": person_context is not None,
            "has_topic_context": topic_context is not None,
        },
    }
