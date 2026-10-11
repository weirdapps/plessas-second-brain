"""Drop the raw Teams payloads the store no longer needs: teams_messages.raw_json.

raw_json keeps each message as teams-cli returned it, 133 MB of the 244 MB table on the
producer, where the text, the HTML and the thread of each message are kept in columns of their
own. Two things read it. The pull compares a message it reads again with the stored payload
to find an edit or a delete (src/export/teams_export.py, _apply_later_version), so new rows
keep writing it; with the payload gone, the next pull that reads the message stores it once
more in full, the same text, and sends no thread back to extraction. And teams-backfill-replies
stores the replies a channel post's payload holds, which until #146 lived nowhere else: a post
holding a reply that is not a row of its own keeps its payload, whichever command runs first,
until the backfill has stored it.

A dry run opens the store read-only. Each batch is one short transaction, so the timers'
writers wait on it seconds at most, and a stopped run keeps what it did. The file shrinks only
after a VACUUM; until then SQLite reuses the pages.
"""

import json
import sqlite3

from src.export.teams_export import _replies_of

# Payloads dropped per transaction.
DROP_BATCH = 500


def drop_raw_json(conn: sqlite3.Connection, *, limit: int = 0, dry_run: bool = False) -> dict:
    """Set raw_json to NULL wherever nothing needs it, oldest message first, at most `limit`
    (0 = every one).

    Returns {"dropped", "bytes", "kept_replies", "kept_unreadable", "left"}: payloads dropped
    (or, in a dry run, that would be) and their size in bytes; channel posts kept because a
    reply in the payload is not stored as a row, or because the payload does not parse; and
    payloads past the limit.
    """
    counts = {"dropped": 0, "bytes": 0, "kept_replies": 0, "kept_unreadable": 0, "left": 0}
    kept = _posts_to_keep(conn, counts)
    held = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM teams_messages WHERE raw_json IS NOT NULL ORDER BY id"
        )
        if row[0] not in kept
    ]
    if limit > 0:
        counts["left"] = max(0, len(held) - limit)
        held = held[:limit]
    for start in range(0, len(held), DROP_BATCH):
        batch = held[start : start + DROP_BATCH]
        marks = ",".join("?" * len(batch))
        # Bytes, not characters: length() of a text counts characters.
        size, present = conn.execute(
            f"SELECT COALESCE(SUM(length(CAST(raw_json AS BLOB))), 0), COUNT(*)"
            f" FROM teams_messages WHERE id IN ({marks}) AND raw_json IS NOT NULL",
            batch,
        ).fetchone()
        if dry_run:
            dropped = present
        else:
            dropped = conn.execute(
                f"UPDATE teams_messages SET raw_json = NULL"
                f" WHERE id IN ({marks}) AND raw_json IS NOT NULL",
                batch,
            ).rowcount
            conn.commit()
        counts["dropped"] += dropped
        counts["bytes"] += size
    return counts


def _posts_to_keep(conn: sqlite3.Connection, counts: dict) -> set[int]:
    """The channel posts whose payload is still the only copy of a reply, or does not parse.

    The posts are the rows teams-backfill-replies reads: in a channel, with no parent. A reply
    is stored as `<chat id>::<its id>`, the key the pull and the backfill both give it.
    """
    kept: set[int] = set()
    posts = conn.execute(
        "SELECT m.id, m.chat_id, m.raw_json FROM teams_messages m"
        " JOIN teams_chats c ON c.id = m.chat_id"
        " WHERE c.chat_kind = 'channel' AND m.parent_message_id IS NULL"
        " AND m.raw_json IS NOT NULL ORDER BY m.id"
    )
    # One payload at a time: together they are tens of megabytes.
    for post_id, chat_id, raw in posts:
        try:
            item = json.loads(raw)
        except ValueError:
            item = None
        if not isinstance(item, dict):
            kept.add(post_id)
            counts["kept_unreadable"] += 1
            continue
        for reply in _replies_of(item):
            stored = conn.execute(
                "SELECT 1 FROM teams_messages WHERE teams_message_id = ?",
                (f"{chat_id}::{reply['id']}",),
            ).fetchone()
            if stored is None:
                kept.add(post_id)
                counts["kept_replies"] += 1
                break
    return kept
