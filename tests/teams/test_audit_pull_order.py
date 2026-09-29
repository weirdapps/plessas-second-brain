"""Channels and chats with activity since their last pull are pulled first.

The deadline reaches about a third of the inventory per run, so a strict
oldest-pulled-first rotation made a busy chat wait for hundreds of silent ones,
and the longer it waited the more it overflowed the single page teams-cli reads
(audit teams-calendar-5). Channels join that first group because discovery never
learns their activity. Chats never pulled come next: ahead of the rotation, but
behind live conversations, so a backfill cannot hold those back (2026-09-29
spec E4). The rotation still orders each group.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from src.export.teams_export import pull_messages

NOW = datetime.now(UTC)


def _pulled(hours_ago: float) -> str:
    """A last_pulled_at stamp in Python's isoformat, as pull_messages writes it."""
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _composed(hours_ago: float) -> str:
    """A last_message_at in Teams' "...Z" format, as discovery stores it."""
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _seed(db, name, last_pulled_at, last_message_at):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at, "
        "last_pulled_at, last_message_at) VALUES (?, 'group', ?, '2026-08-01T00:00:00', ?, ?)",
        (name, name, last_pulled_at, last_message_at),
    )


def _seed_channel(db, name, last_pulled_at):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, team_uuid, channel_id, "
        "first_seen_at, last_pulled_at) VALUES (?, 'channel', ?, 'team-uuid', ?, "
        "'2026-08-01T00:00:00', ?)",
        (f"19:{name}@thread.tacv2", name, name, last_pulled_at),
    )


def test_live_chats_and_channels_go_first_then_unpulled_then_the_rotation(db):
    # Quiet since its pull 2.5 hours ago.
    _seed(db, "quiet-a", _pulled(2.5), _composed(3.5))
    # A message 30 minutes ago, after its pull 90 minutes ago.
    _seed(db, "busy-b", _pulled(1.5), _composed(0.5))
    # Quiet, and pulled 3.5 hours ago.
    _seed(db, "quiet-c", _pulled(3.5), _composed(4.5))
    # Never pulled, activity unknown.
    _seed(db, "new-d", None, None)
    # Pulled longest ago, activity unknown: nothing says it has news.
    _seed(db, "unknown-e", _pulled(4), None)
    # A channel pulled an hour ago: discovery never learns channel activity.
    _seed_channel(db, "chan-f", _pulled(1))
    # Never pulled: a conversation that started 12 minutes ago, and a backfill
    # chat from 2020. The new one must not queue behind the backfill.
    _seed(db, "fresh-g", None, _composed(0.2))
    _seed(db, "old-h", None, "2020-05-01T09:00:00.000Z")
    db.commit()

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = {"messages": []}
        pull_messages(db, concurrency=1)

    pulled_order = [c.args[0][-1] for c in mock.call_args_list]
    assert pulled_order == [
        "busy-b",
        "chan-f",
        "fresh-g",
        "old-h",
        "new-d",
        "unknown-e",
        "quiet-c",
        "quiet-a",
    ]
