"""The disable breaker judges channels and chats separately.

Channels read through chatsvcagg and chats through chatsvc, on different token
audiences, so a lapse can 403 one kind and not the other. Channels are about 3%
of a run, so measured against the whole run a channel-wide 403 stayed under the
25% ceiling and disabled every channel for good (audit teams-calendar-4).
"""

from unittest.mock import patch

from src.export.teams_export import pull_messages

_FORBIDDEN = (
    'teams-cli exit 5: {"code":"upstream","message":"Graph 403 HTTP_403: '
    'Http request forbidden","status":403}'
)


def _seed(db, n, kind):
    for i in range(n):
        db.execute(
            "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, team_uuid, channel_id, "
            "first_seen_at) VALUES (?, ?, 'x', ?, ?, '2026-08-01T00:00:00')",
            (
                f"19:{kind}-{i}@thread.v2",
                kind,
                "team-uuid" if kind == "channel" else None,
                f"19:{kind}-{i}@thread.v2" if kind == "channel" else None,
            ),
        )
    db.commit()


def _disabled(db):
    return [
        r[0]
        for r in db.execute(
            "SELECT teams_chat_id FROM teams_chats WHERE ingest_disabled = 1 ORDER BY id"
        )
    ]


def test_every_channel_failing_among_healthy_chats_disables_none(db):
    _seed(db, 12, "channel")
    _seed(db, 390, "group")

    def side_effect(args, **kw):
        if "--channel" in args:
            raise RuntimeError(_FORBIDDEN)
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli", side_effect=side_effect):
        result = pull_messages(db, concurrency=1)

    assert result["errors"] == 12
    assert _disabled(db) == []


def test_an_isolated_chat_403_is_still_disabled_while_channels_fail(db):
    _seed(db, 12, "channel")
    _seed(db, 390, "group")
    bad = "19:group-7@thread.v2"

    def side_effect(args, **kw):
        if "--channel" in args or args[-1] == bad:
            raise RuntimeError(_FORBIDDEN)
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli", side_effect=side_effect):
        pull_messages(db, concurrency=1)

    assert _disabled(db) == [bad]


def test_an_isolated_channel_403_is_still_disabled(db):
    _seed(db, 12, "channel")
    _seed(db, 390, "group")
    bad = "19:channel-4@thread.v2"

    def side_effect(args, **kw):
        if args[-1] == bad:
            raise RuntimeError(_FORBIDDEN)
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli", side_effect=side_effect):
        pull_messages(db, concurrency=1)

    assert _disabled(db) == [bad]


def test_a_team_without_a_general_channel_is_disabled_in_one_run(db):
    # teams-access raises this once per channel, for every channel of the team,
    # and only after Graph listed the team's channels, so it cannot come from a
    # token lapse. Judged against a small channel pool it tripped the breaker on
    # every run and the team was retried for ever.
    for i in range(9):
        db.execute(
            "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, team_uuid, channel_id, "
            "first_seen_at) VALUES (?, 'channel', 'x', 'renamed-team', ?, "
            "'2026-08-01T00:00:00')",
            (f"19:renamed-{i}@thread.v2", f"19:renamed-{i}@thread.v2"),
        )
    db.commit()
    _seed(db, 3, "channel")
    _seed(db, 300, "group")

    def side_effect(args, **kw):
        if "renamed-team" in args:
            raise RuntimeError(
                'Team renamed-team has no channel named "General", cannot derive chatsvcagg teamId.'
            )
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli", side_effect=side_effect):
        result = pull_messages(db, concurrency=1)

    assert result["errors"] == 9
    assert _disabled(db) == [f"19:renamed-{i}@thread.v2" for i in range(9)]
