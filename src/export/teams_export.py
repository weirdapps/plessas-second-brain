"""Teams ingestion — Step 1 (discover_chats) + Step 2 (pull_messages).

Phase 1 supports channel scope only. Group / 1-on-1 chats land in Phase 2/3.
"""

import json
import re
import sqlite3
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from html.entities import html5
from typing import Literal

from src.export.teams_cli import TeamsCliAuthRequired, TeamsCliError, run_teams_cli
from src.extract.html_text import html_to_text
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


# What says a message's content is HTML: an end tag, a void element (it has no
# end tag to find) or a character reference that HTML decodes. Every message the
# services send as HTML holds one, and plain text holds none unless it quotes
# markup, so "a < b", "x<y and y>z", "R&D" and "AT&T;" stay as they are. The
# shape decides, not the declared type: chats declare "text", "Text" or nothing
# and channel posts "Text" for an HTML body, and the code that waited for "html"
# stored the markup of every message as its text (#187). html_text's
# looks_like_html cannot decide it: it reads how a body opens, which suits a mail,
# and a message can open with text and then a mention.
_END_TAG = re.compile(r"</[A-Za-z][A-Za-z0-9:_-]*\s*>")
_VOID_TAG = re.compile(r"<(?:br|hr|img)\b[^<>]*>", re.IGNORECASE)
_CHAR_REF = re.compile(r"&(#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);")


def _holds_markup(content: str) -> bool:
    if _END_TAG.search(content) or _VOID_TAG.search(content):
        return True
    return any(ref[0] == "#" or f"{ref};" in html5 for ref in _CHAR_REF.findall(content))


def _message_text(content: str) -> str:
    """The text a reader sees of a message's content: converted when it holds
    markup, as it is otherwise. The conversion is a mail body's (html_text): no
    tags, entities decoded, a block or a <br> on a line of its own, and the
    visible text of a mention, a link (its address after it) and an emoji kept.
    The HTML itself is kept beside it, in content_html."""
    return html_to_text(content) if _holds_markup(content) else content


# Written by the service, not said by anyone: membership and topic changes, call
# records (who was on a call, for how long) and recording or transcript notices.
# The last three are XML, which as messages went into the threads the model reads,
# the vectors, and the caller's message counts.
SYSTEM_MESSAGE_TYPES = ("ThreadActivity/", "Event/Call", "RichText/Media_Call")


def _is_system_message(msg_type: str | None) -> bool:
    return msg_type is not None and msg_type.startswith(SYSTEM_MESSAGE_TYPES)


# A sender as the service names it: a person '8:orgid:<oid>', an app '28:<id>' or a
# thread '19:<id>@thread.v2', which writes the system messages.
_BARE_MRI = re.compile(r"\d+:\S+")


def _extract_mri(from_url: str | None) -> str | None:
    """The sender's MRI ('8:orgid:<oid>') from a message's `from`.

    Chat messages give it as a contacts URL ('.../contacts/8:orgid:<oid>') and channel
    posts bare. Only the URL was read, so every channel post was stored with no sender
    and no person: 4,817 of them on the replica of 2026-10-20.
    """
    if not from_url:
        return None
    if "/contacts/" in from_url:
        return from_url.rsplit("/contacts/", 1)[1]
    return from_url if _BARE_MRI.fullmatch(from_url) else None


def _sender_name(msg: dict) -> str | None:
    """The sender's display name, or the name in the sender's token when the message
    carries no other (1 of 4,817 channel posts)."""
    return msg.get("imDisplayName") or msg.get("imdisplayname") or msg.get("fromDisplayNameInToken")


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


# 3. "HTTP_404" / "Graph 404": the service cannot find the chat. Two meeting chats
#    answered "Location lookup failed" on every run from 2026-09-29, two errors an
#    hour for three weeks. A chat just created can 404 for a while, so a 404 is a
#    candidate only once the chat has had no good read for PERSISTENT_404_DAYS (or
#    none since it was first seen that long ago), and it goes through the breaker
#    below with the 403s. A token lapse answers 401 (exit 4, which stops the run)
#    or 403, never 404.
_NOT_FOUND_PATTERNS = ("HTTP_404", "Graph 404")
PERSISTENT_404_DAYS = 7


def _is_not_found(err: BaseException) -> bool:
    msg = str(err)
    return any(p in msg for p in _NOT_FOUND_PATTERNS)


def _unread_for_days(chat: sqlite3.Row, days: int) -> bool:
    """Whether a chat's last good read, or its discovery if it never had one, is at
    least `days` old. A stamp that does not parse says nothing, so it is not."""
    stamp = chat["last_pulled_at"] or chat["first_seen_at"]
    try:
        since = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return datetime.now(UTC) - since >= timedelta(days=days)


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


# teams-cli list-messages reads a single page, 50 messages by default. A chat that
# got more than one page between two polls kept only the newest page and lost the
# rest for good, so chat-scope reads ask for 200, the page Teams' own client
# requests.
CHAT_PAGE_SIZE = 200

# And a chat read pages back (teams-cli --until, --max-pages) until it reaches the
# newest message the chat already has stored: the busiest chat fills 200 in about 37
# hours, and the outage of 2026-10-20 lasted 23. A normal hour stops on the first
# page; ten pages is 2,000 messages, about a week of the busiest chat. Reaching the
# cap is printed, never silent.
CHAT_MAX_PAGES = 10
# What a teams-cli from before --until says to it.
_NO_PAGING = "unknown option"


def pull_messages(
    conn: sqlite3.Connection, concurrency: int = 2, deadline_s: float | None = None
) -> dict:
    """Step 2: full pull of messages for every active chat.

    Eligible = never pulled, OR last_message_at within 12 months or unknown, OR
    messages already stored. So every chat is pulled at least once whatever its
    age (2026-09-29 spec E3), and after that a quiet chat older than a year
    leaves the rotation unless it has stored messages.
    teams-cli list-messages does NOT expose a sync-state cursor. A chat already
    holding messages is read back to the newest of them (CHAT_MAX_PAGES pages of
    CHAT_PAGE_SIZE at most), a channel and a chat never read take one page, and
    UNIQUE(teams_message_id) dedups the overlap. The teams_chats.sync_state
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
        SELECT id, teams_chat_id, chat_kind, team_uuid, channel_id, sync_state,
               first_seen_at, last_pulled_at
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
    # Off for the rest of the run once the installed teams-cli refuses --until.
    paging = {"on": True}

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
                payload = run_teams_cli(args)
            else:
                # oneOnOne / group / meeting: chat-scope read via chatsvc
                payload = _read_chat(conn, chat, paging)
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

            if _is_permanent_error(e) or (
                _is_not_found(e) and _unread_for_days(chat, PERSISTENT_404_DAYS)
            ):
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


def _read_chat(conn: sqlite3.Connection, chat: sqlite3.Row, paging: dict) -> dict:
    """One chat's newest messages, back to the newest one it already has stored.

    A chat with nothing stored takes one page, as before. A teams-cli from before
    --until refuses the flag; the chat is then read one page, and so is every chat
    after it in this run, which the deploy order (CLI first) makes a one-run event.
    """
    newest = conn.execute(
        "SELECT MAX(composed_at) FROM teams_messages WHERE chat_id = ? AND composed_at <> ''",
        (chat["id"],),
    ).fetchone()[0]
    head = ["list-messages", "--page-size", str(CHAT_PAGE_SIZE)]
    # The chat id stays last: the callers' logs and tests read it there.
    tail = ["--chat", chat["teams_chat_id"]]
    if newest is None or not paging["on"]:
        return run_teams_cli(head + tail)
    try:
        payload = run_teams_cli(
            [*head, "--until", newest, "--max-pages", str(CHAT_MAX_PAGES), *tail]
        )
    except TeamsCliAuthRequired:
        raise
    except TeamsCliError as e:
        if _NO_PAGING not in (e.stderr or ""):
            raise
        paging["on"] = False
        print(
            "pull_messages: this teams-cli does not page back (no --until); reading one page"
            " a chat for the rest of the run. Update teams-access.",
            file=sys.stderr,
        )
        return run_teams_cli(head + tail)
    if isinstance(payload, dict) and payload.get("complete") is False:
        print(
            f"pull_messages: chat={chat['teams_chat_id']} stopped at the page cap"
            f" ({CHAT_MAX_PAGES} pages of {CHAT_PAGE_SIZE}) before reaching its newest stored"
            f" message ({newest}): the messages between are not read",
            file=sys.stderr,
        )
    return payload


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
    system = _is_system_message(msg_type)
    deleted = _is_deleted(msg)
    # The service empties a deleted message; blank it whatever this copy carries.
    content_html = "" if deleted else (msg.get("content") or "")
    # A system message is a record the service writes (a call, a member joining)
    # and keeps its XML: as text its fields would run together, and system
    # messages stay out of threads, extraction and vectors.
    content_text = content_html if system else _message_text(content_html)
    sender_display = _sender_name(msg)
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
                1 if system else 0,
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


def _payload_message(raw_json: str | None) -> dict:
    """The message a stored payload holds: a channel post's envelope's, or the payload."""
    try:
        raw = json.loads(raw_json) if raw_json else None
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    msg = raw.get("message", raw)
    return msg if isinstance(msg, dict) else {}


def backfill_senders(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Fill the sender of stored messages from their payloads; no Teams call.

    Until _extract_mri read a bare MRI, every channel post was stored with no
    sender_mri, so no person either (4,817 on the replica of 2026-10-20), and a
    message whose payload named its sender only in the token kept no name. This
    reads each such row's payload once and fills both, then links the senders the
    MRI cache already knows (teams_mri.link_senders); the hourly resolve_mris looks
    up the rest and links them the same way. Idempotent. A dry run counts inside a
    transaction it rolls back.

    Returns {"senders_filled", "without_sender", "names_filled", "persons_linked"}:
    rows given an MRI, rows with no MRI left (no payload, or none named), and rows
    given a name or a person, from a payload or from the cache.
    """
    if conn.in_transaction:
        conn.commit()
    try:
        counts = fill_senders(conn)
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return counts


def fill_senders(conn: sqlite3.Connection) -> dict:
    """backfill_senders' writes, in the caller's transaction, which it leaves open."""
    from src.extract.teams_mri import link_senders

    counts = {"senders_filled": 0, "without_sender": 0, "names_filled": 0, "persons_linked": 0}
    rows = conn.execute(
        "SELECT id, sender_mri, sender_display_name, raw_json FROM teams_messages"
        " WHERE sender_mri IS NULL OR COALESCE(sender_display_name, '') = '' ORDER BY id"
    ).fetchall()
    for row_id, mri, name, raw_json in rows:
        msg = _payload_message(raw_json)
        if mri is None:
            mri = _extract_mri(msg.get("from"))
            if mri is None:
                counts["without_sender"] += 1
            else:
                conn.execute("UPDATE teams_messages SET sender_mri = ? WHERE id = ?", (mri, row_id))
                counts["senders_filled"] += 1
        if not name and _sender_name(msg):
            conn.execute(
                "UPDATE teams_messages SET sender_display_name = ? WHERE id = ?",
                (_sender_name(msg), row_id),
            )
            counts["names_filled"] += 1
    linked = link_senders(conn)
    counts["persons_linked"] = linked["persons_linked"]
    counts["names_filled"] += linked["names_filled"]
    return counts


# strip_stored_html reads and converts this many rows at a time, then writes the
# ones that change in one short transaction: the timers' writers wait on it.
STRIP_HTML_BATCH = 500


def _rows_by_id(conn: sqlite3.Connection, ids: list[int]) -> Iterator[tuple[int, str, str]]:
    """(id, content_text, content_html) of each row in `ids`, a batch at a time."""
    for start in range(0, len(ids), STRIP_HTML_BATCH):
        batch = ids[start : start + STRIP_HTML_BATCH]
        marks = ", ".join("?" * len(batch))
        yield from conn.execute(
            "SELECT id, content_text, content_html FROM teams_messages"
            f" WHERE id IN ({marks}) ORDER BY id",
            batch,
        ).fetchall()


def strip_stored_html(conn: sqlite3.Connection, *, limit: int = 0, dry_run: bool = False) -> dict:
    """Convert the text of stored messages that still hold markup; no Teams or model call.

    Until the content's shape decided (#187), a message's text was converted only
    when its payload declared "html", which the chat service never does, so the
    text of every HTML message was the HTML. This converts each such row from the
    HTML it keeps (content_html), by the rule ingest now uses, credentials redacted
    as there, and writes only the rows whose text changes; the full-text index
    follows through its update trigger. It reads the kept HTML, never the text it
    wrote, so a second run changes nothing, even where the visible text holds
    markup someone typed. System messages keep their XML, as at ingest.

    Nothing goes back to extraction: thread summaries, titles and vectors stay as
    they were extracted. `limit` caps the rows changed (0 = all); a row left as it
    is does not count against it, so each run gets further than the one before. A
    dry run counts and writes nothing.
    """
    counts = {"scanned": 0, "changed": 0, "unchanged": 0, "stopped_at_limit": False}
    # Ids first, so the scan never reads what it has rewritten. Only a text that
    # holds a '<' or a '&' can hold markup; _holds_markup decides.
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM teams_messages WHERE is_system = 0 AND content_html IS NOT NULL"
            " AND (instr(content_text, '<') > 0 OR instr(content_text, '&') > 0) ORDER BY id"
        )
    ]
    pending: list[tuple[str, int, str, str]] = []

    def write() -> None:
        if dry_run:
            counts["changed"] += len(pending)
        else:
            for new_text, row_id, text, html in pending:
                # Unless a pull rewrote the row since it was read: its copy wins.
                written = conn.execute(
                    "UPDATE teams_messages SET content_text = ?"
                    " WHERE id = ? AND content_text = ? AND content_html = ?",
                    (new_text, row_id, text, html),
                ).rowcount
                counts["changed" if written else "unchanged"] += 1
            conn.commit()
        pending.clear()

    for row_id, text, html in _rows_by_id(conn, ids):
        if limit > 0 and counts["changed"] + len(pending) >= limit:
            counts["stopped_at_limit"] = True
            break
        counts["scanned"] += 1
        new_text = redact_secrets(_message_text(html)) if _holds_markup(text) else text
        if new_text == text:
            counts["unchanged"] += 1
            continue
        pending.append((new_text, row_id, text, html))
        if len(pending) == STRIP_HTML_BATCH:
            write()
    write()
    return counts
