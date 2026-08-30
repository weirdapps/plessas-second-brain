"""MCP server for the second-brain knowledge repository.

Exposes the knowledge store as MCP tools for Claude Code plugins.
Run: python -m src.mcp_server
"""

from datetime import UTC

from mcp.server import MCPServer

from src.config import DEFAULT_DB
from src.store.schema import get_connection

mcp = MCPServer(
    "second-brain",
    instructions="Knowledge repository — query 41K+ emails, 22K+ attachment summaries, decisions, action items, and people context. Also includes conversation memory: search past Claude Code conversations, recall user preferences, and retrieve conversation context.",
)


def _get_conn():
    """Get a database connection with row factory."""
    return get_connection(str(DEFAULT_DB))


@mcp.tool()
def person_context(name_or_email: str, days: int = 365) -> dict:
    """Get rich context for a person: email history, topics, sentiment, decisions, open actions, communication pattern.

    Args:
        name_or_email: Person's name (partial match) or email address
        days: Lookback period in days (default: 365)
    """
    from src.store.context import get_person_context

    conn = _get_conn()
    try:
        return get_person_context(conn, name_or_email, days=days)
    finally:
        conn.close()


@mcp.tool()
def topic_context(topic: str, days: int = 365) -> dict:
    """Get context for a topic: related emails, key people, decisions, open actions, key facts.

    Args:
        topic: Topic name (partial match)
        days: Lookback period in days (default: 365)
    """
    from src.store.context import get_topic_context

    conn = _get_conn()
    try:
        return get_topic_context(conn, topic, days=days)
    finally:
        conn.close()


@mcp.tool()
def sender_brief(name_or_email: str, days: int = 365) -> dict:
    """Quick sender briefing: known status, role, email count, top topics, recent decisions, open actions.

    Args:
        name_or_email: Sender name or email address
        days: Lookback period in days (default: 365)
    """
    from src.bridge import sender_brief as _sender_brief

    conn = _get_conn()
    try:
        return _sender_brief(conn, name_or_email, days=days)
    finally:
        conn.close()


@mcp.tool()
def search_emails(query: str, search_type: str = "keyword", limit: int = 20) -> list[dict]:
    """Search emails by keyword (FTS5) or semantic similarity (embeddings).

    Args:
        query: Search query text
        search_type: "keyword" for full-text search, "semantic" for embedding similarity
        limit: Maximum results (default: 20)
    """
    conn = _get_conn()
    try:
        if search_type == "semantic":
            from src.store.embeddings import query_semantic

            return query_semantic(conn, query, limit=limit)
        else:
            from src.store.query import query_by_keyword

            return query_by_keyword(conn, query, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def recall(query: str, limit_per_kind: int = 5, days: int = 365) -> dict:
    """Unified search across every text-bearing index — emails, attachments, standalone documents, conversations, decisions, action items, and inline images. Auto-pulls person/topic context when the query matches a known person name/email or topic.

    Use this as the default 'tell me everything you know about X' entry point.
    Returns a categorized bundle so the caller can see at a glance which kinds
    matched. Each kind capped by limit_per_kind (default 5).

    Args:
        query: Free-text query (keyword, name, topic, etc.)
        limit_per_kind: Max results per category (default 5)
        days: Lookback window for person/topic context (default 365)
    """
    from src.store.embeddings import semantic_email_candidates
    from src.store.recall import recall as _recall

    conn = _get_conn()
    try:
        # Inject the semantic provider so the emails bucket is a keyword+semantic
        # RRF fusion. recall() degrades to keyword-only if the index/ADC is absent.
        return _recall(
            conn,
            query,
            limit_per_kind=limit_per_kind,
            days=days,
            semantic_candidates=semantic_email_candidates,
        )
    finally:
        conn.close()


@mcp.tool()
def query_emails(
    person: str | None = None,
    topic: str | None = None,
    keyword: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Query emails with combined filters: person, topic, keyword, date range.

    Args:
        person: Filter by person name
        topic: Filter by topic
        keyword: Full-text search keyword
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        limit: Maximum results (default: 20)
    """
    from src.store.query import query_combined

    conn = _get_conn()
    try:
        return query_combined(
            conn,
            person=person,
            topic=topic,
            keyword=keyword,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    finally:
        conn.close()


@mcp.tool()
def query_decisions(
    topic: str | None = None,
    person: str | None = None,
    days: int = 365,
    limit: int = 20,
) -> list[dict]:
    """Query recent decisions, optionally filtered by topic or person.

    Args:
        topic: Filter by topic name
        person: Filter by person who decided
        days: Lookback period (default: 30)
        limit: Maximum results (default: 20)
    """
    conn = _get_conn()
    try:
        if topic or person:
            from src.store.query import query_decisions as _qd

            return _qd(conn, topic=topic, person=person, limit=limit)
        else:
            from src.store.context import get_recent_decisions

            return get_recent_decisions(conn, days=days, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def query_actions(
    owner: str | None = None,
    status: str = "open",
    limit: int = 20,
) -> list[dict]:
    """Query action items, optionally filtered by owner and status.

    Args:
        owner: Filter by action owner name
        status: Filter by status: "open" or "completed" (default: "open")
        limit: Maximum results (default: 20)
    """
    from src.store.query import query_action_items

    conn = _get_conn()
    try:
        return query_action_items(conn, owner=owner, status=status, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def stale_threads(days: int = 5) -> dict:
    """Find stale email threads (you sent last, no reply) and overdue action items.

    Args:
        days: Stale threshold in days (default: 5)
    """
    from src.store.query import find_overdue_actions, find_stale_threads

    conn = _get_conn()
    try:
        return {
            "stale_threads": find_stale_threads(conn, days=days),
            "overdue_actions": find_overdue_actions(conn),
        }
    finally:
        conn.close()


@mcp.tool()
def meeting_prep(people: str, topic: str | None = None, days: int = 365) -> dict:
    """Generate meeting preparation dossiers for attendees.

    Args:
        people: Comma-separated list of attendee names or emails
        topic: Optional meeting topic for focused context
        days: Lookback period in days (default: 90)
    """
    from src.store.query import meeting_prep as _mp

    conn = _get_conn()
    try:
        people_list = [p.strip() for p in people.split(",")]
        return _mp(conn, people_list, topic=topic, days=days)
    finally:
        conn.close()


@mcp.tool()
def search_attachments(query: str, limit: int = 20) -> list[dict]:
    """Search attachment content (PDFs, Word, Excel, PowerPoint) using full-text search.

    Searches both extracted text and LLM-generated summaries from email attachments.
    Returns filename, parent email subject, matching snippet, and summary.

    Args:
        query: Search query text (FTS5 syntax supported)
        limit: Maximum results (default: 20)
    """
    from src.store.query import search_attachments as _search

    conn = _get_conn()
    try:
        return _search(conn, query, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def query_calendar_events(
    person: str = "",
    since: str = "",
    until: str = "",
    keyword: str = "",
    limit: int = 20,
) -> dict:
    """Query calendar events by person, date range, or keyword.

    Args:
        person: Filter by attendee name or email (partial match)
        since: Start date (YYYY-MM-DD)
        until: End date (YYYY-MM-DD)
        keyword: Full-text search in subject and body_summary
        limit: Maximum results (default: 20)
    """
    conn = _get_conn()
    try:
        query = "SELECT ce.* FROM calendar_events ce"
        conditions = []
        params: list[str | int] = []
        joins = []

        if person:
            joins.append("JOIN event_attendees ea ON ea.event_id = ce.id")
            conditions.append("(LOWER(ea.name) LIKE ? OR LOWER(ea.email) LIKE ?)")
            pattern = f"%{person.lower()}%"
            params.extend([pattern, pattern])

        if since:
            conditions.append("ce.start_at >= ?")
            params.append(since)
        if until:
            conditions.append("ce.start_at <= ?")
            params.append(until)

        if keyword:
            conditions.append(
                "ce.id IN (SELECT rowid FROM calendar_events_fts WHERE calendar_events_fts MATCH ?)"
            )
            params.append(keyword)

        sql = query
        if joins:
            sql += " " + " ".join(joins)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY ce.start_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        events = []
        for row in rows:
            event_id = row["id"]
            attendees = conn.execute(
                "SELECT name, email, response_status, is_self FROM event_attendees WHERE event_id = ?",
                (event_id,),
            ).fetchall()
            events.append(
                {
                    "id": event_id,
                    "subject": row["subject"],
                    "start_at": row["start_at"],
                    "end_at": row["end_at"],
                    "location": row["location"],
                    "organizer": row["organizer_name"] or row["organizer_email"],
                    "body_summary": row["body_summary"],
                    "is_self_organized": bool(row["is_self_organized"]),
                    "response_status": row["response_status"],
                    "attendees": [
                        {
                            "name": a["name"],
                            "email": a["email"],
                            "status": a["response_status"],
                        }
                        for a in attendees
                    ],
                }
            )
        return {"events": events, "count": len(events)}
    finally:
        conn.close()


@mcp.tool()
def stats() -> dict:
    """Get database statistics: emails, conversations, topics, people, decisions, action items, attachments, date range."""
    from src.store.query import get_stats

    conn = _get_conn()
    try:
        s = get_stats(conn)

        # Add attachment stats
        row = conn.execute("SELECT COUNT(*) as cnt FROM attachments").fetchone()
        s["total_attachments"] = row["cnt"]
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM attachment_content WHERE llm_status = 'extracted'"
        ).fetchone()
        s["attachments_extracted"] = row["cnt"]
        row = conn.execute("SELECT COUNT(*) as cnt FROM key_facts").fetchone()
        s["total_key_facts"] = row["cnt"]

        # Add conversation stats
        try:
            row = conn.execute("SELECT COUNT(*) as cnt FROM conversations").fetchone()
            s["total_conversations"] = row["cnt"]
            row = conn.execute("SELECT COUNT(*) as cnt FROM conversation_turns").fetchone()
            s["total_conversation_turns"] = row["cnt"]
        except Exception:
            s["total_conversations"] = 0
            s["total_conversation_turns"] = 0

        # Calendar stats
        try:
            s["calendar_events"] = conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[
                0
            ]
            s["event_attendees"] = conn.execute("SELECT COUNT(*) FROM event_attendees").fetchone()[
                0
            ]
        except Exception:
            pass

        return s
    finally:
        conn.close()


@mcp.tool()
def search_conversations(
    query: str,
    search_type: str = "keyword",
    workspace: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search past Claude Code conversations by keyword (FTS5) or semantic similarity.

    Args:
        query: Search query text
        search_type: "keyword" for full-text search, "semantic" for embedding similarity
        workspace: Optional workspace/project path filter
        limit: Maximum results (default: 20)
    """
    conn = _get_conn()
    try:
        if search_type == "semantic":
            from src.store.embeddings import query_semantic

            results = query_semantic(conn, query, limit=limit * 2)
            # Filter to conversation results only
            conv_results = [r for r in results if r.get("type") == "conversation"]
            if workspace:
                conv_results = [
                    r
                    for r in conv_results
                    if workspace.lower() in (r.get("project_name") or "").lower()
                ]
            return conv_results[:limit]
        else:
            from src.store.conversation_query import search_conversations_keyword

            return search_conversations_keyword(conn, query, workspace=workspace, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def conversation_context(session_id: str) -> dict:
    """Get full context for a specific Claude Code conversation: turns, decisions, actions, facts, topics.

    Args:
        session_id: Conversation session ID (UUID)
    """
    from src.store.conversation_query import get_conversation_context

    conn = _get_conn()
    try:
        return get_conversation_context(conn, session_id)
    finally:
        conn.close()


@mcp.tool()
def recall_preference(topic: str, limit: int = 20) -> list[dict]:
    """Recall user preferences and technical decisions on a topic from past conversations.

    Searches extracted preferences, corrections, and technical choices from
    conversation history. Use this to check what the user has said before about
    a topic, tool, pattern, or approach.

    Args:
        topic: Topic to search for preferences about
        limit: Maximum results (default: 20)
    """
    from src.store.conversation_query import recall_preferences

    conn = _get_conn()
    try:
        return recall_preferences(conn, topic, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def recent_conversations(
    workspace: str | None = None,
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """List recent Claude Code conversations, optionally filtered by workspace/project.

    Args:
        workspace: Optional workspace path substring filter
        days: Lookback period in days (default: 7)
        limit: Maximum results (default: 10)
    """
    from src.store.conversation_query import recent_conversations as _recent

    conn = _get_conn()
    try:
        return _recent(conn, workspace=workspace, days=days, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def sharepoint_index(
    operation: str,
    url: str | None = None,
) -> dict:
    """Operations on the sharepoint_links table.

    Operations:
      - list_stale: URLs whose last_status is anything other than 'ok'
      - list_unfetched: URLs that have never been successfully fetched
      - refetch: force a re-attempt for a specific URL (requires `url`)

    Args:
        operation: One of "list_stale", "list_unfetched", "refetch"
        url: Required only for refetch
    """
    from src.config import ATTACHMENTS_DIR
    from src.export import sharepoint_fetcher

    conn = _get_conn()
    try:
        if operation == "list_stale":
            # Enumerate the one healthy status rather than the failing ones. The
            # hardcoded IN ('stale', 'http-error') was already incomplete when it
            # was written ('auth-required' existed), and 'unsupported-host'
            # arrived later without anyone thinking to add it here, so a whole
            # class of unfetched link was invisible to this tool. A new status
            # must show up as a problem, not vanish: fail open, the way
            # scripts/health_check.py counts them.
            rows = conn.execute(
                """
                SELECT url, message_id, last_status, last_attempt_at
                FROM sharepoint_links
                WHERE last_status != 'ok'
                ORDER BY last_attempt_at DESC
                """
            ).fetchall()
            return {"links": [dict(r) for r in rows]}

        if operation == "list_unfetched":
            rows = conn.execute(
                """
                SELECT url, message_id, last_status
                FROM sharepoint_links
                WHERE fetched_path IS NULL
                ORDER BY last_attempt_at DESC
                """
            ).fetchall()
            return {"links": [dict(r) for r in rows]}

        if operation == "refetch":
            if not url:
                return {"error": "url is required for refetch"}
            out_dir = ATTACHMENTS_DIR / "sharepoint-refetch"
            result = sharepoint_fetcher.fetch_sharepoint_link(url, out_dir)
            msg_id_row = conn.execute(
                "SELECT message_id FROM sharepoint_links WHERE url = ?", (url,)
            ).fetchone()
            msg_id = msg_id_row["message_id"] if msg_id_row else "unknown"
            sharepoint_fetcher.record_link_in_db(
                conn,
                url=url,
                message_id=msg_id,
                status=result.status,
                fetched_path=str(result.local_path) if result.local_path else None,
                file_name=result.file_name,
                file_size=result.file_size,
            )
            return {
                "status": result.status,
                "local_path": str(result.local_path) if result.local_path else None,
            }

        return {"error": f"unknown operation: {operation}"}
    finally:
        conn.close()


@mcp.tool()
def attachment_image_search(
    query: str,
    limit: int = 10,
) -> dict:
    """Search classified content images by their vision-LLM description.

    Currently uses a LIKE-based substring match on vision_description.
    TODO: upgrade to vector similarity once inline_images carries an embedding
    column (no `embed_text` / `cosine_similarity_query` helpers exist yet).

    Returns ranked matches with all occurrences (sender, message_id, position).

    Args:
        query: Free-text search across vision descriptions
        limit: Maximum number of distinct images to return (default: 10)
    """
    conn = _get_conn()
    try:
        like = f"%{query}%"
        image_rows = conn.execute(
            """
            SELECT sha256, vision_description, classification
            FROM inline_images
            WHERE classification = 'content'
              AND vision_description IS NOT NULL
              AND vision_description LIKE ?
            ORDER BY classified_at DESC
            LIMIT ?
            """,
            (like, limit),
        ).fetchall()

        results = []
        for img in image_rows:
            sha = img["sha256"]
            occ_rows = conn.execute(
                """
                SELECT message_id, sender_email, position_in_body
                FROM inline_image_occurrences
                WHERE sha256 = ?
                ORDER BY position_in_body
                """,
                (sha,),
            ).fetchall()
            results.append(
                {
                    "sha256": sha,
                    "description": img["vision_description"],
                    "occurrences": [dict(o) for o in occ_rows],
                }
            )
        return {"results": results, "count": len(results)}
    finally:
        conn.close()


@mcp.tool()
def outlook_live_search(
    folder: str = "Inbox",
    since_minutes: int = 60,
    subject_contains: str | None = None,
) -> dict:
    """Query the live Outlook mailbox directly (not the indexed brain.db).

    Use for very recent messages (< 1 hour) that haven't been ingested yet.

    Args:
        folder: Mailbox folder name (default: "Inbox")
        since_minutes: Lookback window in minutes (default: 60)
        subject_contains: Optional case-insensitive subject substring filter
    """
    from datetime import datetime, timedelta

    from src.export import outlook_cli

    since_iso = (datetime.now(UTC) - timedelta(minutes=since_minutes)).isoformat()
    since_iso = since_iso.replace("+00:00", "Z")
    args = [
        "list-mail",
        "--folder",
        folder,
        "--since",
        since_iso,
        "--all",
        "--max",
        "500",
    ]
    raw = outlook_cli.run_outlook_cli(args)
    if subject_contains:
        needle = subject_contains.lower()
        raw = [m for m in raw if needle in (m.get("Subject", "") or "").lower()]
    return {"messages": raw, "count": len(raw)}


@mcp.tool()
def search_teams(query: str, kind: str = "both", limit: int = 20) -> dict:
    """Search Teams content.

    Args:
        query: Free-text query.
        kind: 'thread' (summaries+titles), 'message' (raw text), or 'both' (default).
        limit: Max results (default 20).
    """
    from src.store.teams_query import search_teams as q

    conn = _get_conn()
    try:
        return {"results": q(conn, query, kind=kind, limit=limit)}
    finally:
        conn.close()


@mcp.tool()
def teams_thread_context(thread_id: int) -> dict:
    """Full Teams thread: chat metadata, chronological messages, decisions, actions, facts.

    Args:
        thread_id: teams_threads.id
    """
    from src.store.teams_query import thread_context

    conn = _get_conn()
    try:
        return thread_context(conn, thread_id)
    finally:
        conn.close()


@mcp.tool()
def teams_chat_summary(chat_id: int, days: int = 30) -> dict:
    """Recent activity for a Teams chat/channel: threads, last messages, top senders, open actions.

    Args:
        chat_id: teams_chats.id
        days: Lookback window (default 30)
    """
    from src.store.teams_query import chat_summary

    conn = _get_conn()
    try:
        return chat_summary(conn, chat_id, days=days)
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
