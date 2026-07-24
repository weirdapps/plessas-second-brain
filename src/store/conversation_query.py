"""Query functions for conversation memory.

Provides keyword search (FTS5), semantic search, preference recall,
and context retrieval for Claude Code conversation history.
"""

import sqlite3


def search_conversations_keyword(
    conn: sqlite3.Connection,
    query: str,
    workspace: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Full-text search across conversation turns and summaries.

    Args:
        conn: Database connection
        query: FTS5 search query
        workspace: Optional workspace path filter
        limit: Maximum results

    Returns:
        List of matching conversations with turn excerpts
    """
    results = []

    # Search conversation-level summaries
    rows = conn.execute(
        """
        SELECT c.id, c.session_id, c.started_at, c.project_name,
               c.workspace, c.summary, c.turn_count,
               snippet(conversations_fts, 0, '>>>', '<<<', '...', 40) as snippet
        FROM conversations_fts
        JOIN conversations c ON c.id = conversations_fts.rowid
        WHERE conversations_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """,
        (query, limit),
    ).fetchall()

    seen_ids = set()
    for r in rows:
        if workspace and workspace.lower() not in (r["workspace"] or "").lower():
            continue
        seen_ids.add(r["id"])
        results.append(
            {
                "conversation_id": r["id"],
                "session_id": r["session_id"],
                "date": r["started_at"],
                "project_name": r["project_name"],
                "summary": r["summary"],
                "turn_count": r["turn_count"],
                "snippet": r["snippet"],
                "match_type": "summary",
            }
        )

    # Search turn-level content
    remaining = limit - len(results)
    if remaining > 0:
        rows = conn.execute(
            """
            SELECT ct.id, ct.conversation_id, ct.speaker, ct.turn_index,
                   c.session_id, c.started_at, c.project_name, c.summary,
                   snippet(conversation_turns_fts, 1, '>>>', '<<<', '...', 40) as snippet
            FROM conversation_turns_fts
            JOIN conversation_turns ct ON ct.id = conversation_turns_fts.rowid
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE conversation_turns_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """,
            (query, remaining * 2),
        ).fetchall()

        for r in rows:
            if r["conversation_id"] in seen_ids:
                continue
            if workspace and workspace.lower() not in (r["project_name"] or "").lower():
                continue
            seen_ids.add(r["conversation_id"])
            results.append(
                {
                    "conversation_id": r["conversation_id"],
                    "session_id": r["session_id"],
                    "date": r["started_at"],
                    "project_name": r["project_name"],
                    "summary": r["summary"],
                    "snippet": r["snippet"],
                    "match_type": "turn_content",
                    "turn_speaker": r["speaker"],
                }
            )
            if len(results) >= limit:
                break

    return results


def get_conversation_context(
    conn: sqlite3.Connection,
    session_id: str,
) -> dict:
    """Get full context for a specific conversation.

    Args:
        conn: Database connection
        session_id: Conversation session ID

    Returns:
        Dict with conversation details, turns, decisions, actions, facts
    """
    conv = conn.execute(
        """
        SELECT id, session_id, started_at, ended_at, workspace,
               project_name, turn_count, summary, topics_summary
        FROM conversations WHERE session_id = ?
    """,
        (session_id,),
    ).fetchone()

    if not conv:
        return {"error": f"Conversation not found: {session_id}"}

    conv_id = conv["id"]
    result = dict(conv)

    # Get turns (summarized, not full content)
    turns = conn.execute(
        """
        SELECT turn_index, timestamp, speaker, summary,
               content_length, has_code, has_tool_use
        FROM conversation_turns
        WHERE conversation_id = ?
        ORDER BY turn_index
    """,
        (conv_id,),
    ).fetchall()
    result["turns"] = [dict(t) for t in turns]

    # Get decisions from this conversation
    decisions = conn.execute(
        """
        SELECT d.decision, d.decided_by
        FROM decisions d
        JOIN conversation_turns ct ON ct.id = d.conversation_turn_id
        WHERE ct.conversation_id = ?
    """,
        (conv_id,),
    ).fetchall()
    result["decisions"] = [dict(d) for d in decisions]

    # Get action items
    actions = conn.execute(
        """
        SELECT ai.task, ai.owner, ai.deadline, ai.status
        FROM action_items ai
        JOIN conversation_turns ct ON ct.id = ai.conversation_turn_id
        WHERE ct.conversation_id = ?
    """,
        (conv_id,),
    ).fetchall()
    result["action_items"] = [dict(a) for a in actions]

    # Get key facts (including preferences and technical decisions)
    facts = conn.execute(
        """
        SELECT kf.fact
        FROM key_facts kf
        JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id
        WHERE ct.conversation_id = ?
    """,
        (conv_id,),
    ).fetchall()
    result["key_facts"] = [f["fact"] for f in facts]

    # Get topics
    topics = conn.execute(
        """
        SELECT t.display_name
        FROM conversation_topics ct_topics
        JOIN topics t ON t.id = ct_topics.topic_id
        WHERE ct_topics.conversation_id = ?
    """,
        (conv_id,),
    ).fetchall()
    result["topics"] = [t["display_name"] for t in topics]

    return result


def recall_preferences(
    conn: sqlite3.Connection,
    topic: str,
    limit: int = 20,
) -> list[dict]:
    """Search for user preferences and corrections on a topic.

    Searches key_facts with [PREFERENCE] and [TECHNICAL] prefixes,
    plus conversation content for user corrections and style guidance.

    Args:
        conn: Database connection
        topic: Topic to search for preferences about
        limit: Maximum results

    Returns:
        List of preferences with source conversation context
    """
    results = []

    # Search prefixed key_facts first (highest signal)
    rows = conn.execute(
        """
        SELECT kf.fact, c.session_id, c.started_at, c.project_name, c.summary
        FROM key_facts kf
        JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id
        JOIN conversations c ON c.id = ct.conversation_id
        WHERE (kf.fact LIKE '%[PREFERENCE]%' OR kf.fact LIKE '%[TECHNICAL]%')
        ORDER BY c.started_at DESC
        LIMIT ?
    """,
        (limit,),
    ).fetchall()

    for r in rows:
        # Check if topic is relevant (simple substring match)
        if topic.lower() in r["fact"].lower() or topic.lower() in (r["summary"] or "").lower():
            results.append(
                {
                    "preference": r["fact"],
                    "session_id": r["session_id"],
                    "date": r["started_at"],
                    "project": r["project_name"],
                    "conversation_summary": r["summary"],
                }
            )

    # Also search FTS for broader matches
    try:
        fts_rows = conn.execute(
            """
            SELECT kf.fact, c.session_id, c.started_at, c.project_name
            FROM key_facts_fts
            JOIN key_facts kf ON kf.id = key_facts_fts.rowid
            JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE key_facts_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """,
            (topic, limit),
        ).fetchall()

        seen = {r["preference"] for r in results}
        for r in fts_rows:
            if r["fact"] not in seen:
                results.append(
                    {
                        "preference": r["fact"],
                        "session_id": r["session_id"],
                        "date": r["started_at"],
                        "project": r["project_name"],
                    }
                )
                seen.add(r["fact"])
    except Exception:
        pass  # FTS may not have conversation-linked key_facts yet

    # If no specific matches, return all preferences (they're all valuable)
    if not results:
        all_prefs = conn.execute(
            """
            SELECT kf.fact, c.session_id, c.started_at, c.project_name, c.summary
            FROM key_facts kf
            JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE kf.fact LIKE '%[PREFERENCE]%'
            ORDER BY c.started_at DESC
            LIMIT ?
        """,
            (limit,),
        ).fetchall()

        for r in all_prefs:
            results.append(
                {
                    "preference": r["fact"],
                    "session_id": r["session_id"],
                    "date": r["started_at"],
                    "project": r["project_name"],
                    "conversation_summary": r["summary"],
                }
            )

    return results[:limit]


def recent_conversations(
    conn: sqlite3.Connection,
    workspace: str | None = None,
    days: int = 7,
    limit: int = 10,
) -> list[dict]:
    """List recent conversations, optionally filtered by workspace.

    Args:
        conn: Database connection
        workspace: Optional workspace path substring filter
        days: Lookback period (default: 7)
        limit: Maximum results (default: 10)

    Returns:
        List of recent conversations with summaries
    """
    query = """
        SELECT id, session_id, started_at, ended_at, workspace,
               project_name, turn_count, summary, topics_summary
        FROM conversations
        WHERE started_at >= datetime('now', ?)
    """
    params: list = [f"-{days} days"]

    if workspace:
        query += " AND LOWER(workspace) LIKE LOWER(?)"
        params.append(f"%{workspace}%")

    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]
