"""Teams ingestion — Step 1 (discover_chats) + Step 2 (pull_messages).

Phase 1 supports channel scope only. Group / 1-on-1 chats land in Phase 2/3.
"""

import html as html_mod
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from src.export.teams_cli import TeamsCliAuthRequired, run_teams_cli

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


def _is_system_message(msg_type: str | None) -> bool:
    return msg_type is not None and msg_type.startswith("ThreadActivity/")


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
#    permanently unreadable via this code path.
# 2. "HTTP_403" / "Graph 403": no Graph permission on this channel (archived
#    teams, guest-only channels, deleted resources).
_PERMANENT_ERROR_PATTERNS = (
    "has no channel named",
    "HTTP_403",
    "Graph 403",
)


def _is_permanent_error(err: BaseException) -> bool:
    msg = str(err)
    return any(p in msg for p in _PERMANENT_ERROR_PATTERNS)


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
        last_msg = (chat.get("lastMessage") or {}).get("composetime")

        existing = conn.execute(
            "SELECT id FROM teams_chats WHERE teams_chat_id = ?", (chat_id,)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE teams_chats SET topic = ?, member_mris = ?, last_message_at = ? "
                "WHERE id = ?",
                (title, member_mris, last_msg, existing["id"]),
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


def pull_messages(conn: sqlite3.Connection, concurrency: int = 2) -> dict:
    """Step 2: full pull of messages for every active chat.

    Active = (last_message_at within 12 months) OR (any messages already in DB).
    teams-cli list-messages does NOT expose a sync-state cursor (chatsvcagg
    /posts only paginates by --page-size), so we always full-pull and rely on
    UNIQUE(teams_message_id) for dedup. The teams_chats.sync_state column is
    vestigial in Phase 1; kept for forward-compatibility.

    Args:
        conn: open SQLite connection.
        concurrency: max parallel chats. Default 2 (mirrors outlook to avoid
            chatsvc 429 throttling).

    Returns:
        {"chats_pulled": <int>, "messages_inserted": <int>, "errors": <int>}
    """
    now = datetime.now(UTC)
    cutoff_iso = now.replace(year=now.year - 1).isoformat()

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
        """,
        (cutoff_iso,),
    ).fetchall()

    pulled = 0
    inserted = 0
    errors = 0

    # Concurrency wired in but defaulted to sequential for Phase 1 simplicity;
    # parallelism is a Phase 2 follow-up if throughput becomes an issue.
    _ = concurrency

    for chat in rows:
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
                    "--chat",
                    chat["teams_chat_id"],
                ]
            payload = run_teams_cli(args)
            inserted += _persist_messages(conn, chat["id"], payload)
            pulled += 1
        except TeamsCliAuthRequired:
            raise  # whole run is dead; let the orchestrator flip the sentinel
        except Exception as e:
            errors += 1
            import sys

            if _is_permanent_error(e):
                conn.execute(
                    "UPDATE teams_chats SET ingest_disabled = 1 WHERE id = ?",
                    (chat["id"],),
                )
                print(
                    f"pull_messages: disabling chat {chat['teams_chat_id']} "
                    f"(permanent: {str(e)[:140]})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"pull_messages error chat={chat['teams_chat_id']}: {e}",
                    file=sys.stderr,
                )

    conn.commit()
    return {"chats_pulled": pulled, "messages_inserted": inserted, "errors": errors}


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
                    json.dumps(item),
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
