"""brain whatsapp-sync, step 2: assign messages to gap-bounded sessions per chat.

The same model as Teams chat sessions (src/extract/teams_threads.py): a chat's
messages form one session until a silence longer than _GAP_HOURS, and a session
carries on across runs, so a conversation spanning an hourly sync is not cut in
two. Every touched session goes back to 'pending' so it is extracted again.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

_GAP_HOURS = 8


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)


def _title(chat_name: str | None, chat_kind: str, started_at: str) -> str:
    label = chat_name or ("group chat" if chat_kind == "group" else "chat")
    return f"WhatsApp: {label}, {started_at[:10]}"


def bound_threads(conn: sqlite3.Connection) -> dict:
    """Give every unassigned message a thread; return {"threads_created", "threads_updated"}."""
    gap = timedelta(hours=_GAP_HOURS)
    created = 0
    touched: set[int] = set()

    chats = conn.execute(
        """
        SELECT DISTINCT c.id, c.name, c.chat_kind
        FROM whatsapp_chats c
        JOIN whatsapp_messages m ON m.chat_id = c.id
        WHERE m.thread_id IS NULL
        """
    ).fetchall()

    for chat_id, chat_name, kind in chats:
        msgs = conn.execute(
            "SELECT id, message_id, sent_at FROM whatsapp_messages "
            "WHERE chat_id = ? AND thread_id IS NULL ORDER BY sent_at",
            (chat_id,),
        ).fetchall()

        prev: datetime | None = None
        floor: datetime | None = None
        current = 0
        newest = conn.execute(
            """
            SELECT m.thread_id, MIN(m.sent_at), MAX(m.sent_at)
            FROM whatsapp_messages m
            WHERE m.chat_id = ? AND m.thread_id IS NOT NULL
            GROUP BY m.thread_id
            ORDER BY MAX(m.sent_at) DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        if newest is not None:
            current = newest[0]
            prev = _parse(newest[2])
            floor = _parse(newest[1]) - gap

        for mid, message_id, sent_at in msgs:
            this = _parse(sent_at)
            new_session = (
                prev is None or (this - prev) > gap or (floor is not None and this < floor)
            )
            if new_session:
                conn.execute(
                    """
                    INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at,
                        ended_at, message_count, participant_names, title, extraction_status)
                    VALUES (?, ?, ?, ?, 0, '[]', ?, 'pending')
                    """,
                    (chat_id, message_id, sent_at, sent_at, _title(chat_name, kind, sent_at)),
                )
                current = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                created += 1
                floor = None
            conn.execute("UPDATE whatsapp_messages SET thread_id = ? WHERE id = ?", (current, mid))
            touched.add(current)
            prev = this if new_session or prev is None else max(prev, this)

    for tid in touched:
        count, started, ended = conn.execute(
            "SELECT COUNT(*), MIN(sent_at), MAX(sent_at) FROM whatsapp_messages "
            "WHERE thread_id = ?",
            (tid,),
        ).fetchone()
        names = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT sender_name FROM whatsapp_messages "
                "WHERE thread_id = ? AND sender_name IS NOT NULL ORDER BY sender_name",
                (tid,),
            )
        ]
        conn.execute(
            """
            UPDATE whatsapp_threads
            SET message_count = ?, started_at = ?, ended_at = ?, participant_names = ?,
                extraction_status = 'pending'
            WHERE id = ?
            """,
            (count, started, ended, json.dumps(names, ensure_ascii=False), tid),
        )

    conn.commit()
    return {"threads_created": created, "threads_updated": len(touched)}
