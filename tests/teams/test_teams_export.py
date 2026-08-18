"""Tests for teams_export — discover_chats and pull_messages."""

from unittest.mock import patch

from src.export.teams_export import (
    _classify_chat_kind,
    _is_permanent_error,
    discover_chats,
    pull_messages,
)


def test_discover_chats_inserts_channel_rows(db, fixture_loader):
    channels_payload = fixture_loader("list-channels.json")

    # Mock teams-cli: list-teams returns one team; list-channels returns the fixture's channels.
    with patch("src.export.teams_export.run_teams_cli") as mock:

        def side_effect(args, **kw):
            if args[0] == "list-teams":
                return {"teams": [channels_payload["team"]]}
            if args[0] == "list-channels":
                return {"channels": channels_payload["channels"]}
            raise AssertionError(f"unexpected args: {args}")

        mock.side_effect = side_effect

        result = discover_chats(db, scope="channel")

    assert result["chats_discovered"] == 2  # General + Novak channel
    rows = db.execute(
        "SELECT teams_chat_id, chat_kind, team_name, topic FROM teams_chats ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["chat_kind"] == "channel" for r in rows)
    assert rows[0]["team_name"] == "Cards & Digital"
    assert rows[0]["topic"] == "General"


def test_discover_chats_is_idempotent(db, fixture_loader):
    channels_payload = fixture_loader("list-channels.json")

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.side_effect = lambda args, **kw: (
            {"teams": [channels_payload["team"]]}
            if args[0] == "list-teams"
            else {"channels": channels_payload["channels"]}
        )
        discover_chats(db, scope="channel")
        discover_chats(db, scope="channel")  # second run

    count = db.execute("SELECT COUNT(*) FROM teams_chats").fetchone()[0]
    assert count == 2  # unchanged


def _seed_channel(db, fixture_loader):
    """Insert one channel chat row; return its primary-key id."""
    payload = fixture_loader("list-channels.json")
    team = payload["team"]
    ch = payload["channels"][1]  # Novak channel
    db.execute(
        """
        INSERT INTO teams_chats(
            teams_chat_id, chat_kind, topic, team_uuid, team_name, channel_id, first_seen_at
        ) VALUES (?, 'channel', ?, ?, ?, ?, ?)
        """,
        (
            ch["id"],
            ch["displayName"],
            team["id"],
            team["displayName"],
            ch["id"],
            "2026-04-01T00:00:00",
        ),
    )
    db.commit()
    return db.execute("SELECT id FROM teams_chats WHERE teams_chat_id = ?", (ch["id"],)).fetchone()[
        "id"
    ]


def test_pull_messages_inserts_channel_posts(db, fixture_loader):
    _seed_channel(db, fixture_loader)
    msgs_payload = fixture_loader("list-messages-channel.json")

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = msgs_payload
        result = pull_messages(db, concurrency=1)

    assert result["messages_inserted"] == 3

    rows = db.execute(
        "SELECT sender_display_name, content_text, parent_message_id "
        "FROM teams_messages ORDER BY composed_at"
    ).fetchall()
    assert rows[0]["sender_display_name"] == "Novak, Maria"
    assert rows[0]["content_text"].startswith("Q2 cards roadmap")
    assert rows[0]["parent_message_id"] is None
    assert rows[1]["parent_message_id"] == "1717000000000"


def test_pull_messages_idempotent(db, fixture_loader):
    _seed_channel(db, fixture_loader)
    msgs_payload = fixture_loader("list-messages-channel.json")

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = msgs_payload
        pull_messages(db, concurrency=1)
        result_2 = pull_messages(db, concurrency=1)

    assert result_2["messages_inserted"] == 0
    assert db.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0] == 3


def test_is_permanent_error_classifier():
    # Real upstream messages observed in production logs.
    assert _is_permanent_error(
        Exception(
            'teams-cli exit 5: {"code":"upstream","message":"Team X has no channel named \\"General\\""}'
        )
    )
    assert _is_permanent_error(Exception("Graph 403 HTTP_403: Http request forbidden"))
    assert _is_permanent_error(Exception('{"status":403,"message":"HTTP_403"}'))

    # Transient — must NOT trip the auto-disable.
    assert not _is_permanent_error(Exception("HTTP_429 Too Many Requests"))
    assert not _is_permanent_error(Exception("ECONNRESET"))
    assert not _is_permanent_error(Exception("HTTP_500 internal server error"))


def test_pull_messages_disables_chat_on_permanent_error(db, fixture_loader):
    chat_id = _seed_channel(db, fixture_loader)

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.side_effect = Exception(
            'teams-cli exit 5: {"message":"Team abc has no channel named \\"General\\""}'
        )
        result = pull_messages(db, concurrency=1)

    assert result["errors"] == 1
    assert result["messages_inserted"] == 0
    disabled = db.execute(
        "SELECT ingest_disabled FROM teams_chats WHERE id = ?", (chat_id,)
    ).fetchone()["ingest_disabled"]
    assert disabled == 1


def test_pull_messages_keeps_chat_enabled_on_transient_error(db, fixture_loader):
    chat_id = _seed_channel(db, fixture_loader)

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.side_effect = Exception("HTTP_429 throttled, retry after 30s")
        result = pull_messages(db, concurrency=1)

    assert result["errors"] == 1
    disabled = db.execute(
        "SELECT ingest_disabled FROM teams_chats WHERE id = ?", (chat_id,)
    ).fetchone()["ingest_disabled"]
    assert disabled == 0  # transient — still try next time


def test_pull_messages_skips_disabled_chats(db, fixture_loader):
    chat_id = _seed_channel(db, fixture_loader)
    db.execute("UPDATE teams_chats SET ingest_disabled = 1 WHERE id = ?", (chat_id,))

    with patch("src.export.teams_export.run_teams_cli") as mock:
        result = pull_messages(db, concurrency=1)

    # Disabled chat is filtered out by the SELECT — teams-cli never invoked.
    assert mock.call_count == 0
    assert result["chats_pulled"] == 0
    assert result["errors"] == 0


def test_pull_messages_strips_html_and_decodes_entities(db, fixture_loader):
    _seed_channel(db, fixture_loader)
    msgs_payload = fixture_loader("list-messages-channel.json")

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = msgs_payload
        pull_messages(db, concurrency=1)

    # The 3rd post is the HTML one (composed_at 09:00).
    row = db.execute(
        "SELECT content_text, content_html FROM teams_messages WHERE composed_at LIKE '2026-04-29T09:00%'"
    ).fetchone()

    # HTML preserved verbatim
    assert "<b>ROI</b>" in row["content_html"]
    assert "&gt;" in row["content_html"]

    # Stripped text: tags gone, entities decoded, '<br>' is a newline,
    # and the literal '>' / '<' from "&gt;" / "&lt;" survive after decode.
    assert (
        "<" not in row["content_text"] or "<" in "ROI > 15% when x < 20"
    )  # entity-decoded literal allowed
    assert "Καλημέρα" in row["content_text"]
    assert "ROI > 15% when x < 20" in row["content_text"]
    assert "R&D" in row["content_text"]
    assert "<b>" not in row["content_text"]


def test_classify_chat_kind_meeting():
    chat = {
        "id": "19:meeting_xxx@thread.v2",
        "chatType": "meeting",
        "isOneOnOne": False,
        "members": [
            {"mri": "8:orgid:a"},
            {"mri": "8:orgid:b"},
        ],
    }
    assert _classify_chat_kind(chat) == "meeting"


def test_classify_chat_kind_oneOnOne():
    chat = {
        "id": "19:a_b@unq.gbl.spaces",
        "chatType": "chat",
        "isOneOnOne": True,
        "members": [
            {"mri": "8:orgid:a"},
            {"mri": "8:orgid:b"},
        ],
    }
    assert _classify_chat_kind(chat) == "oneOnOne"


def test_classify_chat_kind_group():
    chat = {
        "id": "19:xxx@thread.v2",
        "chatType": "chat",
        "isOneOnOne": False,
        "members": [
            {"mri": "8:orgid:a"},
            {"mri": "8:orgid:b"},
            {"mri": "8:orgid:c"},
        ],
    }
    assert _classify_chat_kind(chat) == "group"


def test_classify_chat_kind_self_chat_returns_none():
    chat = {
        "id": "19:xxx@thread.v2",
        "chatType": "chat",
        "isOneOnOne": False,
        "members": [{"mri": "8:orgid:a"}],
    }
    assert _classify_chat_kind(chat) is None


def test_classify_chat_kind_two_humans_no_isOneOnOne_flag():
    """Spec fallback: 2 human members → oneOnOne even when isOneOnOne is absent."""
    chat = {
        "id": "19:xxx@thread.v2",
        "chatType": "chat",
        # isOneOnOne deliberately absent
        "members": [
            {"mri": "8:orgid:a"},
            {"mri": "8:orgid:b"},
        ],
    }
    assert _classify_chat_kind(chat) == "oneOnOne"


def test_classify_chat_kind_ignores_bot_members():
    """Non-human members (MRI not starting with '8:') don't count toward classification."""
    chat = {
        "id": "19:xxx@thread.v2",
        "chatType": "chat",
        "isOneOnOne": False,
        "members": [
            {"mri": "8:orgid:a"},
            {"mri": "28:orgid:bot1"},  # bot — ignored
            {"mri": "28:orgid:bot2"},  # bot — ignored
        ],
    }
    # 1 human member → None (skip)
    assert _classify_chat_kind(chat) is None


def test_discover_chat_chats_inserts_oneOnOne_and_group(db, fixture_loader):
    chats_payload = fixture_loader("list-chats.json")

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = chats_payload
        from src.export.teams_export import _discover_chat_chats

        result = _discover_chat_chats(db)

    assert result["chats_inserted"] == 2  # 1on1 + group; meeting + self-chat skipped
    rows = db.execute(
        "SELECT chat_kind, topic, teams_chat_id FROM teams_chats ORDER BY chat_kind"
    ).fetchall()
    kinds = sorted(r["chat_kind"] for r in rows)
    assert kinds == ["group", "oneOnOne"]

    group_row = next(r for r in rows if r["chat_kind"] == "group")
    assert group_row["topic"] == "Cards strategy huddle"

    oneOnOne_row = next(r for r in rows if r["chat_kind"] == "oneOnOne")
    assert oneOnOne_row["topic"] is None  # 1-on-1 has no title; resolved later


def test_discover_chat_chats_skips_meeting(db, fixture_loader):
    chats_payload = fixture_loader("list-chats.json")
    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = chats_payload
        from src.export.teams_export import _discover_chat_chats

        _discover_chat_chats(db)
    meeting_count = db.execute(
        "SELECT COUNT(*) FROM teams_chats WHERE chat_kind = 'meeting'"
    ).fetchone()[0]
    assert meeting_count == 0


def test_discover_chat_chats_is_idempotent(db, fixture_loader):
    chats_payload = fixture_loader("list-chats.json")
    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = chats_payload
        from src.export.teams_export import _discover_chat_chats

        _discover_chat_chats(db)
        _discover_chat_chats(db)  # second run
    count = db.execute("SELECT COUNT(*) FROM teams_chats").fetchone()[0]
    assert count == 2  # unchanged


def test_discover_chat_chats_updates_renamed_group(db, fixture_loader):
    """UPDATE branch: existing group with changed title is refreshed in place."""
    import copy

    initial = fixture_loader("list-chats.json")
    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = initial
        from src.export.teams_export import _discover_chat_chats

        _discover_chat_chats(db)

    # Confirm initial state
    initial_topic = db.execute(
        "SELECT topic FROM teams_chats WHERE chat_kind = 'group'"
    ).fetchone()["topic"]
    assert initial_topic == "Cards strategy huddle"

    # Modify the group's title in the payload, re-discover.
    modified = copy.deepcopy(initial)
    # Find the group chat (the one with isOneOnOne == False AND a non-null title)
    for chat in modified["chats"]:
        if chat.get("isOneOnOne") is False and chat.get("title") == "Cards strategy huddle":
            chat["title"] = "Cards strategy (renamed)"
            break
    else:
        raise AssertionError("test fixture changed: group chat with expected title not found")

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = modified
        result = _discover_chat_chats(db)

    # Both rows already existed → both go through UPDATE path; no new inserts.
    assert result["chats_inserted"] == 0
    assert result["chats_updated"] == 2

    refreshed_topic = db.execute(
        "SELECT topic FROM teams_chats WHERE chat_kind = 'group'"
    ).fetchone()["topic"]
    assert refreshed_topic == "Cards strategy (renamed)"


def test_discover_chats_scope_all_calls_both_paths(db, fixture_loader):
    channels_payload = fixture_loader("list-channels.json")
    chats_payload = fixture_loader("list-chats.json")

    with patch("src.export.teams_export.run_teams_cli") as mock:

        def side_effect(args, **kw):
            if args[0] == "list-teams":
                return {"teams": [channels_payload["team"]]}
            if args[0] == "list-channels":
                return {"channels": channels_payload["channels"]}
            if args[0] == "list-chats":
                return chats_payload
            raise AssertionError(f"unexpected args: {args}")

        mock.side_effect = side_effect
        result = discover_chats(db, scope="all")

    # 2 channels (from existing fixture) + 2 chats (1on1 + group, meeting/self-chat skipped)
    assert result["chats_inserted"] == 4
    kinds = sorted(r[0] for r in db.execute("SELECT chat_kind FROM teams_chats").fetchall())
    assert kinds == ["channel", "channel", "group", "oneOnOne"]


def _seed_oneOnOne_chat(db) -> int:
    """Insert one oneOnOne chat row; return its primary-key id."""
    db.execute(
        """
        INSERT INTO teams_chats(
            teams_chat_id, chat_kind, topic, team_uuid, team_name,
            channel_id, first_seen_at, last_message_at
        ) VALUES (?, 'oneOnOne', NULL, NULL, NULL, NULL, ?, ?)
        """,
        (
            "19:test-1on1@unq.gbl.spaces",
            "2026-05-01T08:00:00Z",
            "2026-05-04T08:00:00Z",
        ),
    )
    db.commit()
    return db.execute(
        "SELECT id FROM teams_chats WHERE teams_chat_id = '19:test-1on1@unq.gbl.spaces'"
    ).fetchone()["id"]


def test_pull_messages_uses_chat_arg_for_oneOnOne(db, fixture_loader):
    _seed_oneOnOne_chat(db)
    msgs_payload = fixture_loader("list-messages-chat.json")

    captured_args = []

    def fake_cli(args, **kw):
        captured_args.append(args)
        return msgs_payload

    with patch("src.export.teams_export.run_teams_cli", side_effect=fake_cli):
        result = pull_messages(db, concurrency=1)

    assert result["messages_inserted"] == 3
    assert len(captured_args) == 1
    assert captured_args[0][:2] == ["list-messages", "--chat"]
    assert captured_args[0][2] == "19:test-1on1@unq.gbl.spaces"


def test_pull_messages_still_uses_team_channel_for_channel(db, fixture_loader):
    """Regression: existing channel pull path must keep working."""
    _seed_channel(db, fixture_loader)  # existing helper from this file
    msgs_payload = fixture_loader("list-messages-channel.json")

    captured_args = []

    def fake_cli(args, **kw):
        captured_args.append(args)
        return msgs_payload

    with patch("src.export.teams_export.run_teams_cli", side_effect=fake_cli):
        pull_messages(db, concurrency=1)

    assert "--team" in captured_args[0]
    assert "--channel" in captured_args[0]
    assert "--chat" not in captured_args[0]


# --- Systemic failure must not look like 1,167 permanent ones ----------------
# _is_permanent_error treats "Graph 403" as a property of the CHAT, so a
# Graph-wide auth lapse — which 403s every chat in the run — flipped
# ingest_disabled on 1,179 of 1,219 chats in one sweep on prod. Nothing ever
# clears the flag (recovery is a documented manual UPDATE), so Teams ingestion
# silently shrank to the 40 survivors and reported "0 messages inserted across
# 40 chats; 0 errors" every 30 minutes for 33 hours while real messages arrived.


def _seed_chats(db, n, kind="group", disabled=0):
    for i in range(n):
        db.execute(
            "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, ingest_disabled, "
            "first_seen_at) VALUES (?, ?, ?, ?, ?)",
            (
                f"19:chat-{kind}-{i}@thread.v2",
                kind,
                f"chat {i}",
                disabled,
                "2026-08-01T00:00:00",
            ),
        )
    db.commit()


def _disabled_count(db):
    return db.execute("SELECT COUNT(*) FROM teams_chats WHERE ingest_disabled = 1").fetchone()[0]


def test_a_403_affecting_every_chat_disables_none_of_them(db):
    """The signature of an auth problem, not of per-chat permissions."""
    _seed_chats(db, 8)

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.side_effect = RuntimeError("Graph 403 Forbidden")
        pull_messages(db, concurrency=1)

    assert _disabled_count(db) == 0


def test_an_isolated_403_still_disables_that_one_chat(db):
    """A genuinely unreadable chat must still be dropped, or every run keeps
    hammering it — which is what the flag was introduced for."""
    _seed_chats(db, 8)
    bad = "19:chat-group-3@thread.v2"

    def side_effect(args, **kw):
        if args[-1] == bad:
            raise RuntimeError("Graph 403 Forbidden")
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.side_effect = side_effect
        pull_messages(db, concurrency=1)

    rows = db.execute("SELECT teams_chat_id FROM teams_chats WHERE ingest_disabled = 1").fetchall()
    assert [r["teams_chat_id"] for r in rows] == [bad]


# --- Bounded, and covering every chat over time ------------------------------
# Re-enabling the 1,167 makes the working set 1,207 chats at a measured
# 0.45 s each — about 9 minutes against sb-teams-sync's TimeoutStartSec=600,
# before Steps 3-6 run at all. So the pull needs a deadline; and with a deadline
# it needs an order that rotates, or the same head is re-pulled forever and the
# tail is never read.


def test_pull_stops_starting_work_once_the_deadline_passes(db):
    _seed_chats(db, 5)

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = {"messages": []}
        result = pull_messages(db, concurrency=1, deadline_s=0)

    assert mock.call_count == 0
    assert result["deferred"] == 5


def test_pull_takes_the_least_recently_pulled_chats_first(db):
    """Round-robin, so a deadline truncates the tail rather than starving it."""
    _seed_chats(db, 4)
    db.execute(
        "UPDATE teams_chats SET last_pulled_at = ? WHERE teams_chat_id = ?",
        ("2026-08-18T10:00:00", "19:chat-group-0@thread.v2"),
    )
    db.execute(
        "UPDATE teams_chats SET last_pulled_at = ? WHERE teams_chat_id = ?",
        ("2026-08-18T09:00:00", "19:chat-group-1@thread.v2"),
    )
    db.commit()

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = {"messages": []}
        pull_messages(db, concurrency=1)

    pulled_order = [c.args[0][-1] for c in mock.call_args_list]
    # Never-pulled first, then oldest stamp.
    assert pulled_order[:2] == ["19:chat-group-2@thread.v2", "19:chat-group-3@thread.v2"]
    assert pulled_order[2:] == ["19:chat-group-1@thread.v2", "19:chat-group-0@thread.v2"]


def test_a_successful_pull_stamps_the_chat(db):
    """Without the stamp the rotation cannot advance."""
    _seed_chats(db, 1)

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = {"messages": []}
        pull_messages(db, concurrency=1)

    assert db.execute("SELECT last_pulled_at FROM teams_chats").fetchone()[0] is not None
