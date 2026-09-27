"""The chat-list upsert keeps the newest last_message_at it has seen.

teams-access declares lastMessage.composeTime in camelCase, but discovery read
only the lowercase composetime, so it got NULL for every chat and wrote that NULL
over the value pull_messages had just set (audit db-integrity-5: 769 of 1,227
chats showed no last activity). The fixture in list-chats.json uses the lowercase
key, which is how the mismatch hid.
"""

import copy
from unittest.mock import patch

from src.export.teams_export import _discover_chat_chats

ONE_ON_ONE = (
    "19:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa_bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb@unq.gbl.spaces"
)


def _discover(db, payload):
    with patch("src.export.teams_export.run_teams_cli", return_value=payload):
        _discover_chat_chats(db)


def _last_message_at(db, chat_id=ONE_ON_ONE):
    return db.execute(
        "SELECT last_message_at FROM teams_chats WHERE teams_chat_id = ?", (chat_id,)
    ).fetchone()[0]


def test_the_camel_case_compose_time_is_read(db, fixture_loader):
    _discover(db, fixture_loader("list-chats-camelcase.json"))

    assert _last_message_at(db) == "2026-09-25T12:34:07.123Z"


def test_a_chat_listed_without_a_last_message_keeps_its_value(db, fixture_loader):
    payload = fixture_loader("list-chats-camelcase.json")
    _discover(db, payload)

    bare = copy.deepcopy(payload)
    for chat in bare["chats"]:
        chat.pop("lastMessage")
    _discover(db, bare)

    assert _last_message_at(db) == "2026-09-25T12:34:07.123Z"


def test_an_older_value_does_not_replace_a_newer_one(db, fixture_loader):
    payload = fixture_loader("list-chats-camelcase.json")
    _discover(db, payload)
    # pull_messages has since stored a message newer than the listing knew of.
    db.execute(
        "UPDATE teams_chats SET last_message_at = ? WHERE teams_chat_id = ?",
        ("2026-09-26T08:00:00.000Z", ONE_ON_ONE),
    )

    _discover(db, payload)

    assert _last_message_at(db) == "2026-09-26T08:00:00.000Z"


def test_a_newer_value_replaces_an_older_one(db, fixture_loader):
    payload = fixture_loader("list-chats-camelcase.json")
    _discover(db, payload)

    newer = copy.deepcopy(payload)
    newer["chats"][0]["lastMessage"]["composeTime"] = "2026-09-27T07:00:00.000Z"
    _discover(db, newer)

    assert _last_message_at(db) == "2026-09-27T07:00:00.000Z"
