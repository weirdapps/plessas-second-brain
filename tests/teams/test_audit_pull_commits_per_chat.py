"""Each chat's pull is committed before the next chat is fetched.

pull_messages wrote every chat (its messages and its last_pulled_at) into one
transaction and committed once, after the loop. The loop runs for the whole
TEAMS_PULL_DEADLINE_S, so every teams-sync held brain.db's write lock across
about 150 s of teams-cli calls, even a run that inserted nothing. On 2026-09-27
the calendar backfill and the calendar catch-up run each waited out their 60 s
busy_timeout inside the 20:32 run's pull and exited 1 with "database is locked".
"""

import sqlite3
from unittest.mock import patch

import pytest

from src.export.teams_cli import TeamsCliAuthRequired
from src.export.teams_export import pull_messages


def _seed(db, name):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at) "
        "VALUES (?, 'group', ?, '2026-08-01T00:00:00')",
        (name, name),
    )


def _another_writer_is_blocked(path):
    other = sqlite3.connect(path, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return True
    else:
        other.rollback()
        return False
    finally:
        other.close()


def test_no_write_lock_is_held_while_the_next_chat_is_fetched(db):
    for name in ("chat-a", "chat-b", "chat-c"):
        _seed(db, name)
    db.commit()
    path = db.execute("PRAGMA database_list").fetchone()["file"]
    fetched = []
    blocked = []

    def fetch(args):
        # The first fetch has no earlier chat that could hold the lock.
        if fetched:
            blocked.append(_another_writer_is_blocked(path))
        fetched.append(args[-1])
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli", side_effect=fetch):
        result = pull_messages(db, concurrency=1)

    assert result["chats_pulled"] == 3
    assert blocked == [False, False]


def test_the_chats_pulled_before_an_auth_failure_are_kept(db):
    for name in ("chat-a", "chat-b"):
        _seed(db, name)
    db.commit()
    fetched = []

    def fetch(args):
        if fetched:
            raise TeamsCliAuthRequired("session expired")
        fetched.append(args[-1])
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli", side_effect=fetch):
        with pytest.raises(TeamsCliAuthRequired):
            pull_messages(db, concurrency=1)
    # The exception leaves the run without a commit; closing it discards the rest.
    db.rollback()

    pulled = db.execute(
        "SELECT teams_chat_id FROM teams_chats WHERE last_pulled_at IS NOT NULL"
    ).fetchall()
    assert [r["teams_chat_id"] for r in pulled] == fetched
