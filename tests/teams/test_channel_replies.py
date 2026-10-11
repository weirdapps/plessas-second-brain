"""Channel replies are stored, and later edits and deletes reach the store.

chatsvcagg /posts returns each channel post with its replies inside it
(`replies.messages`, `replies.totalCount`), and `_persist_messages` kept only the
post: 3,135 replies, 62% of channel content, sat in raw_json where no search,
thread or extraction could see them. Every message was also insert-or-ignore, so
an edit or a delete that arrived on a later poll was dropped and the store kept
the first draft of an edited message and the text of a deleted one.

The payloads below are synthetic. Their key names and value types mirror what
the store holds (a post wraps its message in an envelope, timestamps are
`composeTime`, ids and `version` are millisecond epochs, `version` an int on
channels and a string on chats, `edittime` and `deletetime` ints in
`properties`); no name or text in them is real.
"""

import json
import sys
from unittest.mock import patch

import pytest

from src.export.teams_export import _persist_messages, backfill_channel_replies
from src.extract.teams_threads import bound_threads

CHANNEL = "19:example-channel@thread.tacv2"
POST_ID = "1760000000000"
POST_AT = "2026-10-01T08:00:00.0000000Z"
REPLY_1 = ("1760000060000", "2026-10-01T08:01:00.0000000Z")
REPLY_2 = ("1760000120000", "2026-10-01T08:02:00.0000000Z")


def _contact(n: int) -> str:
    return f"https://teams.example.test/v1/users/ME/contacts/8:orgid:0000000{n}-0000-0000-0000-000000000000"


def _message(mid, at, text, *, version=None, edittime=None, deletetime=None, parent=None):
    """One message as chatsvcagg returns it, inside a post or as a reply."""
    properties = {}
    if edittime is not None:
        properties["edittime"] = edittime
    if deletetime is not None:
        properties["deletetime"] = deletetime
    return {
        "messageType": "Text",
        "content": text,
        "clientMessageId": "1",
        "imDisplayName": "Example, Person",
        "properties": properties,
        "id": mid,
        "type": "Message",
        "composeTime": at,
        "originalArrivalTime": at,
        "containerId": CHANNEL,
        "parentMessageId": parent or mid,
        "from": _contact(1),
        "sequenceId": 1,
        "version": int(mid) if version is None else version,
        "threadType": "topic",
        "isEscalationToNewPerson": False,
    }


def _reply(rid, at, text, **kw):
    return _message(rid, at, text, parent=POST_ID, **kw)


def _post(replies=(), text="Draft plan for the quarter, comments welcome below.", **kw):
    """A channel post envelope; latestMessageTime is the newest reply's time."""
    replies = list(replies)
    return {
        "containerId": CHANNEL,
        "id": POST_ID,
        "latestMessageTime": replies[-1]["composeTime"] if replies else POST_AT,
        "message": _message(POST_ID, POST_AT, text, **kw),
        "replies": {"messages": replies, "totalCount": len(replies)},
    }


def _two_replies():
    return [
        _reply(*REPLY_1, "Agreed on the first part, the second needs numbers."),
        _reply(*REPLY_2, "I will add the numbers by Friday and share again."),
    ]


@pytest.fixture
def channel(db):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, team_uuid, team_name,"
        " channel_id, first_seen_at) VALUES (?, 'channel', 'Plans', 'team-1', 'Team',"
        " ?, '2026-09-01T00:00:00')",
        (CHANNEL, CHANNEL),
    )
    db.commit()
    return db.execute("SELECT id FROM teams_chats").fetchone()["id"]


def _rows(db):
    return db.execute(
        "SELECT teams_message_id, chat_id, parent_message_id, composed_at, content_text,"
        " content_html, raw_json, thread_id FROM teams_messages ORDER BY composed_at"
    ).fetchall()


def _thread(db):
    return db.execute("SELECT * FROM teams_threads").fetchone()


def _extracted(db):
    """Stand in for teams_pipeline having extracted every thread."""
    db.execute("UPDATE teams_threads SET extraction_status = 'extracted'")
    db.commit()


# --- Replies -----------------------------------------------------------------


def test_a_posts_replies_are_stored_as_rows_under_the_post(db, channel):
    assert _persist_messages(db, channel, {"posts": [_post(_two_replies())]}) == 3

    rows = {r["teams_message_id"]: r for r in _rows(db)}
    assert set(rows) == {
        f"{channel}::{POST_ID}",
        f"{channel}::{REPLY_1[0]}",
        f"{channel}::{REPLY_2[0]}",
    }
    for reply_id, at in (REPLY_1, REPLY_2):
        row = rows[f"{channel}::{reply_id}"]
        assert row["chat_id"] == channel
        assert row["parent_message_id"] == POST_ID
        assert row["composed_at"] == at
    assert rows[f"{channel}::{POST_ID}"]["parent_message_id"] is None


def test_a_post_is_dated_by_its_own_compose_time_not_its_last_reply(db, channel):
    """latestMessageTime moves with every reply; dating the post by it put the
    question after its own answers in the thread the model reads."""
    _persist_messages(db, channel, {"posts": [_post(_two_replies())]})

    post = db.execute(
        "SELECT composed_at FROM teams_messages WHERE teams_message_id = ?",
        (f"{channel}::{POST_ID}",),
    ).fetchone()
    assert post["composed_at"] == POST_AT


def test_reading_the_same_page_again_adds_nothing(db, channel):
    payload = {"posts": [_post(_two_replies())]}
    _persist_messages(db, channel, payload)

    assert _persist_messages(db, channel, payload) == 0
    assert db.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0] == 3


def test_replies_join_the_posts_thread_after_the_post(db, channel):
    _persist_messages(db, channel, {"posts": [_post(_two_replies())]})

    bound_threads(db)

    thread = _thread(db)
    assert db.execute("SELECT COUNT(*) FROM teams_threads").fetchone()[0] == 1
    assert thread["anchor_message_id"] == POST_ID
    assert thread["message_count"] == 3
    assert (thread["started_at"], thread["ended_at"]) == (POST_AT, REPLY_2[1])
    assert {r["thread_id"] for r in _rows(db)} == {thread["id"]}


def test_a_new_reply_sends_an_extracted_thread_back_to_extraction(db, channel):
    first, second = _two_replies()
    _persist_messages(db, channel, {"posts": [_post([first])]})
    bound_threads(db)
    _extracted(db)

    assert _persist_messages(db, channel, {"posts": [_post([first, second])]}) == 1
    bound_threads(db)

    thread = _thread(db)
    assert thread["extraction_status"] == "pending"
    assert thread["message_count"] == 3


def test_a_reply_is_redacted_like_a_post(db, channel):
    secret = "AIzaSy" + "A1b2C3d4E5" * 3 + "fghij"  # shape fixture, not a key
    reply = _reply(*REPLY_1, f"the key is {secret}")

    _persist_messages(db, channel, {"posts": [_post([reply])]})

    row = db.execute(
        "SELECT content_text, content_html, raw_json FROM teams_messages WHERE parent_message_id = ?",
        (POST_ID,),
    ).fetchone()
    for column in ("content_text", "content_html", "raw_json"):
        assert secret not in row[column], column
        assert "[REDACTED:google-key]" in row[column], column


# --- Later versions ----------------------------------------------------------


def _chat(db):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, first_seen_at)"
        " VALUES ('19:example-dm@unq.gbl.spaces', 'oneOnOne', '2026-09-01T00:00:00')"
    )
    db.commit()
    return db.execute("SELECT id FROM teams_chats WHERE chat_kind = 'oneOnOne'").fetchone()["id"]


def _chat_message(text, version, **properties):
    """A chat message as chatsvc returns it: flat, lower-case keys, string version."""
    return {
        "id": "1760000300000",
        "messagetype": "Text",
        "contenttype": "text",
        "content": text,
        "imdisplayname": "Example, Person",
        "from": _contact(2),
        "composetime": "2026-10-01T09:00:00.0000000Z",
        "originalarrivaltime": "2026-10-01T09:00:00.0000000Z",
        "version": version,
        "properties": properties,
    }


ORIGINAL = "The budget review moves to Thursday afternoon this week."
EDITED = "The budget review moves to Friday morning this week, same room."


def test_an_edited_chat_message_replaces_the_stored_text_and_queues_its_thread(db):
    chat = _chat(db)
    _persist_messages(db, chat, {"messages": [_chat_message(ORIGINAL, "1760000300000")]})
    bound_threads(db)
    _extracted(db)

    edited = _chat_message(EDITED, "1760000400000", edittime=1760000400000)
    assert _persist_messages(db, chat, {"messages": [edited]}) == 0

    (row,) = _rows(db)
    assert row["content_text"] == EDITED
    assert EDITED in row["raw_json"] and ORIGINAL not in row["raw_json"]
    assert _thread(db)["extraction_status"] == "pending"


def test_an_edited_reply_is_applied(db, channel):
    first, second = _two_replies()
    _persist_messages(db, channel, {"posts": [_post([first, second])]})
    bound_threads(db)
    _extracted(db)

    edited = _reply(REPLY_1[0], REPLY_1[1], EDITED, version=1760000500000, edittime=1760000500000)
    _persist_messages(db, channel, {"posts": [_post([edited, second])]})

    row = db.execute(
        "SELECT content_text FROM teams_messages WHERE teams_message_id = ?",
        (f"{channel}::{REPLY_1[0]}",),
    ).fetchone()
    assert row["content_text"] == EDITED
    assert _thread(db)["extraction_status"] == "pending"


def test_a_deleted_message_is_blanked_in_every_stored_copy_and_its_thread_queued(db, channel):
    first, second = _two_replies()
    _persist_messages(db, channel, {"posts": [_post([first, second])]})
    bound_threads(db)
    _extracted(db)
    old_text = first["content"]

    # The service empties a deleted message and stamps it; the guard below must
    # blank it even if a payload still carried the text.
    deleted = _reply(
        REPLY_1[0], REPLY_1[1], old_text, version=1760000600000, deletetime=1760000600000
    )
    _persist_messages(db, channel, {"posts": [_post([deleted, second])]})

    row = db.execute(
        "SELECT content_text, content_html, raw_json FROM teams_messages"
        " WHERE teams_message_id = ?",
        (f"{channel}::{REPLY_1[0]}",),
    ).fetchone()
    assert row["content_text"] == "" and row["content_html"] == ""
    assert old_text not in row["raw_json"]
    hits = db.execute(
        "SELECT COUNT(*) FROM teams_messages_fts WHERE teams_messages_fts MATCH 'numbers'"
    ).fetchone()[0]
    assert hits == 1, "only the surviving reply still matches its words"
    assert _thread(db)["extraction_status"] == "pending"


def test_a_deleted_post_keeps_its_replies(db, channel):
    _persist_messages(db, channel, {"posts": [_post(_two_replies())]})

    deleted = _post(_two_replies(), text="", version=1760000700000, deletetime=1760000700000)
    _persist_messages(db, channel, {"posts": [deleted]})

    texts = {r["teams_message_id"]: r["content_text"] for r in _rows(db)}
    assert texts[f"{channel}::{POST_ID}"] == ""
    assert texts[f"{channel}::{REPLY_1[0]}"] and texts[f"{channel}::{REPLY_2[0]}"]


@pytest.mark.parametrize("version", ["1760000300000", "1760000200000"])
def test_a_same_or_older_version_changes_nothing(db, version):
    chat = _chat(db)
    _persist_messages(db, chat, {"messages": [_chat_message(ORIGINAL, "1760000300000")]})
    bound_threads(db)
    _extracted(db)

    _persist_messages(db, chat, {"messages": [_chat_message(EDITED, version)]})

    (row,) = _rows(db)
    assert row["content_text"] == ORIGINAL
    assert _thread(db)["extraction_status"] == "extracted"


def test_a_newer_version_with_the_same_text_does_not_queue_the_thread(db):
    """A reaction bumps the version but says nothing new: no model call for it."""
    chat = _chat(db)
    _persist_messages(db, chat, {"messages": [_chat_message(ORIGINAL, "1760000300000")]})
    bound_threads(db)
    _extracted(db)

    reacted = _chat_message(ORIGINAL, "1760000400000", emotions=[{"key": "like"}])
    _persist_messages(db, chat, {"messages": [reacted]})

    (row,) = _rows(db)
    assert "emotions" in row["raw_json"], "the stored payload is the newest one"
    assert _thread(db)["extraction_status"] == "extracted"


# --- Backfill from stored payloads -------------------------------------------


def _store_post_as_before(db, chat_id, item):
    """A post row as the code before this change wrote it: dated by the newest
    reply, replies left inside raw_json."""
    db.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, sender_display_name,"
        " composed_at, message_type, content_text, content_html, parent_message_id,"
        " is_system, raw_json) VALUES (?, ?, 'Example, Person', ?, 'Text', ?, ?, NULL, 0, ?)",
        (
            f"{chat_id}::{item['id']}",
            chat_id,
            item["latestMessageTime"],
            item["message"]["content"],
            item["message"]["content"],
            json.dumps(item),
        ),
    )
    db.commit()


def _no_teams_call(*args, **kwargs):
    raise AssertionError("the backfill must not call Teams")


def test_the_backfill_stores_the_replies_held_in_stored_posts(db, channel):
    _store_post_as_before(db, channel, _post(_two_replies()))
    bound_threads(db)
    _extracted(db)

    with patch("src.export.teams_export.run_teams_cli", side_effect=_no_teams_call):
        counts = backfill_channel_replies(db)
    bound_threads(db)

    assert counts["posts_read"] == 1
    assert counts["replies_found"] == 2
    assert counts["replies_stored"] == 2
    assert counts["posts_redated"] == 1
    parents = [r["parent_message_id"] for r in _rows(db)]
    assert parents == [None, POST_ID, POST_ID]
    thread = _thread(db)
    assert thread["extraction_status"] == "pending"
    assert (thread["message_count"], thread["started_at"]) == (3, POST_AT)


def test_the_backfill_is_idempotent(db, channel):
    _store_post_as_before(db, channel, _post(_two_replies()))
    backfill_channel_replies(db)

    counts = backfill_channel_replies(db)

    assert (counts["replies_found"], counts["replies_stored"], counts["posts_redated"]) == (
        2,
        0,
        0,
    )
    assert db.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0] == 3


def test_the_backfill_redacts_what_it_stores(db, channel):
    """A payload stored before a pattern existed is redacted on the way into its row."""
    secret = "ghp_" + "a1B2c3D4e5" * 4  # shape fixture, not a token
    _store_post_as_before(db, channel, _post([_reply(*REPLY_1, f"token {secret} for the job")]))

    backfill_channel_replies(db)

    row = db.execute(
        "SELECT content_text, content_html, raw_json FROM teams_messages"
        " WHERE parent_message_id = ?",
        (POST_ID,),
    ).fetchone()
    for column in ("content_text", "content_html", "raw_json"):
        assert secret not in row[column], column
        assert "[REDACTED:github-token]" in row[column], column


def test_the_backfill_reads_only_channel_posts(db, channel):
    _store_post_as_before(db, _chat(db), _post(_two_replies()))

    counts = backfill_channel_replies(db)

    assert counts["posts_read"] == 0
    assert db.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0] == 1


# --- The command ---------------------------------------------------------------


def _run_cli(monkeypatch, db_path, *argv):
    from src import cli

    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db_path), *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code
    return 0


def _db_path(db):
    return db.execute("PRAGMA database_list").fetchone()["file"]


def test_the_command_backfills_and_queues_the_threads(db, channel, monkeypatch, capsys):
    _store_post_as_before(db, channel, _post(_two_replies()))
    bound_threads(db)
    _extracted(db)
    monkeypatch.setattr("src.export.teams_export.run_teams_cli", _no_teams_call)

    assert _run_cli(monkeypatch, _db_path(db), "teams-backfill-replies") == 0

    out = capsys.readouterr().out
    assert "2 stored" in out
    assert db.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0] == 3
    assert _thread(db)["extraction_status"] == "pending"


def test_the_command_refuses_a_replica(db, channel, monkeypatch, capsys):
    _store_post_as_before(db, channel, _post(_two_replies()))
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    assert _run_cli(monkeypatch, _db_path(db), "teams-backfill-replies") == 2

    assert db.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0] == 1
