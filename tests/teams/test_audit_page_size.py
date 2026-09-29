"""A chat-scope pull asks for more than teams-cli's default page of 50.

teams-cli list-messages reads one page and nothing more, so a chat that got more
messages than the page holds between two polls kept only the newest page and
lost the rest for good (audit teams-calendar-5: 228 missing sequence ids in the
busiest chat). A larger page is the stopgap until the CLI can page backwards.
"""

from unittest.mock import patch

from src.export.teams_export import CHAT_PAGE_SIZE, pull_messages


def _seed(db, kind):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, team_uuid, channel_id, "
        "first_seen_at) VALUES (?, ?, 'x', ?, ?, '2026-08-01T00:00:00')",
        (
            f"19:{kind}@thread.v2",
            kind,
            "team-uuid" if kind == "channel" else None,
            f"19:{kind}@thread.v2" if kind == "channel" else None,
        ),
    )
    db.commit()


def _captured_args(db):
    captured = []

    def fake_cli(args, **kw):
        captured.append(args)
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli", side_effect=fake_cli):
        result = pull_messages(db, concurrency=1)
    return captured, result


def test_a_chat_read_asks_for_a_page_of_200(db):
    _seed(db, "group")

    captured, result = _captured_args(db)

    assert CHAT_PAGE_SIZE == 200
    args = captured[0]
    assert args[args.index("--page-size") + 1] == "200"
    assert args[args.index("--chat") + 1] == "19:group@thread.v2"
    # The CLI reads these keys, so the stopgap must not rename them.
    assert set(result) == {"chats_pulled", "messages_inserted", "errors", "deferred"}


def test_a_channel_read_is_left_as_it_was(db):
    _seed(db, "channel")

    captured, _ = _captured_args(db)

    assert "--page-size" not in captured[0]


def test_a_meeting_chat_read_asks_for_a_page_of_200(db):
    _seed(db, "meeting")

    captured, _ = _captured_args(db)

    args = captured[0]
    assert args[args.index("--page-size") + 1] == "200"
    assert args[args.index("--chat") + 1] == "19:meeting@thread.v2"
