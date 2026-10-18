"""teams-drop-raw-json: drop the raw Teams payloads, keeping what the replies backfill needs.

raw_json held 133 MB of the 244 MB teams_messages table on the producer. Its readers are the
pull's edit detection, which a dropped payload costs one rewrite of the message the next time
a pull reads it, and teams-backfill-replies, which needs a channel post's payload until the
replies inside it are stored as rows. The payloads below are synthetic.
"""

import json
import sqlite3
import sys

import pytest

from src.export.teams_export import _persist_messages, backfill_channel_replies
from src.extract.teams_threads import bound_threads
from src.store import teams_raw_json
from src.store.teams_raw_json import drop_raw_json

CHANNEL = "19:example-channel@thread.tacv2"
CONTACT = (
    "https://teams.example.test/v1/users/ME/contacts/8:orgid:00000001-0000-0000-0000-000000000000"
)


def _chat(db, kind="oneOnOne", teams_id="19:example-dm@unq.gbl.spaces"):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, first_seen_at)"
        " VALUES (?, ?, '2026-09-01T00:00:00')",
        (teams_id, kind),
    )
    db.commit()
    row = db.execute("SELECT id FROM teams_chats WHERE teams_chat_id = ?", (teams_id,)).fetchone()
    return row[0]


def _chat_message(n, text):
    """The n-th message of a chat as chatsvc returns it: flat, lower-case keys, string version."""
    mid = str(1760000300000 + 60000 * n)
    return {
        "id": mid,
        "messagetype": "Text",
        "contenttype": "text",
        "content": text,
        "imdisplayname": "Example, Person",
        "from": CONTACT,
        "composetime": f"2026-10-01T09:{n:02d}:00.0000000Z",
        "version": mid,
        "properties": {},
    }


def _channel_message(n, text, parent):
    """The n-th message of the channel, a post or a reply: camel-case keys, an int version."""
    mid = str(1760000000000 + 60000 * n)
    return {
        "messageType": "Text",
        "content": text,
        "imDisplayName": "Example, Person",
        "properties": {},
        "id": mid,
        "composeTime": f"2026-10-01T08:{n:02d}:00.0000000Z",
        "parentMessageId": parent or mid,
        "from": CONTACT,
        "version": int(mid),
    }


def _post(replies):
    """A channel post envelope holding `replies` replies."""
    post = _channel_message(0, "A draft plan for the quarter, comments welcome.", None)
    held = [
        _channel_message(n, f"Reply {n}, agreed with one change.", post["id"])
        for n in range(1, replies + 1)
    ]
    return {
        "containerId": CHANNEL,
        "id": post["id"],
        "latestMessageTime": held[-1]["composeTime"] if held else post["composeTime"],
        "message": post,
        "replies": {"messages": held, "totalCount": len(held)},
    }


def _store_post_as_before(db, chat_id, item):
    """A post as the code before replies were stored wrote it: replies only inside raw_json."""
    db.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, composed_at, message_type,"
        " content_text, content_html, is_system, raw_json) VALUES (?, ?, ?, 'Text', ?, ?, 0, ?)",
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


def _payloads(db):
    return dict(db.execute("SELECT teams_message_id, raw_json FROM teams_messages").fetchall())


def _texts(db):
    return [
        tuple(r)
        for r in db.execute(
            "SELECT id, content_text, content_html, composed_at, thread_id FROM teams_messages"
            " ORDER BY id"
        )
    ]


@pytest.fixture
def store(db):
    """A one-to-one chat with three messages and a channel with a post and its two replies, all
    stored as the pull stores them now."""
    chat = _chat(db)
    _persist_messages(db, chat, {"messages": [_chat_message(n, f"Message {n}.") for n in range(3)]})
    channel = _chat(db, "channel", CHANNEL)
    _persist_messages(db, channel, {"posts": [_post(2)]})
    bound_threads(db)
    db.commit()
    return db


def test_it_drops_every_payload_nothing_needs_and_only_the_payload(store):
    texts = _texts(store)

    counts = drop_raw_json(store)

    assert counts["dropped"] == 6
    assert counts["bytes"] > 0
    assert (counts["kept_replies"], counts["kept_unreadable"], counts["left"]) == (0, 0, 0)
    assert set(_payloads(store).values()) == {None}
    assert _texts(store) == texts


def test_it_leaves_the_full_text_index_alone(store):
    pages = store.execute("SELECT id, block FROM teams_messages_fts_data ORDER BY id").fetchall()

    drop_raw_json(store)

    after = store.execute("SELECT id, block FROM teams_messages_fts_data ORDER BY id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in pages]
    store.execute(
        "INSERT INTO teams_messages_fts(teams_messages_fts, rank) VALUES('integrity-check', 1)"
    )


def test_a_post_holding_replies_not_stored_keeps_its_payload_until_they_are(db):
    channel = _chat(db, "channel", CHANNEL)
    _store_post_as_before(db, channel, _post(1))

    first = drop_raw_json(db)

    assert (first["dropped"], first["kept_replies"]) == (0, 1)
    assert backfill_channel_replies(db)["replies_stored"] == 1

    second = drop_raw_json(db)

    assert (second["dropped"], second["kept_replies"]) == (2, 0)
    assert set(_payloads(db).values()) == {None}


def _sending_a_file(n):
    """The n-th chat message, sending a SharePoint file: its payload alone names it."""
    message = _chat_message(n, "The plan, attached.")
    entry = {"objectUrl": "https://contoso.sharepoint.com/sites/Team/Shared Documents/Plan.docx"}
    message["properties"] = {"files": json.dumps([entry])}
    return message


def _teams_scan_reached(db, row_id):
    db.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value)"
        " VALUES ('sharepoint_teams_scan_last_id', ?)",
        (str(row_id),),
    )
    db.commit()


def test_a_payload_naming_a_file_the_sharepoint_pass_has_not_read_is_kept_until_it_has(db):
    """process-sharepoint reads a sent file from the payload only (properties.files): the HTML
    kept in content_html does not name it. Dropped before the Teams scan passed the message,
    the file would never be fetched."""
    chat = _chat(db)
    _persist_messages(db, chat, {"messages": [_sending_a_file(0), _chat_message(1, "Thanks.")]})
    db.commit()
    sent, plain = (r[0] for r in db.execute("SELECT id FROM teams_messages ORDER BY id"))

    first = drop_raw_json(db)

    assert (first["dropped"], first["kept_links"]) == (1, 1)
    assert _payloads(db)[f"{chat}::{_sending_a_file(0)['id']}"] is not None

    _teams_scan_reached(db, sent)
    second = drop_raw_json(db)

    assert (second["dropped"], second["kept_links"]) == (1, 0)
    assert set(_payloads(db).values()) == {None}
    assert plain > sent


def test_the_command_says_it_kept_payloads_for_the_sharepoint_pass(db, monkeypatch, capsys):
    chat = _chat(db)
    _persist_messages(db, chat, {"messages": [_sending_a_file(0)]})
    db.commit()

    assert _run_cli(monkeypatch, _db_path(db), "teams-drop-raw-json") == 0

    out = capsys.readouterr().out
    assert "kept 1 payloads naming a SharePoint file" in out
    assert "process-sharepoint" in out


def test_a_post_whose_payload_does_not_parse_is_kept(db):
    channel = _chat(db, "channel", CHANNEL)
    db.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, composed_at, is_system, raw_json)"
        " VALUES (?, ?, '2026-10-01T08:00:00Z', 0, '{not json')",
        (f"{channel}::{_post(0)['id']}", channel),
    )
    db.commit()

    counts = drop_raw_json(db)

    assert (counts["dropped"], counts["kept_unreadable"]) == (0, 1)
    assert list(_payloads(db).values()) == ["{not json"]


def test_a_dry_run_counts_and_changes_nothing(store):
    before = _payloads(store)

    counts = drop_raw_json(store, dry_run=True)

    assert counts["dropped"] == 6
    assert counts["bytes"] == sum(len(v.encode()) for v in before.values())
    assert _payloads(store) == before


def test_the_limit_takes_the_oldest_first_and_says_how_many_are_left(store):
    ids = [r[0] for r in store.execute("SELECT id FROM teams_messages ORDER BY id")]

    counts = drop_raw_json(store, limit=4)

    assert (counts["dropped"], counts["left"]) == (4, 2)
    dropped = [r[0] for r in store.execute("SELECT id FROM teams_messages WHERE raw_json IS NULL")]
    assert sorted(dropped) == ids[:4]


def test_each_batch_is_committed_so_a_stopped_run_keeps_what_it_did(store, monkeypatch):
    monkeypatch.setattr(teams_raw_json, "DROP_BATCH", 2)
    last = store.execute("SELECT MAX(id) FROM teams_messages").fetchone()[0]
    store.execute(
        "CREATE TRIGGER refuse BEFORE UPDATE OF raw_json ON teams_messages"
        f" WHEN old.id = {int(last)} BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    store.commit()

    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        drop_raw_json(store)
    store.rollback()

    left = store.execute("SELECT COUNT(*) FROM teams_messages WHERE raw_json IS NOT NULL")
    assert left.fetchone()[0] == 2  # the last batch, refused; the two before it committed


def test_a_message_read_again_gets_its_payload_back_and_no_extraction(db):
    """A dropped payload reads as version 0 to the pull's edit detection, so the next pull that
    reads the message stores it again, once, with the same text: the thread stays extracted."""
    chat = _chat(db)
    message = _chat_message(0, "The review moves to Thursday.")
    _persist_messages(db, chat, {"messages": [message]})
    bound_threads(db)
    db.execute("UPDATE teams_threads SET extraction_status = 'extracted'")
    db.commit()
    stored = _payloads(db)
    drop_raw_json(db)

    _persist_messages(db, chat, {"messages": [message]})

    assert _payloads(db) == stored
    assert db.execute("SELECT extraction_status FROM teams_threads").fetchone()[0] == "extracted"


# --- The command ------------------------------------------------------------------------------


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


def test_the_command_drops_and_reports(store, monkeypatch, capsys):
    assert _run_cli(monkeypatch, _db_path(store), "teams-drop-raw-json") == 0

    assert "dropped 6 payloads" in capsys.readouterr().out
    assert set(_payloads(store).values()) == {None}


def test_the_command_dry_run_changes_nothing(store, monkeypatch, capsys):
    before = _payloads(store)

    assert _run_cli(monkeypatch, _db_path(store), "teams-drop-raw-json", "--dry-run") == 0

    assert "would drop 6 payloads" in capsys.readouterr().out
    assert _payloads(store) == before


def test_the_command_says_what_it_kept_and_what_is_left(db, monkeypatch, capsys):
    channel = _chat(db, "channel", CHANNEL)
    _store_post_as_before(db, channel, _post(1))
    chat = _chat(db)
    _persist_messages(db, chat, {"messages": [_chat_message(n, f"Message {n}.") for n in range(3)]})
    db.commit()

    assert _run_cli(monkeypatch, _db_path(db), "teams-drop-raw-json", "--limit", "2") == 0

    out = capsys.readouterr().out
    assert "dropped 2 payloads" in out
    assert "teams-backfill-replies" in out
    assert "1 left" in out


def test_the_command_refuses_a_negative_limit(store, monkeypatch):
    before = _payloads(store)

    assert _run_cli(monkeypatch, _db_path(store), "teams-drop-raw-json", "--limit", "-1") == 2

    assert _payloads(store) == before


def test_the_command_refuses_a_replica(store, monkeypatch):
    before = _payloads(store)
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    assert _run_cli(monkeypatch, _db_path(store), "teams-drop-raw-json") == 2

    assert _payloads(store) == before


def test_the_command_on_a_store_without_teams(tmp_path, monkeypatch, capsys):
    path = tmp_path / "plain.db"
    sqlite3.connect(str(path)).close()

    assert _run_cli(monkeypatch, path, "teams-drop-raw-json") == 0

    assert "nothing to drop" in capsys.readouterr().out
