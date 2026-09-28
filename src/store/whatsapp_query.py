"""Read-side queries over whatsapp_chats / whatsapp_messages / whatsapp_threads.

Used by src/store/recall.py (the whatsapp bucket), src/mcp_server.py
(search_whatsapp) and src/store/query.py (coverage).
"""

from __future__ import annotations

import json
import sqlite3


def search_whatsapp(
    conn: sqlite3.Connection,
    query: str,
    chat: str | None = None,
    days: int | None = None,
    limit: int = 20,
) -> list[dict]:
    """Session summaries and titles, then raw message text, newest first, one row per session.

    Args:
        chat: only chats whose name contains this (accents and case ignored), or
            whose JID is exactly this.
        days: only sessions active in the last N days.

    Tried as the exact phrase, then every word in any order, then any meaningful
    word, with rows from that last pass flagged partial_match, as search_teams does.
    """
    from src.store.query import _sanitize_fts5_query, fts5_query_variants
    from src.store.teams_query import _sanitize_fts

    try:
        conn.execute("SELECT 1 FROM whatsapp_threads_fts LIMIT 0")
    except sqlite3.OperationalError:
        return []  # a store from before v26
    variants = [(_sanitize_fts(query), False)]
    for expression, partial in fts5_query_variants(query):
        if expression != variants[0][0] and expression != _sanitize_fts5_query(""):
            variants.append((expression, partial))
    for safe, partial in variants:
        out = _hits(conn, safe, chat, days, limit)
        if out:
            if partial:
                for row in out:
                    row["partial_match"] = True
            return out
    return []


def _filters(chat: str | None, days: int | None, alias: str) -> tuple[str, list]:
    clauses, params = [], []
    if chat:
        from src.store.greek import search_fold

        clauses.append("(c.chat_jid = ? OR sb_fold(c.name) LIKE ?)")
        params += [chat, f"%{search_fold(chat)}%"]
    if days is not None:
        clauses.append(f"julianday({alias}) >= julianday('now', ?)")
        params.append(f"-{int(days)} days")
    return "".join(f" AND {c}" for c in clauses), params


def _hits(
    conn: sqlite3.Connection, safe: str, chat: str | None, days: int | None, limit: int
) -> list[dict]:
    from src.store.greek import register_sql_functions

    register_sql_functions(conn)
    results: dict[int, dict] = {}

    where, params = _filters(chat, days, "t.ended_at")
    for r in conn.execute(
        f"""
        SELECT t.id, t.title, t.summary, t.started_at, t.ended_at, t.message_count,
               t.participant_names, c.name AS chat_name, c.chat_kind, c.id AS chat_id,
               snippet(whatsapp_threads_fts, 1, '[', ']', '...', 32) AS snippet
        FROM whatsapp_threads_fts
        JOIN whatsapp_threads t ON t.id = whatsapp_threads_fts.rowid
        JOIN whatsapp_chats c ON c.id = t.chat_id
        WHERE whatsapp_threads_fts MATCH ?{where}
        ORDER BY rank
        LIMIT ?
        """,
        (safe, *params, limit),
    ).fetchall():
        results[r["id"]] = _row(r, "thread")

    where, params = _filters(chat, days, "m.sent_at")
    for r in conn.execute(
        f"""
        SELECT m.thread_id AS id, m.sender_name, m.sent_at, t.title, t.summary, t.started_at,
               t.ended_at, t.message_count, t.participant_names, c.name AS chat_name,
               c.chat_kind, c.id AS chat_id,
               snippet(whatsapp_messages_fts, 0, '[', ']', '...', 24) AS snippet
        FROM whatsapp_messages_fts
        JOIN whatsapp_messages m ON m.id = whatsapp_messages_fts.rowid
        LEFT JOIN whatsapp_threads t ON t.id = m.thread_id
        JOIN whatsapp_chats c ON c.id = m.chat_id
        WHERE whatsapp_messages_fts MATCH ?{where}
        ORDER BY rank
        LIMIT ?
        """,
        (safe, *params, limit),
    ).fetchall():
        key = r["id"] if r["id"] is not None else -len(results) - 1
        if key in results:
            continue
        row = _row(r, "message")
        row["matched_sender"] = r["sender_name"]
        row["matched_at"] = r["sent_at"]
        results[key] = row

    out = list(results.values())
    out.sort(key=lambda x: x.get("ended_at") or x.get("matched_at") or "", reverse=True)
    return out[:limit]


def _row(r: sqlite3.Row, match: str) -> dict:
    return {
        "thread_id": r["id"],
        "chat_id": r["chat_id"],
        "chat_name": r["chat_name"],
        "chat_kind": r["chat_kind"],
        "title": r["title"],
        "summary": r["summary"],
        "snippet": r["snippet"],
        "started_at": r["started_at"],
        "ended_at": r["ended_at"],
        "message_count": r["message_count"],
        "participants": json.loads(r["participant_names"] or "[]"),
        "match": match,
    }
