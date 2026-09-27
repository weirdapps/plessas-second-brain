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
        scope: 'channel' (channels only) or 'all' (channels + DM + group).

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
    1. chatType == 'meeting' → 'meeting' (caller skips per 2026-05-04 spec).
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
    """Discover non-channel chats (oneOnOne + group) via teams-cli list-chats.

    Skips chat_kind='meeting' entirely (spec 2026-05-04). Idempotent:
    UNIQUE(teams_chat_id) drops re-inserts. Updates `topic` on existing
    rows so renamed groups stay in sync; never touches `ingest_disabled`.
    """
    chats = run_teams_cli(["list-chats"]).get("chats", [])
    inserted = 0
    updated = 0

    for chat in chats:
        kind = _classify_chat_kind(chat)
        if kind is None or kind == "meeting":
            continue

        chat_id = chat["id"]
        title = chat.get("title")  # NULL for 1-on-1, set for group
        member_mris = json.dumps(
            [
                m.get("mri")
                for m in chat.get("members", [])
                if isinstance(m, dict) and str(m.get("mri", "")).startswith("8:")
            ]
        )
        # teams-access types the key as composeTime; the lowercase spelling is
        # what the chatsvc message payloads use, so accept both.
        last_message = chat.get("lastMessage") or {}
        last_msg = last_message.get("composeTime") or last_message.get("composetime")

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

    Active = (last_message_at within 12 months) OR (any messages already in DB).
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

    # Chats with a message since their last pull go first, then the rotation by
    # oldest pull. The deadline reaches only part of the inventory per run, and
    # a busy chat that waited its turn behind hundreds of quiet ones overflowed
    # the one page teams-cli reads. julianday() because last_message_at is
    # Teams' "...Z" and last_pulled_at is Python's "...+00:00".
    rows = conn.execute(
        """
        SELECT id, teams_chat_id, chat_kind, team_uuid, channel_id, sync_state
        FROM teams_chats
        WHERE ingest_disabled = 0
          AND chat_kind IN ('channel', 'oneOnOne', 'group')
          AND (
            last_message_at IS NULL
            OR last_message_at >= ?
            OR EXISTS (SELECT 1 FROM teams_messages tm WHERE tm.chat_id = teams_chats.id)
          )
        ORDER BY
          CASE WHEN last_pulled_at IS NULL
                 OR julianday(last_message_at) > julianday(last_pulled_at)
               THEN 0 ELSE 1 END,
          last_pulled_at IS NOT NULL, last_pulled_at
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
                # oneOnOne / group — chat-scope read via chatsvc
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
    """Insert channel posts (or chat messages — Phase 2+) from one teams-cli payload.

    Returns count of newly-inserted rows. UNIQUE on teams_message_id makes it
    idempotent — duplicates silently dropped.
    """
    inserted = 0
    items = payload.get("posts") or payload.get("messages") or []

    for item in items:
        # Channel posts wrap the message in an outer envelope; chat messages are flat.
        msg = item.get("message", item)
        upstream_id = item.get("id") or msg.get("id")
        composed_at = msg.get("composetime") or item.get("latestMessageTime") or ""
        msg_type = msg.get("messageType") or msg.get("messagetype")
        content_html = msg.get("content") or ""
        content_text = (
            _strip_html(content_html)
            if (msg.get("contentType") or msg.get("contenttype")) == "html"
            else content_html
        )
        sender_display = msg.get("imDisplayName") or msg.get("imdisplayname")
        sender_mri = _extract_mri(msg.get("from"))

        # Channel: replyChainId in properties pins the parent post.
        parent_id = None
        props = msg.get("properties") or {}
        if isinstance(props, dict):
            parent_id = props.get("replyChainId")

        composite_id = f"{chat_id}::{upstream_id}"
        # Teams never passes through data/staging, so the redaction that
        # write_json_atomic applies to every staging batch never saw it. All
        # three stored copies of the message are covered: text, HTML and raw.
        content_text = redact_secrets(content_text)
        content_html = redact_secrets(content_html)
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
                    json.dumps(redact_payload(item)),
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # Already present; idempotent.

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
