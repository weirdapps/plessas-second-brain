"""brain whatsapp-sync, step 1: the pushed snapshot into whatsapp_chats and whatsapp_messages.

The snapshot is built on the Mac that runs the WhatsApp bridge
(scripts/whatsapp_snapshot.py) and pushed to the producer by
scripts/wrappers/launchd/sync-whatsapp-to-vps.sh. It is a whole-store copy, not
a delta, so this step is an idempotent upsert keyed on (chat_jid, message_id):
a second run over the same snapshot writes nothing.

It is one short write transaction and calls no model. Extraction and embedding
come later and commit per thread (see whatsapp_pipeline).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from src.config import WHATSAPP_SNAPSHOT
from src.redact import redact_secrets

# WhatsApp's own "Status" stories: posts to everyone, not a conversation.
_NOT_CONVERSATIONS = frozenset({"status@broadcast"})

# The bridge writes Go's time.Time, as "2026-09-01 10:00:00.123456789+03:00";
# history sync and older rows carry other shapes of the same instant.
_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*"
    r"(Z|[+-]\d{2}:?\d{2})?"
)


class SnapshotUnavailable(Exception):
    """The snapshot is missing or unreadable, which is not the same as empty."""


def normalize_ts(raw: object) -> str | None:
    """A bridge timestamp as ISO 8601 UTC with a Z, or None if it is not one."""
    m = _TS.match(str(raw or "").strip())
    if not m:
        return None
    day, clock, frac, tz = m.groups()
    if tz in (None, "Z"):
        tz = "+00:00"
    elif ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    micro = f".{frac[:6].ljust(6, '0')}" if frac else ""
    try:
        dt = datetime.fromisoformat(f"{day}T{clock}{micro}{tz}")
    except ValueError:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def chat_kind(jid: str) -> str:
    if jid.endswith("@g.us"):
        return "group"
    if jid.endswith("@s.whatsapp.net") or jid.endswith("@lid"):
        return "direct"
    return "other"


def _user(jid_or_user: str | None) -> str:
    """The user part of a JID ('30000000001@s.whatsapp.net' -> '30000000001')."""
    return (jid_or_user or "").split("@", 1)[0].split(":", 1)[0]


def _has_name(name: str | None) -> bool:
    """A real name, not an empty string or a bare number standing in for one."""
    stripped = (name or "").strip().lstrip("+").replace(" ", "")
    return bool(stripped) and not stripped.isdigit()


def _open_snapshot(snapshot: Path) -> sqlite3.Connection:
    if not snapshot.is_file():
        raise SnapshotUnavailable(f"no WhatsApp snapshot at {snapshot}")
    try:
        src = sqlite3.connect(f"file:{quote(str(snapshot.resolve()))}?mode=ro", uri=True)
        src.execute("SELECT count(*) FROM messages").fetchone()
    except sqlite3.Error as e:
        raise SnapshotUnavailable(f"WhatsApp snapshot unreadable: {type(e).__name__}") from None
    return src


def _read_contacts(src: sqlite3.Connection) -> dict[str, str]:
    """user -> name from the snapshot's contacts table.

    The Mac names each person in the snapshot from the bridge's contact store,
    address book first. A snapshot built before that table existed has none, and
    then nobody is named this way.
    """
    has_table = src.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'contacts'"
    ).fetchone()
    if not has_table:
        return {}
    return {
        user: name
        for user, name in src.execute("SELECT user, name FROM contacts")
        if user and _has_name(name)
    }


def _read_lid_map(src: sqlite3.Connection) -> dict[str, str]:
    """number -> anonymous id (bare users), from the snapshot's lid_map; none in a
    snapshot built before the table existed."""
    has_table = src.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lid_map'"
    ).fetchone()
    if not has_table:
        return {}
    return {pn: lid for lid, pn in src.execute("SELECT lid, pn FROM lid_map") if lid and pn}


def _moved_chats(conn: sqlite3.Connection, chats: list, lid_of: dict[str, str]) -> dict[str, str]:
    """{chat JID under a number: the JID of the same person's chat under their anonymous id}.

    WhatsApp moved contacts to anonymous ids (LIDs), and the bridge began a second
    direct chat under the id, so one contact's conversation was split: on the
    replica of 2026-10-20 one chat held three months under the number and another
    everything since under the id. A chat under a number is the id's chat whenever
    the person has one, in the snapshot or already stored.
    """
    direct = {jid for jid, _ in chats if chat_kind(jid) == "direct"}
    direct |= {r[0] for r in conn.execute("SELECT chat_jid FROM whatsapp_chats")}
    moved = {}
    for jid in direct:
        lid = lid_of.get(_user(jid)) if jid.endswith("@s.whatsapp.net") else None
        if lid and f"{lid}@lid" in direct:
            moved[jid] = f"{lid}@lid"
    return moved


def ingest_snapshot(conn: sqlite3.Connection, snapshot: Path | str = WHATSAPP_SNAPSHOT) -> dict:
    """Upsert every chat and message in `snapshot`; return the counts.

    Returns {"chats", "messages_inserted", "messages_updated", "skipped",
    "chats_merged"}. `skipped` counts rows whose timestamp is not a time, which
    cannot be placed in a session or searched by date. `chats_merged` counts the
    stored chats under a number joined to the same person's chat under an
    anonymous id (_moved_chats).
    """
    src = _open_snapshot(Path(snapshot))
    try:
        chats = src.execute("SELECT jid, name FROM chats").fetchall()
        messages = src.execute(
            "SELECT id, chat_jid, sender, content, timestamp, is_from_me, media_type, "
            "filename FROM messages"
        ).fetchall()
        contacts = _read_contacts(src)
        lid_of = _read_lid_map(src)
    finally:
        src.close()

    owner = os.environ.get("BRAIN_USER_NAME") or "me"
    # A direct chat takes the contact's name over the one the bridge kept: the
    # bridge's is often a number, and the contact's puts the address book first.
    names = {
        jid: (contacts.get(_user(jid)) if chat_kind(jid) == "direct" else None) or name
        for jid, name in chats
    }
    # Who a number is, from the chats we hold with them one to one.
    direct_names = {
        _user(jid): name for jid, name in chats if chat_kind(jid) == "direct" and _has_name(name)
    }

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = updated = skipped = 0
    touched_threads: set[int] = set()
    chat_ids: dict[str, int] = {}

    moved = _moved_chats(conn, chats, lid_of)

    def chat_id(jid: str) -> int:
        jid = moved.get(jid, jid)
        if jid not in chat_ids:
            name = names.get(jid)
            conn.execute(
                """
                INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_jid) DO UPDATE SET name = excluded.name
                WHERE excluded.name IS NOT NULL AND excluded.name <> ''
                  AND excluded.name IS NOT whatsapp_chats.name
                """,
                (jid, name if _has_name(name) else (name or None), chat_kind(jid), now),
            )
            chat_ids[jid] = conn.execute(
                "SELECT id FROM whatsapp_chats WHERE chat_jid = ?", (jid,)
            ).fetchone()[0]
        return chat_ids[jid]

    # A chat stored under a number before the snapshot carried the pairs joins the id's
    # chat. Its sessions keep their ids, so their summaries, items and vectors go with
    # them; each message keeps the JID the bridge files it under, which the upsert
    # below matches on. The emptied chat is deleted.
    merged = 0
    for number_jid, lid_jid in sorted(moved.items()):
        old = conn.execute(
            "SELECT id FROM whatsapp_chats WHERE chat_jid = ?", (number_jid,)
        ).fetchone()
        if old is None:
            continue
        target = chat_id(lid_jid)
        conn.execute("UPDATE whatsapp_messages SET chat_id = ? WHERE chat_id = ?", (target, old[0]))
        conn.execute(
            "UPDATE OR IGNORE whatsapp_threads SET chat_id = ? WHERE chat_id = ?", (target, old[0])
        )
        still_held = conn.execute(
            "SELECT 1 FROM whatsapp_messages WHERE chat_id = ?1"
            " UNION ALL SELECT 1 FROM whatsapp_threads WHERE chat_id = ?1 LIMIT 1",
            (old[0],),
        ).fetchone()
        if still_held is None:
            conn.execute("DELETE FROM whatsapp_chats WHERE id = ?", (old[0],))
            merged += 1

    for mid, jid, sender, content, ts, from_me, media_type, filename in messages:
        if not mid or not jid or jid in _NOT_CONVERSATIONS:
            continue
        sent_at = normalize_ts(ts)
        if sent_at is None:
            skipped += 1
            continue
        sender_name: str | None
        if from_me:
            sender_name = owner
        elif contacts.get(_user(sender)):
            sender_name = contacts[_user(sender)]
        elif (
            chat_kind(jid) == "direct" and _user(jid) == _user(sender) and _has_name(names.get(jid))
        ):
            sender_name = names[jid]
        else:
            sender_name = direct_names.get(_user(sender)) or _user(sender) or None
        text = redact_secrets(content) if content else (content or "")
        row = (
            jid,
            mid,
            chat_id(jid),
            _user(sender) or None,
            sender_name,
            1 if from_me else 0,
            sent_at,
            text,
            media_type or None,
            filename or None,
        )
        existing = conn.execute(
            "SELECT id, content, sender_name, media_type, thread_id, sent_at FROM whatsapp_messages "
            "WHERE chat_jid = ? AND message_id = ?",
            (jid, mid),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, sender_jid,
                    sender_name, is_from_me, sent_at, content, media_type, filename)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            inserted += 1
            continue
        # The time too: a message the bridge re-dated kept its first time, and so did
        # its session's bounds.
        if (existing[1], existing[2], existing[3], existing[5]) == (
            text,
            sender_name,
            media_type or None,
            sent_at,
        ):
            continue
        conn.execute(
            "UPDATE whatsapp_messages SET content = ?, sender_name = ?, media_type = ?, "
            "filename = ?, sent_at = ? WHERE id = ?",
            (text, sender_name, media_type or None, filename or None, sent_at, existing[0]),
        )
        updated += 1
        if existing[4] is not None:
            touched_threads.add(existing[4])

    # A renamed sender is re-extracted, and the session's participant list and bounds
    # are rebuilt the way bound_threads builds them, or the prompt keeps the old
    # numbers and a re-dated message its old place.
    for tid in touched_threads:
        participants = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT sender_name FROM whatsapp_messages "
                "WHERE thread_id = ? AND sender_name IS NOT NULL ORDER BY sender_name",
                (tid,),
            )
        ]
        started, ended = conn.execute(
            "SELECT MIN(sent_at), MAX(sent_at) FROM whatsapp_messages WHERE thread_id = ?", (tid,)
        ).fetchone()
        conn.execute(
            "UPDATE whatsapp_threads SET extraction_status = 'pending', participant_names = ?, "
            "started_at = ?, ended_at = ? WHERE id = ?",
            (json.dumps(participants, ensure_ascii=False), started, ended, tid),
        )
    conn.execute(
        """
        UPDATE whatsapp_chats SET last_message_at = (
            SELECT MAX(sent_at) FROM whatsapp_messages m WHERE m.chat_id = whatsapp_chats.id
        )
        """
    )
    conn.commit()
    return {
        "chats": len(chat_ids),
        "messages_inserted": inserted,
        "messages_updated": updated,
        "skipped": skipped,
        "chats_merged": merged,
    }
