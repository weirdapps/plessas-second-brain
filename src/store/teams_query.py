"""Read-side queries over teams_chats / teams_messages / teams_threads.

Used by:
- src/cli.py — teams-search, teams-thread, teams-chat, teams-stats
- src/store/recall.py — _search_teams branch
- src/mcp_server.py — three new MCP tools
"""

import json
import sqlite3


def search_teams(
    conn: sqlite3.Connection, query: str, kind: str = "both", limit: int = 20
) -> list[dict]:
    """Hit teams_threads_fts (summary/title) and teams_messages_fts (raw text).

    Args:
        kind: 'thread' | 'message' | 'both'.

    Returns:
        Deduped-by-thread list, newest first.
    """
    safe = _sanitize_fts(query)
    results: dict[int, dict] = {}

    if kind in ("thread", "both"):
        for r in conn.execute(
            """
            SELECT t.id, t.title, t.summary, t.started_at, t.ended_at,
                   t.message_count, t.participant_display_names,
                   c.team_name, c.topic, c.chat_kind, c.id AS chat_id,
                   snippet(teams_threads_fts, 1, '[', ']', '...', 32) AS snippet
            FROM teams_threads_fts
            JOIN teams_threads t ON t.id = teams_threads_fts.rowid
            JOIN teams_chats c ON c.id = t.chat_id
            WHERE teams_threads_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe, limit),
        ).fetchall():
            results[r["id"]] = {
                "thread_id": r["id"],
                "chat_id": r["chat_id"],
                "title": r["title"],
                "summary": r["summary"],
                "snippet": r["snippet"],
                "team_name": r["team_name"],
                "channel_topic": r["topic"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "message_count": r["message_count"],
                "participants": json.loads(r["participant_display_names"] or "[]"),
                "match": "thread",
            }

    if kind in ("message", "both"):
        for r in conn.execute(
            """
            SELECT m.thread_id, m.content_text, m.sender_display_name, m.composed_at,
                   t.title, t.summary, t.started_at, t.ended_at, t.message_count,
                   t.participant_display_names,
                   c.team_name, c.topic, c.chat_kind, c.id AS chat_id,
                   snippet(teams_messages_fts, 0, '[', ']', '...', 24) AS snippet
            FROM teams_messages_fts
            JOIN teams_messages m ON m.id = teams_messages_fts.rowid
            LEFT JOIN teams_threads t ON t.id = m.thread_id
            JOIN teams_chats c ON c.id = m.chat_id
            WHERE teams_messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe, limit),
        ).fetchall():
            tid = r["thread_id"] or -1  # ungrouped messages get a negative bucket
            if tid in results:
                continue
            results[tid] = {
                "thread_id": r["thread_id"],
                "chat_id": r["chat_id"],
                "title": r["title"],
                "summary": r["summary"],
                "snippet": r["snippet"],
                "team_name": r["team_name"],
                "channel_topic": r["topic"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "message_count": r["message_count"],
                "participants": json.loads(r["participant_display_names"] or "[]"),
                "match": "message",
                "matched_sender": r["sender_display_name"],
                "matched_at": r["composed_at"],
            }

    out = list(results.values())
    out.sort(key=lambda x: x.get("ended_at") or "", reverse=True)
    return out[:limit]


def thread_context(conn: sqlite3.Connection, thread_id: int) -> dict:
    """Return the full thread: chat, messages chronologically, decisions, actions, facts."""
    t = conn.execute(
        """
        SELECT t.*, c.team_name, c.topic AS channel_topic, c.chat_kind, c.teams_chat_id
        FROM teams_threads t JOIN teams_chats c ON c.id = t.chat_id
        WHERE t.id = ?
        """,
        (thread_id,),
    ).fetchone()
    if not t:
        return {"error": "thread not found"}

    msgs = conn.execute(
        """
        SELECT composed_at, sender_display_name, sender_mri, sender_person_id,
               content_text, parent_message_id, is_system
        FROM teams_messages
        WHERE thread_id = ?
        ORDER BY composed_at
        """,
        (thread_id,),
    ).fetchall()

    decisions = conn.execute(
        "SELECT decision, decided_by, decision_date FROM decisions WHERE teams_thread_id = ?",
        (thread_id,),
    ).fetchall()
    actions = conn.execute(
        "SELECT task, owner, deadline, status FROM action_items WHERE teams_thread_id = ?",
        (thread_id,),
    ).fetchall()
    facts = conn.execute(
        "SELECT fact FROM key_facts WHERE teams_thread_id = ?", (thread_id,)
    ).fetchall()

    return {
        "thread": dict(t),
        "messages": [dict(m) for m in msgs],
        "decisions": [dict(d) for d in decisions],
        "action_items": [dict(a) for a in actions],
        "key_facts": [dict(f) for f in facts],
    }


def chat_summary(conn: sqlite3.Connection, chat_id: int, days: int = 30) -> dict:
    """Recent activity for one chat: thread count, last messages, top participants, open actions."""
    chat = conn.execute("SELECT * FROM teams_chats WHERE id = ?", (chat_id,)).fetchone()
    if not chat:
        return {"error": "chat not found"}

    cutoff = conn.execute(
        "SELECT datetime('now', ?) AS cutoff", (f"-{int(days)} days",)
    ).fetchone()["cutoff"]

    threads = conn.execute(
        """
        SELECT id, title, summary, started_at, ended_at, message_count
        FROM teams_threads
        WHERE chat_id = ? AND ended_at >= ?
        ORDER BY ended_at DESC
        LIMIT 20
        """,
        (chat_id, cutoff),
    ).fetchall()

    last_messages = conn.execute(
        """
        SELECT composed_at, sender_display_name, content_text
        FROM teams_messages
        WHERE chat_id = ?
        ORDER BY composed_at DESC
        LIMIT 10
        """,
        (chat_id,),
    ).fetchall()

    top_senders = conn.execute(
        """
        SELECT sender_display_name AS name, COUNT(*) AS n
        FROM teams_messages
        WHERE chat_id = ? AND composed_at >= ?
        GROUP BY sender_display_name
        ORDER BY n DESC
        LIMIT 10
        """,
        (chat_id, cutoff),
    ).fetchall()

    open_actions = conn.execute(
        """
        SELECT a.task, a.owner, a.deadline
        FROM action_items a
        JOIN teams_threads t ON t.id = a.teams_thread_id
        WHERE t.chat_id = ? AND a.status = 'open'
        ORDER BY COALESCE(a.deadline, '9999')
        LIMIT 20
        """,
        (chat_id,),
    ).fetchall()

    return {
        "chat": dict(chat),
        "threads": [dict(t) for t in threads],
        "last_messages": [dict(m) for m in last_messages],
        "top_senders": [dict(s) for s in top_senders],
        "open_actions": [dict(a) for a in open_actions],
    }


def stats(conn: sqlite3.Connection) -> dict:
    """Counts + extremes for `brain teams-stats`."""
    out = {}
    out["total_teams_chats"] = conn.execute("SELECT COUNT(*) FROM teams_chats").fetchone()[0]
    out["total_teams_threads"] = conn.execute("SELECT COUNT(*) FROM teams_threads").fetchone()[0]
    out["total_teams_messages"] = conn.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0]
    extremes = conn.execute(
        "SELECT MIN(composed_at) AS earliest, MAX(composed_at) AS latest FROM teams_messages"
    ).fetchone()
    out["earliest_teams_message"] = extremes["earliest"]
    out["latest_teams_message"] = extremes["latest"]
    out["teams_extraction_pending"] = conn.execute(
        "SELECT COUNT(*) FROM teams_threads WHERE extraction_status = 'pending'"
    ).fetchone()[0]
    out["teams_extraction_failed"] = conn.execute(
        "SELECT COUNT(*) FROM teams_threads WHERE extraction_status = 'failed'"
    ).fetchone()[0]
    return out


def _sanitize_fts(q: str) -> str:
    """Escape FTS5 punctuation by double-quoting the whole query."""
    return '"' + q.replace('"', '""') + '"'
