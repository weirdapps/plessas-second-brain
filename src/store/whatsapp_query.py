"""Read-side queries over whatsapp_chats / whatsapp_messages / whatsapp_threads.

Used by src/store/recall.py (the whatsapp bucket), src/mcp_server.py
(search_whatsapp) and src/store/query.py (coverage).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

# WhatsApp's coverage starts after the last silence this long, when what lies before it
# is at most STRAY_SHARE of the messages. When the bridge was first linked, WhatsApp
# gave it the last message of each older chat and nothing else: 21 messages from
# 2022-06-15 to 2026-06-30, then everything from 2026-07-02, and stats read 'first:
# 2022-06-15', so an empty answer said WhatsApp had been searched back to 2022. A long
# history with an outage in it is no sliver, and keeps its first date.
HISTORY_GAP_DAYS = 30
STRAY_SHARE = 0.05


def _at(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def whatsapp_span(conn: sqlite3.Connection) -> dict:
    """WhatsApp's coverage, as stats gives it: {"first", "last", "items"}, and
    "earliest" and "earlier_items" when stray older messages were left out of "first"."""
    try:
        times = [
            r[0] for r in conn.execute("SELECT sent_at FROM whatsapp_messages ORDER BY sent_at")
        ]
    except sqlite3.OperationalError:  # a store from before WhatsApp
        times = []
    if not times:
        return {"first": None, "last": None, "items": 0}
    cut = 0
    for i in range(1, min(len(times), int(len(times) * STRAY_SHARE) + 1)):
        before, after = _at(times[i - 1]), _at(times[i])
        if before and after and after - before > timedelta(days=HISTORY_GAP_DAYS):
            cut = i
    span: dict = {"first": times[cut], "last": times[-1], "items": len(times)}
    if cut:
        span.update(earliest=times[0], earlier_items=cut)
    return span


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
    from src.store.cross_script import name_forms
    from src.store.query import _sanitize_fts5_query, fts5_query_variants
    from src.store.teams_query import _sanitize_fts

    try:
        conn.execute("SELECT 1 FROM whatsapp_threads_fts LIMIT 0")
    except sqlite3.OperationalError:
        return []  # a store from before v26
    variants = [(_sanitize_fts(query), False)]
    for expression, partial in fts5_query_variants(query, name_forms(conn)):
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
