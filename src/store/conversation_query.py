"""Query functions for conversation memory.

Provides keyword search (FTS5), semantic search, preference recall,
and context retrieval for Claude Code conversation history.
"""

import logging
import sqlite3
import warnings
from collections.abc import Callable
from typing import Any

from src.store.greek import register_sql_functions, search_fold, search_phrase
from src.store.query import _sanitize_fts5_query, fts5_query_variants

logger = logging.getLogger(__name__)

# A conversation is in a workspace when the filter is in its path or its project
# name. Callers pass either: the path is the documented form and what
# recent_conversations filters on, and the project name is its basename. The
# summary branch matched only the path and the turn branch only the name, so a
# path filter dropped every turn-level hit. Three parameters: _workspace_args.
_IN_WORKSPACE = (
    "(? = '' OR LOWER(c.workspace) LIKE LOWER(?) OR LOWER(c.project_name) LIKE LOWER(?))"
)


# An agent session: none of its turns is the owner's and some are another's
# (src/export/conversation_export.py), a teammate's or a subagent's session or one
# the owner typed nothing into. Every read here leaves them out unless asked. A
# conversation stored with no turns says nothing either way and is read as
# before. The unary + keeps SQLite on the conversation_id index. `c` is the
# conversations row.
_AGENT_SESSION = (
    "(NOT EXISTS (SELECT 1 FROM conversation_turns k WHERE k.conversation_id = c.id"
    " AND +k.speaker = 'user')"
    " AND EXISTS (SELECT 1 FROM conversation_turns k WHERE k.conversation_id = c.id"
    " AND +k.speaker = 'agent'))"
)


def _owner_sessions(include_agents: bool) -> str:
    """The SQL condition, from AND, that leaves agent sessions out unless included."""
    return "" if include_agents else f" AND NOT {_AGENT_SESSION}"


# Rows read per row returned where repeats are dropped after the read.
_REPEATS_HEADROOM = 10


def unique_facts(facts: list, text: Callable[[Any], str] = str) -> list:
    """`facts` less each one whose text, folded, repeats an earlier one's.

    A standing preference was extracted again in every session it came up in,
    once up to 162 times, the copies apart only in case, accents, spacing and
    punctuation.
    """
    seen: set[str] = set()
    kept = []
    for fact in facts:
        key = search_phrase(text(fact))
        if key not in seen:
            seen.add(key)
            kept.append(fact)
    return kept


def _workspace_args(workspace: str | None) -> tuple[str, str, str]:
    ws = workspace or ""
    return ws, f"%{ws}%", f"%{ws}%"


def conversation_ids_in_workspace(
    conn: sqlite3.Connection, workspace: str, *, include_agents: bool = False
) -> set[int]:
    """The ids of the conversations in `workspace` ('' for any), by the keyword
    search's rule, agent sessions only when included.

    Semantic search ranks among these, so its limit counts matches in the
    workspace, as the keyword search's does.
    """
    rows = conn.execute(
        f"SELECT c.id FROM conversations c WHERE {_IN_WORKSPACE}{_owner_sessions(include_agents)}",
        _workspace_args(workspace),
    )
    return {row[0] for row in rows}


def with_kinds(conn: sqlite3.Connection, rows: list[dict], key: str) -> list[dict]:
    """`rows` with each one's `kind`, 'agent' or 'owner', read off the conversation id at `key`."""
    ids = sorted({row[key] for row in rows})
    if not ids:
        return rows
    marks = ", ".join("?" * len(ids))
    agents = {
        row[0]
        for row in conn.execute(
            f"SELECT c.id FROM conversations c WHERE c.id IN ({marks}) AND {_AGENT_SESSION}", ids
        )
    }
    for row in rows:
        row["kind"] = "agent" if row[key] in agents else "owner"
    return rows


def search_conversations_keyword(
    conn: sqlite3.Connection,
    query: str,
    workspace: str | None = None,
    limit: int = 20,
    *,
    include_agents: bool = False,
) -> list[dict]:
    """Full-text search across conversation turns and summaries.

    Args:
        conn: Database connection
        query: FTS5 search query
        workspace: Optional workspace path filter
        limit: Maximum results
        include_agents: Search agent sessions too, each row then saying its kind

    Returns:
        List of matching conversations with turn excerpts. When no conversation
        carries every token, those matching any meaningful token come back
        instead, flagged partial_match.
    """
    # Defang FTS5 operator syntax before every MATCH in this module. search_emails
    # and search_attachments have always done this; these three sites did not, so
    # any natural-language query raised OperationalError straight out of the MCP
    # tool: a question mark is "fts5: syntax error near "?"", an ampersand the
    # same, and a bare word after a hyphen becomes "no such column".
    from src.store.cross_script import name_forms

    for safe_query, partial in fts5_query_variants(query, name_forms(conn)):
        results = _conversation_hits(conn, safe_query, workspace, limit, include_agents)
        if results:
            if partial:
                for row in results:
                    row["partial_match"] = True
            return with_kinds(conn, results, "conversation_id") if include_agents else results
    return []


def _conversation_hits(
    conn: sqlite3.Connection,
    safe_query: str,
    workspace: str | None,
    limit: int,
    include_agents: bool = False,
) -> list[dict]:
    """search_conversations_keyword for one sanitized MATCH expression.

    The workspace filter is in the SQL, so LIMIT counts matches in that
    workspace. Filtered afterwards, a page of matches from other workspaces
    left this empty and sent the caller to the any-token fallback.
    """
    results: list[dict] = []
    in_workspace = _workspace_args(workspace)
    owner_sessions = _owner_sessions(include_agents)

    # Search conversation-level summaries
    rows = conn.execute(
        f"""
        SELECT c.id, c.session_id, c.started_at, c.project_name,
               c.workspace, c.summary, c.turn_count,
               snippet(conversations_fts, 0, '>>>', '<<<', '...', 40) as snippet
        FROM conversations_fts
        JOIN conversations c ON c.id = conversations_fts.rowid
        WHERE conversations_fts MATCH ?
          AND {_IN_WORKSPACE}{owner_sessions}
        ORDER BY rank
        LIMIT ?
    """,
        (safe_query, *in_workspace, limit),
    ).fetchall()

    seen_ids = set()
    for r in rows:
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
            f"""
            SELECT ct.id, ct.conversation_id, ct.speaker, ct.turn_index,
                   c.session_id, c.started_at, c.project_name, c.summary,
                   snippet(conversation_turns_fts, 1, '>>>', '<<<', '...', 40) as snippet
            FROM conversation_turns_fts
            JOIN conversation_turns ct ON ct.id = conversation_turns_fts.rowid
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE conversation_turns_fts MATCH ?
              AND {_IN_WORKSPACE}{owner_sessions}
            ORDER BY rank
            LIMIT ?
        """,
            (safe_query, *in_workspace, remaining * 2),
        ).fetchall()

        for r in rows:
            if r["conversation_id"] in seen_ids:
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
    *,
    include_agents: bool = False,
) -> dict:
    """Get full context for a specific conversation.

    Args:
        conn: Database connection
        session_id: Conversation session ID
        include_agents: Read an agent session too; one is otherwise an error

    Returns:
        Dict with conversation details and kind ('owner' or 'agent'), turns,
        decisions, actions, facts
    """
    conv = conn.execute(
        f"""
        SELECT id, session_id, started_at, ended_at, workspace,
               project_name, turn_count, summary, topics_summary,
               {_AGENT_SESSION} AS agent_session
        FROM conversations c WHERE session_id = ?
    """,
        (session_id,),
    ).fetchone()

    if not conv:
        return {"error": f"Conversation not found: {session_id}"}
    if conv["agent_session"] and not include_agents:
        return {
            "error": f"Conversation {session_id} is an agent session (a teammate's or "
            "subagent's, or one with no turn the owner typed); pass include_agents to read it",
            "kind": "agent",
        }

    conv_id = conv["id"]
    result = dict(conv)
    del result["agent_session"]
    result["kind"] = "agent" if conv["agent_session"] else "owner"

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
    result["key_facts"] = unique_facts([f["fact"] for f in facts])

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
    *,
    include_agents: bool = False,
) -> list[dict]:
    """Search for user preferences and corrections on a topic.

    Searches key_facts with [PREFERENCE] and [TECHNICAL] prefixes,
    plus conversation content for user corrections and style guidance.

    Args:
        conn: Database connection
        topic: Topic to search for preferences about
        limit: Maximum results
        include_agents: Take facts from agent sessions too, whose "preferences"
            are a lead agent's orders and a skill's instructions

    Returns:
        List of preferences with source conversation context, each text once

    Deprecated: the recall_preference MCP tool is retired. Preferences belong in
    CLAUDE.md and Claude Code's own memory; this stays one release for callers.
    """
    warnings.warn(
        "recall_preferences is deprecated: preferences belong in CLAUDE.md and "
        "Claude Code's own memory",
        DeprecationWarning,
        stacklevel=2,
    )
    register_sql_functions(conn)
    results = []
    owner_sessions = _owner_sessions(include_agents)
    on_topic = f"%{search_fold(topic)}%"

    # Search prefixed key_facts first (highest signal), on the topic before the
    # LIMIT: the newest were taken first and matched after, which left most
    # topics nothing here and the full-text branch below the whole page.
    rows = conn.execute(
        f"""
        SELECT kf.fact, c.session_id, c.started_at, c.project_name, c.summary
        FROM key_facts kf
        JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id
        JOIN conversations c ON c.id = ct.conversation_id
        WHERE (kf.fact LIKE '%[PREFERENCE]%' OR kf.fact LIKE '%[TECHNICAL]%')
          AND (sb_fold(kf.fact) LIKE ? OR sb_fold(c.summary) LIKE ?){owner_sessions}
        ORDER BY c.started_at DESC
        LIMIT ?
    """,
        (on_topic, on_topic, limit * _REPEATS_HEADROOM),
    ).fetchall()

    for r in rows:
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
            f"""
            SELECT kf.fact, c.session_id, c.started_at, c.project_name
            FROM key_facts_fts
            JOIN key_facts kf ON kf.id = key_facts_fts.rowid
            JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE key_facts_fts MATCH ?{owner_sessions}
            ORDER BY rank
            LIMIT ?
        """,
            (_sanitize_fts5_query(topic), limit * _REPEATS_HEADROOM),
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
    except sqlite3.OperationalError as e:
        # Narrow, and logged. A bare `except Exception: pass` here hid the same
        # FTS5 syntax error that crashed search_conversations, so this branch
        # silently contributed nothing for years and the caller could not tell.
        logger.warning("recall_preferences FTS branch skipped: %s", e)

    # If no specific matches, return all preferences (they're all valuable)
    if not results:
        all_prefs = conn.execute(
            f"""
            SELECT kf.fact, c.session_id, c.started_at, c.project_name, c.summary
            FROM key_facts kf
            JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id
            JOIN conversations c ON c.id = ct.conversation_id
            WHERE kf.fact LIKE '%[PREFERENCE]%'{owner_sessions}
            ORDER BY c.started_at DESC
            LIMIT ?
        """,
            (limit * _REPEATS_HEADROOM,),
        ).fetchall()

        # Mark them. These rows are the most RECENT preferences, not preferences
        # about `topic`: the topic is dropped from the query entirely. Unlabelled,
        # three unrelated topics returned byte-identical lists and the caller read
        # them as answers. `topic_match: False` is the whole difference between
        # orientation and a wrong answer.
        for r in all_prefs:
            results.append(
                {
                    "preference": r["fact"],
                    "session_id": r["session_id"],
                    "date": r["started_at"],
                    "project": r["project_name"],
                    "conversation_summary": r["summary"],
                    "topic_match": False,
                    "note": (
                        f"no preference recorded for '{topic}'; these are the most "
                        "recent preferences overall, shown for orientation only"
                    ),
                }
            )

    return unique_facts(results, lambda row: row["preference"])[:limit]


def recent_conversations(
    conn: sqlite3.Connection,
    workspace: str | None = None,
    days: int = 7,
    limit: int = 10,
    *,
    include_agents: bool = False,
) -> list[dict]:
    """List recent conversations, optionally filtered by workspace.

    Args:
        conn: Database connection
        workspace: Optional workspace path substring filter
        days: Lookback period (default: 7)
        limit: Maximum results (default: 10)
        include_agents: List agent sessions too

    Returns:
        List of recent conversations with summaries

    Deprecated: the recent_conversations MCP tool is retired (Claude Code's
    /resume lists recent sessions); this stays one release for callers.
    """
    warnings.warn(
        "recent_conversations is deprecated: Claude Code's /resume lists recent sessions",
        DeprecationWarning,
        stacklevel=2,
    )
    # datetime() on both sides: started_at is stored as '2026-09-27T00:27:13Z'
    # and datetime('now', ...) renders a space, so compared as strings every
    # session from earlier on the cutoff day counted as newer than the cutoff.
    query = f"""
        SELECT id, session_id, started_at, ended_at, workspace,
               project_name, turn_count, summary, topics_summary
        FROM conversations c
        WHERE datetime(started_at) >= datetime('now', ?){_owner_sessions(include_agents)}
    """
    params: list = [f"-{days} days"]

    if workspace:
        query += " AND LOWER(workspace) LIKE LOWER(?)"
        params.append(f"%{workspace}%")

    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]
