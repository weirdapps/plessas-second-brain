"""Chats with activity since their last pull are pulled first.

The deadline reaches about a third of the inventory per run, so a strict
oldest-pulled-first rotation made a busy chat wait for hundreds of silent ones,
and the longer it waited the more it overflowed the single page teams-cli reads
(audit teams-calendar-5). The rotation still orders each group.
"""

from unittest.mock import patch

from src.export.teams_export import pull_messages


def _seed(db, name, last_pulled_at, last_message_at):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at, "
        "last_pulled_at, last_message_at) VALUES (?, 'group', ?, '2026-08-01T00:00:00', ?, ?)",
        (name, name, last_pulled_at, last_message_at),
    )


def test_chats_with_new_activity_go_before_the_rotation(db):
    # Quiet since its pull at 09:00.
    _seed(db, "quiet-a", "2026-09-27T09:00:00.000000+00:00", "2026-09-27T08:00:00.000Z")
    # A message at 11:00, after its pull at 10:00.
    _seed(db, "busy-b", "2026-09-27T10:00:00.000000+00:00", "2026-09-27T11:00:00.000Z")
    # Quiet, and pulled longest ago.
    _seed(db, "quiet-c", "2026-09-27T08:00:00.000000+00:00", "2026-09-27T07:00:00.000Z")
    # Never pulled.
    _seed(db, "new-d", None, None)
    # Pulled, activity unknown: nothing says it has news, so it waits its turn.
    _seed(db, "unknown-e", "2026-09-27T07:30:00.000000+00:00", None)
    db.commit()

    with patch("src.export.teams_export.run_teams_cli") as mock:
        mock.return_value = {"messages": []}
        pull_messages(db, concurrency=1)

    pulled_order = [c.args[0][-1] for c in mock.call_args_list]
    assert pulled_order == ["new-d", "busy-b", "unknown-e", "quiet-c", "quiet-a"]
