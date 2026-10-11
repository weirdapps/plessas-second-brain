"""Teams ingestion — Step 1 (discover_chats) + Step 2 (pull_messages).

Phase 1 supports channel scope only. Group / 1-on-1 chats land in Phase 2/3.
"""

import html as html_mod
import json
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

from src.export.teams_cli import TeamsCliAuthRequired, run_teams_cli
from src.redact import redact_payload, redact_secrets

Scope = Literal["channel", "all"]


def discover_chats(conn: sqlite3.Connection, scope: Scope = "channel") -> dict:
    """Step 1: refresh teams_chats with current channel + chat inventory.

    Args:
        conn: open SQLite connection (with row_factory = sqlite3.Row).
        scope: 'channel' (channels only) or 'all' (channels + DM + group + meeting).

    Returns:
        {"chats_discovered": <int>, "chats_inserted": <int>, "chats_updated": <int>}
    """
    channel_result = _discover_channel_chats(conn)
    if scope == "channel":
        return channel_result

    chat_result = _discover_chat_chats(conn)
    return {
        "chats_discovered": channel_result["chats_discovered"],
        "chats_inserted": channel_result["chats_inserted"] + chat_result["chats_inserted"],
        "chats_updated": channel_result["chats_updated"] + chat_result["chats_updated"],
    }


def _discover_channel_chats(conn: sqlite3.Connection) -> dict:
    """Existing channel-discovery logic; extracted from discover_chats."""
    teams = run_teams_cli(["list-teams"]).get("teams", [])
    inserted = 0
    updated = 0
    discovered = 0

    for team in teams:
        team_uuid = team["id"]
        team_name = team.get("displayName", "")
        channels = run_teams_cli(["list-channels", "--team-id", team_uuid]).get("channels", [])

        for ch in channels:
            discovered += 1
            channel_id = ch["id"]
            display_name = ch.get("displayName", "")

            existing = conn.execute(
                "SELECT id FROM teams_chats WHERE teams_chat_id = ?", (channel_id,)
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE teams_chats SET team_name = ?, topic = ? WHERE id = ?",
                    (team_name, display_name, existing["id"]),
                )
                updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO teams_chats(
                        teams_chat_id, chat_kind, topic, team_uuid, team_name,
                        channel_id, first_seen_at
                    ) VALUES (?, 'channel', ?, ?, ?, ?, ?)
                    """,
                    (
                        channel_id,
                        display_name,
                        team_uuid,
                        team_name,
                        channel_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                inserted += 1

    conn.commit()
    return {
        "chats_discovered": discovered,
        "chats_inserted": inserted,
        "chats_updated": updated,
    }


def _strip_html(html_str: str | None) -> str:
    """Cheap HTML→text. Good enough for FTS; raw HTML is preserved in content_html."""
    if not html_str:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html_str, flags=re.IGNORECASE)
    # Only strip when '<' is followed by a tag-like char — otherwise it's a literal '<'.
    text = re.sub(r"<(?=[a-zA-Z/!?])[^>]+>", "", text)
    text = html_mod.unescape(text)
    return text.strip()


# Written by the service, not said by anyone: membership and topic changes, call
# records (who was on a call, for how long) and recording or transcript notices.
# The last three are XML, which as messages went into the threads the model reads,
# the vectors, and the caller's message counts.
SYSTEM_MESSAGE_TYPES = ("ThreadActivity/", "Event/Call", "RichText/Media_Call")


def _is_system_message(msg_type: str | None) -> bool:
    return msg_type is not None and msg_type.startswith(SYSTEM_MESSAGE_TYPES)


def _extract_mri(from_url: str | None) -> str | None:
    """Pull '8:orgid:<oid>' out of '.../contacts/8:orgid:<oid>'."""
    if not from_url:
        return None
    if "/contacts/" not in from_url:
        return None
    return from_url.rsplit("/contacts/", 1)[1]


# Permanent upstream errors that no amount of retrying will fix. Matching one
# of these auto-flips teams_chats.ingest_disabled=1 so we stop hammering the
# chat every hour. Manual recovery: UPDATE teams_chats SET ingest_disabled=0.
#
# 1. "no channel named 'General'": chatsvcagg requires a literal "General"
#    channel to derive the teamId; teams that renamed/archived theirs are
#    permanently unreadable via this code path. It is disabled unconditionally:
#    teams-access raises it only after Graph has listed the team's channels, so
#    a token lapse cannot produce it, and it hits every channel of the team at
#    once, so a team with five or more channels tripped the breaker below on
#    every run and was retried for ever.
# 2. "HTTP_403" / "Graph 403": no Graph permission on this channel (archived
#    teams, guest-only channels, deleted resources). A lapse 403s the same way,
#    so these go through the breaker below.
_NO_GENERAL_CHANNEL_PATTERN = "has no channel named"
_PERMANENT_ERROR_PATTERNS = (
    _NO_GENERAL_CHANNEL_PATTERN,
    "HTTP_403",
    "Graph 403",
)


def _is_permanent_error(err: BaseException) -> bool:
    msg = str(err)
    return any(p in msg for p in _PERMANENT_ERROR_PATTERNS)


# A 403 is only evidence about one chat if the rest of the run is fine. Teams
# issues per-audience tokens, so a Graph-side lapse 403s EVERY chat — and
# because the flag is one-way (recovery is a manual UPDATE), one such lapse
# permanently dropped 1,179 of 1,219 chats on prod. Ingestion then reported
# "0 messages inserted across 40 chats; 0 errors" every 30 minutes for 33 hours.
# Above these bounds the run is treated as evidence about the SERVICE, not the
# chats. The absolute floor keeps a tiny run (2 chats, 1 bad) from tripping it.
PERMANENT_DISABLE_MAX_SHARE = 0.25
PERMANENT_DISABLE_MIN_COUNT = 3


def _is_systemic_failure(permanent_count: int, attempted: int) -> bool:
    """Whether this run's permanent-looking errors indict the service, not the chats."""
    if attempted <= 0:
        return True
    ceiling = max(PERMANENT_DISABLE_MIN_COUNT, attempted * PERMANENT_DISABLE_MAX_SHARE)
    return permanent_count > ceiling


def _classify_chat_kind(chat: dict) -> str | None:
    """Map a list-chats payload entry to teams_chats.chat_kind.

    Order of precedence (matches spec § 3.1):
    1. chatType == 'meeting' → 'meeting' (stored once it has a message, 2026-09-29 spec E1).
    2. isOneOnOne flag (when present, definitive).
    3. Human-member count: 2 → 'oneOnOne', ≥3 → 'group', ≤1 → None (skip).

    A human member has an MRI starting with '8:' (Teams user prefix).
    """
    if chat.get("chatType") == "meeting":
        return "meeting"
    if chat.get("isOneOnOne"):
        return "oneOnOne"
    human_member_count = sum(
        1
        for m in chat.get("members", [])
        if isinstance(m, dict) and str(m.get("mri", "")).startswith("8:")
    )
    if human_member_count >= 3:
        return "group"
    if human_member_count == 2:
        return "oneOnOne"
    return None


def _discover_chat_chats(conn: sqlite3.Connection) -> dict:
    """Discover non-channel chats (oneOnOne, group, meeting) via teams-cli list-chats.

    Meeting chats are stored once they have a message (2026-09-29 spec E1, which
    reverses the 2026-05-04 skip). Idempotent: UNIQUE(teams_chat_id) drops
    re-inserts. Updates `topic` on existing rows so renamed groups stay in sync;
    never touches `ingest_disabled`.
    """
    chats = run_teams_cli(["list-chats"]).get("chats", [])
    inserted = 0
    updated = 0

    for chat in chats:
        kind = _classify_chat_kind(chat)
        if kind is None:
            continue

        # teams-access types the key as composeTime; the lowercase spelling is
        # what the chatsvc message payloads use, so accept both.
        last_message = chat.get("lastMessage") or {}
        last_msg = last_message.get("composeTime") or last_message.get("composetime")
        # Teams creates a meeting chat for every meeting and most stay empty.
        # One is stored once it has a message, which the listing then carries.
        if kind == "meeting" and not last_msg:
            continue

        chat_id = chat["id"]
        title = chat.get("title")  # NULL for 1-on-1, set for group and meeting
        member_mris = json.dumps(
            [
                m.get("mri")
                for m in chat.get("members", [])
                if isinstance(m, dict) and str(m.get("mri", "")).startswith("8:")
            ]
        )

        existing = conn.execute(
            "SELECT id FROM teams_chats WHERE teams_chat_id = ?", (chat_id,)
        ).fetchone()

        if existing:
            # Only ever move last_message_at forward. A listing with no last
            # message, or one older than a message pull_messages has since
            # stored, used to write NULL or the stale value over it.
            conn.execute(
                """
                UPDATE teams_chats SET topic = ?, member_mris = ?,
                    last_message_at = CASE
                        WHEN ? IS NOT NULL AND (
                            julianday(last_message_at) IS NULL
                            OR julianday(?) > julianday(last_message_at)
                        ) THEN ?
                        ELSE last_message_at
                    END
                WHERE id = ?
                """,
                (title, member_mris, last_msg, last_msg, last_msg, existing["id"]),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO teams_chats(
                    teams_chat_id, chat_kind, topic, team_uuid, team_name,
                    channel_id, member_mris, last_message_at, first_seen_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    chat_id,
                    kind,
                    title,
                    member_mris,
                    last_msg,
                    datetime.now(UTC).isoformat(),
                ),
            )
            inserted += 1

    conn.commit()
    return {"chats_inserted": inserted, "chats_updated": updated}


# teams-cli list-messages reads a single page, 50 messages by default, and does
# not page backwards. A chat that got more than one page between two polls kept
# only the newest page and lost the rest for good, so chat-scope reads ask for
# 200, the page Teams' own client requests. It is a stopgap: the real fix is for
# the CLI to follow backwardLink until it reaches a message already stored.
CHAT_PAGE_SIZE = 200


def pull_messages(
    conn: sqlite3.Connection, concurrency: int = 2, deadline_s: float | None = None
) -> dict:
    """Step 2: full pull of messages for every active chat.

    Eligible = never pulled, OR last_message_at within 12 months or unknown, OR
    messages already stored. So every chat is pulled at least once whatever its
    age (2026-09-29 spec E3), and after that a quiet chat older than a year
    leaves the rotation unless it has stored messages.
    teams-cli list-messages does NOT expose a sync-state cursor and reads one
    page only (CHAT_PAGE_SIZE for chats), so each run re-reads the newest page
    and relies on UNIQUE(teams_message_id) for dedup. The teams_chats.sync_state
    column is vestigial in Phase 1; kept for forward-compatibility.

    Args:
        conn: open SQLite connection.
        concurrency: max parallel chats. Default 2 (mirrors outlook to avoid
            chatsvc 429 throttling).
        deadline_s: Optional wall-clock budget. Once spent, no further chat is
            STARTED and the rest come back as `deferred`. A full pass over the
            restored 1,207-chat inventory is ~9 min at a measured 0.45 s each,
            against sb-teams-sync's TimeoutStartSec=600.

    Returns:
        {"chats_pulled", "messages_inserted", "errors", "deferred"}
    """
    now = datetime.now(UTC)
    # timedelta, not replace(year=...), which raises on 29 February.
    cutoff_iso = (now - timedelta(days=365)).isoformat()

    # Order (2026-09-29 spec E4): channels and chats with a message since their
    # last pull first, then chats never pulled (newest activity first, so a new
    # conversation does not queue behind a backfill), then the rotation by
    # oldest pull. The deadline reaches only part of the inventory per run, and
    # a busy chat that waited its turn behind hundreds of quiet ones overflowed
    # the one page teams-cli reads. Channels go first because discovery never
    # learns their activity. Never-pulled chats used to sort first of all, which
    # would have put a backfill of a thousand meeting chats ahead of live
    # conversations. julianday() because last_message_at is Teams' "...Z" and
    # last_pulled_at is Python's "...+00:00"; a NULL julianday sorts last under
    # DESC.
    rows = conn.execute(
        """
        SELECT id, teams_chat_id, chat_kind, team_uuid, channel_id, sync_state
        FROM teams_chats
        WHERE ingest_disabled = 0
          AND chat_kind IN ('channel', 'oneOnOne', 'group', 'meeting')
          AND (
            last_pulled_at IS NULL
            OR last_message_at IS NULL
            OR last_message_at >= ?
            OR EXISTS (SELECT 1 FROM teams_messages tm WHERE tm.chat_id = teams_chats.id)
          )
        ORDER BY
          CASE
            WHEN chat_kind = 'channel' THEN 0
            WHEN last_pulled_at IS NOT NULL
                 AND julianday(last_message_at) > julianday(last_pulled_at) THEN 0
            WHEN last_pulled_at IS NULL THEN 1
            ELSE 2
          END,
          last_pulled_at IS NOT NULL, last_pulled_at,
          julianday(last_message_at) DESC, id
        """,
        (cutoff_iso,),
    ).fetchall()

    pulled = 0
    inserted = 0
    errors = 0
    deferred = 0
    deadline = None if deadline_s is None else time.monotonic() + deadline_s
    # Disabling is decided after the run, not inside the loop: a Graph-wide auth
    # lapse 403s every chat, and applying the flag per-chat turned one transient
    # failure into 1,179 permanently dropped chats on prod. Channels and chats
    # are judged apart: channels read through chatsvcagg and chats through
    # chatsvc, on different token audiences, so a lapse can hit one kind alone.
    # Channels are about 3% of a run, so against the whole run a channel-wide
    # 403 stayed under the ceiling and disabled every channel.
    permanent_candidates: dict[str, list[int]] = {"channel": [], "chat": []}
    attempted: dict[str, int] = {"channel": 0, "chat": 0}
    # A team with no "General" channel follows a successful Graph call, so it
    # says nothing about the service and skips the breaker.
    no_general_channel: list[int] = []

    # Concurrency wired in but defaulted to sequential for Phase 1 simplicity;
    # parallelism is a Phase 2 follow-up if throughput becomes an issue.
    _ = concurrency

    for chat in rows:
        if deadline is not None and time.monotonic() >= deadline:
            deferred += 1
            continue
        audience = "channel" if chat["chat_kind"] == "channel" else "chat"
        attempted[audience] += 1
        try:
            # teams-cli list-messages has no --sync-state flag (channel reads via
            # chatsvcagg /posts don't expose a cursor). We always pull and rely on
            # UNIQUE(teams_message_id) for dedup. Phase 2 may add a max-message-age
            # cap once volume justifies it.
            if chat["chat_kind"] == "channel":
                args = [
                    "list-messages",
                    "--team",
                    chat["team_uuid"],
                    "--channel",
                    chat["channel_id"],
                ]
            else:
                # oneOnOne / group / meeting: chat-scope read via chatsvc
                args = [
                    "list-messages",
                    "--page-size",
                    str(CHAT_PAGE_SIZE),
                    "--chat",
                    chat["teams_chat_id"],
                ]
            payload = run_teams_cli(args)
            inserted += _persist_messages(conn, chat["id"], payload)
            conn.execute(
                "UPDATE teams_chats SET last_pulled_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), chat["id"]),
            )
            pulled += 1
        except TeamsCliAuthRequired:
            raise  # whole run is dead; let the orchestrator flip the sentinel
        except Exception as e:
            errors += 1
            import sys

            if _is_permanent_error(e):
                if _NO_GENERAL_CHANNEL_PATTERN in str(e):
                    no_general_channel.append(chat["id"])
                else:
                    permanent_candidates[audience].append(chat["id"])
                print(
                    f"pull_messages: candidate for disable {chat['teams_chat_id']} "
                    f"(permanent: {str(e)[:140]})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"pull_messages error chat={chat['teams_chat_id']}: {e}",
                    file=sys.stderr,
                )
        # One chat, one transaction. Committed only after the loop, the pull held
        # the write lock across the whole deadline of teams-cli calls, and every
        # other writer waiting past its busy_timeout failed "database is locked".
        conn.commit()

    # Stamp the moment, not just the flag. Without a date, a sweep that took
    # 1,179 of 1,219 chats in one pass looks exactly like archived rooms
    # accumulating a few at a time over months — and the clustering is the
    # only thing that tells those two apart after the fact.
    disabled_at = datetime.now(UTC).isoformat()
    for chat_id in no_general_channel:
        conn.execute(
            "UPDATE teams_chats SET ingest_disabled = 1, ingest_disabled_at = ? WHERE id = ?",
            (disabled_at, chat_id),
        )
    for audience, candidates in permanent_candidates.items():
        if not candidates:
            continue
        if _is_systemic_failure(len(candidates), attempted[audience]):
            import sys as _sys

            print(
                f"pull_messages: {len(candidates)}/{attempted[audience]} {audience} reads "
                "returned a permanent-looking error — treating as systemic "
                "(auth/service) and disabling none",
                file=_sys.stderr,
            )
            continue
        for chat_id in candidates:
            conn.execute(
                "UPDATE teams_chats SET ingest_disabled = 1, ingest_disabled_at = ? WHERE id = ?",
                (disabled_at, chat_id),
            )

    conn.commit()
    return {
        "chats_pulled": pulled,
        "messages_inserted": inserted,
        "errors": errors,
        "deferred": deferred,
    }


def _persist_messages(conn: sqlite3.Connection, chat_id: int, payload: dict) -> int:
    """Store the channel posts or chat messages of one teams-cli payload.

    Returns the count of newly inserted rows. UNIQUE on teams_message_id makes it
    idempotent; a message already stored is updated only when this copy is a
    later version of it (an edit or a delete, see _store_message).
    """
    inserted = 0
    items = payload.get("posts") or payload.get("messages") or []

    for item in items:
        # Channel posts wrap the message in an outer envelope; chat messages are flat.
        msg = item.get("message", item)
        upstream_id = item.get("id") or msg.get("id")
        # A post's envelope spells it composeTime; chat messages composetime.
        # latestMessageTime is the newest reply's time, so a post dated by it
        # sorted after its own answers once they were stored.
        composed_at = (
            msg.get("composetime") or msg.get("composeTime") or item.get("latestMessageTime") or ""
        )

        # Channel: replyChainId in properties pins the parent post.
        parent_id = None
        props = msg.get("properties") or {}
        if isinstance(props, dict):
            parent_id = props.get("replyChainId")

        stored = _store_message(conn, chat_id, upstream_id, msg, item, composed_at, parent_id)
        if stored == "inserted":
            inserted += 1

        # chatsvcagg /posts returns each post with its replies inside it. Only
        # the post used to be kept, which left most channel content in raw_json,
        # out of search, threads and extraction. Each reply is a row of the same
        # chat whose parent is the post, which is how bound_threads puts it in
        # the post's thread and sends that thread back to extraction.
        for reply in _replies_of(item):
            reply_at = reply.get("composeTime") or reply.get("composetime") or ""
            stored = _store_message(conn, chat_id, reply["id"], reply, reply, reply_at, upstream_id)
            if stored == "inserted":
                inserted += 1

    # Update chat's last_message_at + sync_state cursor.
    if items:
        last_ts = max(
            ((it.get("message") or it).get("composetime") or it.get("latestMessageTime") or "")
            for it in items
        )
        if last_ts:
            conn.execute(
                "UPDATE teams_chats SET last_message_at = ? WHERE id = ?",
                (last_ts, chat_id),
            )

    new_cursor = (payload.get("_metadata") or {}).get("syncState")
    if new_cursor:
        conn.execute(
            "UPDATE teams_chats SET sync_state = ? WHERE id = ?",
            (new_cursor, chat_id),
        )

    return inserted


StoreOutcome = Literal["inserted", "updated", "unchanged"]


def _replies_of(item: dict) -> list[dict]:
    """The replies a channel post carries (chatsvcagg /posts); none for a chat message."""
    if not isinstance(item.get("message"), dict):
        return []
    replies = item.get("replies")
    messages = replies.get("messages") if isinstance(replies, dict) else None
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict) and m.get("id")]


def _version_stamp(msg: dict) -> int:
    """When a message last changed, in ms since the epoch: the newest of its
    version, edit and delete times, 0 when it carries none.

    The service bumps `version` on every change and stamps `edittime` or
    `deletetime` in properties. Channels give `version` as an int, chats as a
    string of digits.
    """
    props = msg.get("properties")
    if not isinstance(props, dict):
        props = {}
    newest = 0
    for value in (msg.get("version"), props.get("edittime"), props.get("deletetime")):
        if not isinstance(value, int | str):
            continue
        try:
            newest = max(newest, int(value))
        except ValueError:
            continue
    return newest


def _is_deleted(msg: dict) -> bool:
    props = msg.get("properties")
    return isinstance(props, dict) and bool(props.get("deletetime"))


def _without_content(raw: dict) -> dict:
    """`raw` with the message's own text emptied: the envelope's message for a
    post (its replies are messages of their own), the payload itself otherwise."""
    if isinstance(raw.get("message"), dict):
        return {**raw, "message": {**raw["message"], "content": ""}}
    return {**raw, "content": ""}


def _store_message(
    conn: sqlite3.Connection,
    chat_id: int,
    upstream_id: str,
    msg: dict,
    raw: dict,
    composed_at: str,
    parent_id: str | None,
) -> StoreOutcome:
    """Insert one message, or apply a later version of one already stored.

    `msg` is the message itself and `raw` what raw_json keeps: a channel post's
    envelope, or the message for anything else.
    """
    msg_type = msg.get("messageType") or msg.get("messagetype")
    deleted = _is_deleted(msg)
    # The service empties a deleted message; blank it whatever this copy carries.
    content_html = "" if deleted else (msg.get("content") or "")
    content_text = (
        _strip_html(content_html)
        if (msg.get("contentType") or msg.get("contenttype")) == "html"
        else content_html
    )
    sender_display = msg.get("imDisplayName") or msg.get("imdisplayname")
    sender_mri = _extract_mri(msg.get("from"))
    composite_id = f"{chat_id}::{upstream_id}"
    # Teams never passes through data/staging, so the redaction that
    # write_json_atomic applies to every staging batch never saw it. All
    # three stored copies of the message are covered: text, HTML and raw.
    content_text = redact_secrets(content_text)
    content_html = redact_secrets(content_html)
    raw_json = json.dumps(redact_payload(_without_content(raw) if deleted else raw))
    try:
        conn.execute(
            """
            INSERT INTO teams_messages(
                teams_message_id, chat_id, sender_mri, sender_display_name,
                composed_at, message_type, content_text, content_html,
                parent_message_id, is_system, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                composite_id,
                chat_id,
                sender_mri,
                sender_display,
                composed_at,
                msg_type,
                content_text,
                content_html,
                parent_id,
                1 if _is_system_message(msg_type) else 0,
                raw_json,
            ),
        )
        return "inserted"
    except sqlite3.IntegrityError:
        pass  # Already stored; a later version of it is applied below.
    return _apply_later_version(
        conn, composite_id, msg, composed_at, msg_type, content_text, content_html, raw_json
    )


def _apply_later_version(
    conn: sqlite3.Connection,
    composite_id: str,
    msg: dict,
    composed_at: str,
    msg_type: str | None,
    content_text: str,
    content_html: str,
    raw_json: str,
) -> StoreOutcome:
    """Bring a stored message up to this copy when it is a later version of it.

    Every run reads each chat's newest page again, so an edit or a delete arrives
    as a copy of a message already stored, and insert-or-ignore dropped it: the
    store kept the first draft of an edited message and the text of a deleted
    one. A later version replaces the stored text, HTML and payload, and sends
    the thread back to extraction when what the message says changed. A change
    that says nothing new, such as a reaction, updates the payload alone and
    costs no model call. A same or older version changes nothing.
    """
    row = conn.execute(
        "SELECT id, thread_id, content_text, raw_json FROM teams_messages"
        " WHERE teams_message_id = ?",
        (composite_id,),
    ).fetchone()
    # The common case: the newest page read again, unchanged.
    if row is None or row["raw_json"] == raw_json:
        return "unchanged"
    try:
        stored = json.loads(row["raw_json"]) if row["raw_json"] else {}
    except ValueError:
        stored = {}
    stored_msg = stored.get("message", stored) if isinstance(stored, dict) else {}
    if not isinstance(stored_msg, dict):
        stored_msg = {}
    if _version_stamp(msg) <= _version_stamp(stored_msg):
        return "unchanged"
    is_system = None if msg_type is None else (1 if _is_system_message(msg_type) else 0)
    conn.execute(
        """
        UPDATE teams_messages
        SET composed_at = COALESCE(NULLIF(?, ''), composed_at),
            message_type = COALESCE(?, message_type),
            is_system = COALESCE(?, is_system),
            content_text = ?, content_html = ?, raw_json = ?
        WHERE id = ?
        """,
        (composed_at, msg_type, is_system, content_text, content_html, raw_json, row["id"]),
    )
    if row["thread_id"] is not None and content_text != row["content_text"]:
        conn.execute(
            "UPDATE teams_threads SET extraction_status = 'pending' WHERE id = ?",
            (row["thread_id"],),
        )
    return "updated"


def backfill_channel_replies(conn: sqlite3.Connection) -> dict:
    """Store the replies that stored channel posts hold in raw_json; no Teams call.

    Until replies were stored as rows, each channel post kept them only inside
    its stored payload. This reads those payloads once and stores every reply
    through the path a pull takes, redaction included. It also dates each post
    by its own compose time, where the old code took latestMessageTime, the
    newest reply's time. Idempotent: a second run stores nothing. Run
    bound_threads afterwards, which puts the replies in their posts' threads and
    sends those threads back to extraction.
    """
    counts = {
        "posts_read": 0,
        "unreadable": 0,
        "replies_found": 0,
        "replies_stored": 0,
        "replies_updated": 0,
        "posts_redated": 0,
    }
    # Ids first, so the replies written below never join the scan.
    post_ids = [
        r[0]
        for r in conn.execute(
            "SELECT m.id FROM teams_messages m JOIN teams_chats c ON c.id = m.chat_id"
            " WHERE c.chat_kind = 'channel' AND m.parent_message_id IS NULL"
            " AND m.raw_json IS NOT NULL ORDER BY m.id"
        ).fetchall()
    ]
    for n, post_id in enumerate(post_ids, start=1):
        row = conn.execute(
            "SELECT chat_id, teams_message_id, composed_at, raw_json FROM teams_messages"
            " WHERE id = ?",
            (post_id,),
        ).fetchone()
        try:
            item = json.loads(row["raw_json"])
        except ValueError:
            item = None
        if not isinstance(item, dict):
            counts["unreadable"] += 1
            continue
        counts["posts_read"] += 1
        msg = item.get("message")
        own_time = (
            (msg.get("composeTime") or msg.get("composetime")) if isinstance(msg, dict) else None
        )
        if own_time and own_time != row["composed_at"]:
            conn.execute(
                "UPDATE teams_messages SET composed_at = ? WHERE id = ?", (own_time, post_id)
            )
            counts["posts_redated"] += 1
        # The anchor bound_threads gives the post's thread.
        upstream_id = row["teams_message_id"].split("::", 1)[-1]
        for reply in _replies_of(item):
            counts["replies_found"] += 1
            reply_at = reply.get("composeTime") or reply.get("composetime") or ""
            outcome = _store_message(
                conn, row["chat_id"], reply["id"], reply, reply, reply_at, upstream_id
            )
            if outcome == "inserted":
                counts["replies_stored"] += 1
            elif outcome == "updated":
                counts["replies_updated"] += 1
        # Short transactions: the timers' writers wait on this one.
        if n % 100 == 0:
            conn.commit()
    conn.commit()
    return counts
