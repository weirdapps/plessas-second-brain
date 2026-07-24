"""Step 3: assign messages to teams_threads.

Phase 1 = channel scope only. A 'channel_post' thread is the top-level post
plus any messages whose parent_message_id points to it. Chat-session
gap-bounding is Phase 2.
"""

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta

_CHAT_THREAD_GAP_HOURS = 8


def bound_threads(conn: sqlite3.Connection) -> dict:
    """Assign thread_id to messages that don't have one. Channel messages use
    parent-id reply chains; oneOnOne/group use gap-bounded sessions
    (default _CHAT_THREAD_GAP_HOURS = 8).

    Returns:
        {"threads_created": <int>, "threads_updated": <int>}
    """
    created = 0
    updated_thread_ids: set[int] = set()

    # 1. Channel messages — existing reply-chain logic.
    channel_unassigned = conn.execute(
        """
        SELECT m.id AS mid, m.chat_id, m.teams_message_id, m.composed_at,
               m.parent_message_id, c.chat_kind
        FROM teams_messages m
        JOIN teams_chats c ON c.id = m.chat_id
        WHERE m.thread_id IS NULL
          AND m.is_system = 0
          AND c.chat_kind = 'channel'
        ORDER BY m.chat_id, m.composed_at
        """
    ).fetchall()
    for row in channel_unassigned:
        anchor_upstream = row["parent_message_id"] or _strip_chat_prefix(row["teams_message_id"])
        thread_id = _get_or_create_thread(
            conn,
            chat_id=row["chat_id"],
            anchor_message_id=anchor_upstream,
            started_at=row["composed_at"],
        )
        if thread_id is None:
            continue
        if thread_id < 0:
            created += 1
            thread_id = -thread_id
        conn.execute(
            "UPDATE teams_messages SET thread_id = ? WHERE id = ?",
            (thread_id, row["mid"]),
        )
        updated_thread_ids.add(thread_id)

    # 2. Non-channel messages — gap-bounded sessions per chat.
    chat_session_created, chat_session_touched = _bound_chat_session_threads(conn)
    created += chat_session_created
    updated_thread_ids.update(chat_session_touched)

    # 3. Recompute aggregates for every touched thread (existing logic, unchanged).
    for tid in updated_thread_ids:
        agg = conn.execute(
            """
            SELECT COUNT(*) AS msg_count,
                   MIN(composed_at) AS started,
                   MAX(composed_at) AS ended
            FROM teams_messages
            WHERE thread_id = ? AND is_system = 0
            """,
            (tid,),
        ).fetchone()
        name_rows = conn.execute(
            """
            SELECT DISTINCT sender_display_name
            FROM teams_messages
            WHERE thread_id = ?
              AND is_system = 0
              AND sender_display_name IS NOT NULL
            """,
            (tid,),
        ).fetchall()
        names = [r["sender_display_name"] for r in name_rows]
        conn.execute(
            """
            UPDATE teams_threads
            SET message_count = ?, started_at = ?, ended_at = ?,
                participant_display_names = ?,
                extraction_status = 'pending'
            WHERE id = ?
            """,
            (
                agg["msg_count"],
                agg["started"],
                agg["ended"],
                json.dumps(names),
                tid,
            ),
        )

    conn.commit()
    return {"threads_created": created, "threads_updated": len(updated_thread_ids)}


def _bound_chat_session_threads(conn: sqlite3.Connection) -> tuple[int, set[int]]:
    """Gap-bound oneOnOne + group chat messages into chat_session threads.
    Returns (created_count, touched_thread_ids).
    """
    created = 0
    touched: set[int] = set()
    gap = timedelta(hours=_CHAT_THREAD_GAP_HOURS)

    chats = conn.execute(
        """
        SELECT DISTINCT c.id, c.chat_kind, c.topic
        FROM teams_chats c
        JOIN teams_messages m ON m.chat_id = c.id
        WHERE c.chat_kind IN ('oneOnOne', 'group')
          AND m.thread_id IS NULL
          AND m.is_system = 0
        """
    ).fetchall()

    for chat in chats:
        chat_id = chat["id"]
        chat_kind = chat["chat_kind"]
        chat_topic = chat["topic"]

        msgs = conn.execute(
            """
            SELECT id, teams_message_id, composed_at
            FROM teams_messages
            WHERE chat_id = ?
              AND thread_id IS NULL
              AND is_system = 0
            ORDER BY composed_at
            """,
            (chat_id,),
        ).fetchall()

        prev_dt: datetime | None = None
        cur_thread_id: int = 0  # Will be assigned on first iteration

        for msg in msgs:
            this_dt = _parse_iso(msg["composed_at"])
            new_session = prev_dt is None or (this_dt - prev_dt) > gap

            if new_session:
                title = _chat_session_title(chat_kind, chat_topic, msg["composed_at"])
                cur_thread_id = _create_chat_session_thread(
                    conn,
                    chat_id=chat_id,
                    anchor_message_id=msg["teams_message_id"],
                    started_at=msg["composed_at"],
                    title=title,
                )
                created += 1

            conn.execute(
                "UPDATE teams_messages SET thread_id = ? WHERE id = ?",
                (cur_thread_id, msg["id"]),
            )
            touched.add(cur_thread_id)
            prev_dt = this_dt

    return created, touched


def _parse_iso(ts: str) -> datetime:
    """Parse ISO timestamp; tolerate trailing 'Z' that fromisoformat dislikes."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _chat_session_title(chat_kind: str, chat_topic: str | None, started_at: str) -> str:
    """Build the title for a new chat_session thread.

    oneOnOne: 'DM session YYYY-MM-DD' (participant name resolved later).
    group:    'Group [<topic>]: session YYYY-MM-DD' if topic, else
              'Group session YYYY-MM-DD'.
    """
    date = started_at[:10]
    if chat_kind == "oneOnOne":
        return f"DM session {date}"
    if chat_topic:
        return f"Group [{chat_topic}]: session {date}"
    return f"Group session {date}"


def _create_chat_session_thread(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    anchor_message_id: str,
    started_at: str,
    title: str,
) -> int:
    """Insert a new teams_threads row with thread_kind='chat_session'.
    Returns the new row id.
    """
    conn.execute(
        """
        INSERT INTO teams_threads(
            chat_id, thread_kind, anchor_message_id, started_at, ended_at,
            message_count, participants, participant_display_names,
            title, extraction_status
        ) VALUES (?, 'chat_session', ?, ?, ?, 0, '[]', '[]', ?, 'pending')
        """,
        (chat_id, anchor_message_id, started_at, started_at, title),
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return new_id


def _strip_chat_prefix(composite: str) -> str:
    """'<chat_pk>::<upstream_id>' -> '<upstream_id>'."""
    return composite.split("::", 1)[1] if "::" in composite else composite


def _get_or_create_thread(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    anchor_message_id: str,
    started_at: str,
) -> int | None:
    """Return thread.id; negative when newly created (caller decodes)."""
    existing = conn.execute(
        "SELECT id FROM teams_threads WHERE chat_id = ? AND anchor_message_id = ?",
        (chat_id, anchor_message_id),
    ).fetchone()
    if existing:
        return existing["id"]
    conn.execute(
        """
        INSERT INTO teams_threads(
            chat_id, thread_kind, anchor_message_id,
            started_at, ended_at, message_count, extraction_status
        ) VALUES (?, 'channel_post', ?, ?, ?, 0, 'pending')
        """,
        (chat_id, anchor_message_id, started_at, started_at),
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return -new_id


def _json_array(items: Iterable[str]) -> str:
    return json.dumps(list(items))
