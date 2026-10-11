"""A message's text is the text a reader sees, decided by the content's shape.

Ingest converted a message's HTML only when its payload declared contentType
"html", which the chat service never sends: chats declare "text", "Text" or
nothing and channel posts "Text", whatever the body holds. So the stored text of
every HTML message was the HTML itself, and search indexed its tags, attribute
names and entities as words while the model read them. The text is now converted
whenever the content holds markup, and `teams-strip-html` converts the rows stored
before, from the HTML each row keeps.

The payloads are synthetic. Their key names and value types mirror the services
(a chat message is flat with lower-case keys; a channel post wraps its message in
an envelope with camelCase keys and holds its replies); no name or text in them is
real.
"""

import json
import re
import sys
from unittest.mock import patch

import pytest

from src.export.teams_export import (
    _persist_messages,
    backfill_channel_replies,
    pull_messages,
    strip_stored_html,
)

CHANNEL = "19:example-channel@thread.tacv2"
DM = "19:example-dm@unq.gbl.spaces"
SENDER = (
    "https://teams.example.test/v1/users/ME/contacts/8:orgid:00000001-0000-0000-0000-000000000000"
)

# A body as the services write one: a mention, a link to a file, entities, a
# block, a line break, an older-style mention and an emoji drawn as an image.
HTML = (
    '<p>Morning <span itemscope="" itemtype="http://schema.skype.com/Mention" itemid="0">'
    "Example Person</span>, the figures are in "
    '<a href="https://files.example.test/q3-figures.xlsx">q3-figures.xlsx</a></p>'
    "<div>ROI &gt; 15% when x &lt; 20 &amp; R&amp;D&nbsp;agrees<br>next line</div>"
    '<p><at id="1">Other Person</at> <img alt="&#x1F642;" '
    'itemtype="http://schema.skype.com/Emoji" src="https://example.test/e.png"></p>'
)
# Markup someone typed: the services escape it, so it is text once converted.
TYPED_MARKUP = "<p>close it with &lt;/div&gt; and &amp;amp; stays</p>"
TYPED_MARKUP_TEXT = "close it with </div> and &amp; stays"
CALL_XML = (
    "<partlist type='ended' alt=''><part identity='8:orgid:x'><name>8:orgid:x</name>"
    "<displayName>Example Person</displayName><duration>61.5</duration></part></partlist>"
)

_TAG = re.compile(r"</?[A-Za-z]")


def _assert_converted(text: str) -> None:
    """What a reader sees of HTML: no markup, entities decoded, a block and what
    follows a <br> on lines of their own, the visible text of a mention, a link
    and an emoji kept."""
    assert not _TAG.search(text), "no tag is left"
    for entity in ("&gt;", "&lt;", "&amp;", "&nbsp;", "&#x1F642;"):
        assert entity not in text, entity
    lines = text.split("\n")
    assert "ROI > 15% when x < 20 & R&D agrees" in lines
    assert "next line" in lines
    for visible in ("Example Person", "Other Person", "q3-figures.xlsx", "\U0001f642"):
        assert visible in text, visible


# --- Payloads ----------------------------------------------------------------


def _chat(db) -> int:
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, first_seen_at)"
        " VALUES (?, 'oneOnOne', '2026-09-01T00:00:00')",
        (DM,),
    )
    db.commit()
    return db.execute("SELECT id FROM teams_chats WHERE teams_chat_id = ?", (DM,)).fetchone()[0]


def _channel(db) -> int:
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, team_uuid, team_name,"
        " channel_id, first_seen_at) VALUES (?, 'channel', 'Plans', 'team-1', 'Team',"
        " ?, '2026-09-01T00:00:00')",
        (CHANNEL, CHANNEL),
    )
    db.commit()
    row = db.execute("SELECT id FROM teams_chats WHERE teams_chat_id = ?", (CHANNEL,)).fetchone()
    return row[0]


def _chat_message(mid, content, *, contenttype="text", messagetype="RichText/Html"):
    """A chat message as chatsvc returns it; `contenttype` None leaves the key out."""
    msg = {
        "id": mid,
        "messagetype": messagetype,
        "content": content,
        "imdisplayname": "Example, Person",
        "from": SENDER,
        "composetime": "2026-10-01T09:00:00.0000000Z",
        "version": mid,
        "properties": {},
    }
    if contenttype is not None:
        msg["contenttype"] = contenttype
    return msg


def _channel_message(mid, content, *, parent=None, content_type="Text"):
    """A channel message as chatsvcagg returns it, in a post or as a reply."""
    msg = {
        "messageType": "RichText/Html",
        "content": content,
        "imDisplayName": "Example, Person",
        "properties": {},
        "id": mid,
        "composeTime": "2026-10-01T08:00:00.0000000Z",
        "parentMessageId": parent or mid,
        "from": SENDER,
        "version": int(mid),
    }
    if content_type is not None:
        msg["contentType"] = content_type
    return msg


def _post(mid, content, replies=()):
    replies = list(replies)
    return {
        "containerId": CHANNEL,
        "id": mid,
        "latestMessageTime": "2026-10-01T08:00:00.0000000Z",
        "message": _channel_message(mid, content),
        "replies": {"messages": replies, "totalCount": len(replies)},
    }


def _stored(db, teams_message_id):
    return db.execute(
        "SELECT content_text, content_html FROM teams_messages WHERE teams_message_id = ?",
        (teams_message_id,),
    ).fetchone()


# --- Ingest: the export path -------------------------------------------------


def test_a_chat_pull_converts_html_the_service_declares_as_text(db):
    chat = _chat(db)
    payload = {"messages": [_chat_message("1760000300000", HTML, contenttype="text")]}

    with patch("src.export.teams_export.run_teams_cli", return_value=payload):
        pull_messages(db, concurrency=1)

    row = _stored(db, f"{chat}::1760000300000")
    _assert_converted(row["content_text"])
    assert row["content_html"] == HTML, "the HTML is kept as it came"


@pytest.mark.parametrize("contenttype", ["text", "Text", None, "html", "announcement"])
def test_a_message_is_converted_whatever_type_it_declares(db, contenttype):
    chat = _chat(db)

    _persist_messages(
        db, chat, {"messages": [_chat_message("1760000300000", HTML, contenttype=contenttype)]}
    )

    _assert_converted(_stored(db, f"{chat}::1760000300000")["content_text"])


def test_a_channel_pull_converts_a_post_and_its_replies(db):
    channel = _channel(db)
    reply = _channel_message("1760000060000", HTML, parent="1760000000000", content_type=None)
    payload = {"posts": [_post("1760000000000", HTML, [reply])]}

    with patch("src.export.teams_export.run_teams_cli", return_value=payload):
        pull_messages(db, concurrency=1)

    for upstream_id in ("1760000000000", "1760000060000"):
        row = _stored(db, f"{channel}::{upstream_id}")
        _assert_converted(row["content_text"])
        assert row["content_html"] == HTML


def test_the_replies_backfill_converts_the_replies_it_stores(db):
    """Replies stored from a post's kept payload take the same path as a pull."""
    channel = _channel(db)
    reply = _channel_message("1760000060000", HTML, parent="1760000000000", content_type="text")
    post = _post("1760000000000", "Plain words for the post, nothing marked up.", [reply])
    db.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, composed_at, message_type,"
        " content_text, content_html, parent_message_id, is_system, raw_json)"
        " VALUES (?, ?, ?, 'RichText/Html', ?, ?, NULL, 0, ?)",
        (
            f"{channel}::1760000000000",
            channel,
            post["latestMessageTime"],
            post["message"]["content"],
            post["message"]["content"],
            json.dumps(post),
        ),
    )
    db.commit()

    backfill_channel_replies(db)

    _assert_converted(_stored(db, f"{channel}::1760000060000")["content_text"])


@pytest.mark.parametrize(
    "plain",
    [
        "a < b && c > d",
        "if x<y and y>z then swap them",
        "R&D and AT&T; Q&A after lunch",
        "<3 see you at 5",
        "x &lt y, typed without the semicolon",
    ],
)
def test_plain_text_holding_a_literal_lt_or_amp_is_stored_as_it_is(db, plain):
    chat = _chat(db)

    _persist_messages(
        db, chat, {"messages": [_chat_message("1760000300000", plain, messagetype="Text")]}
    )

    assert _stored(db, f"{chat}::1760000300000")["content_text"] == plain


def test_a_system_message_keeps_the_services_xml(db):
    """Call records and membership changes are records, not text anyone wrote;
    their fields would run together as text, and system messages are kept out of
    threads, extraction and vectors already."""
    chat = _chat(db)

    _persist_messages(
        db,
        chat,
        {"messages": [_chat_message("1760000300000", CALL_XML, messagetype="Event/Call")]},
    )

    assert _stored(db, f"{chat}::1760000300000")["content_text"] == CALL_XML


# --- The backfill of stored rows --------------------------------------------


def _store_as_before(db, chat_id, mid, content, *, text=None, kind="RichText/Html", system=0):
    """A row as ingest stored it before: the text was the HTML. `text` stores
    something else as the text, such as what this change already converted."""
    db.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, sender_display_name,"
        " composed_at, message_type, content_text, content_html, is_system, raw_json)"
        " VALUES (?, ?, 'Example, Person', '2026-10-01T09:00:00Z', ?, ?, ?, ?, '{}')",
        (f"{chat_id}::{mid}", chat_id, kind, content if text is None else text, content, system),
    )
    db.commit()
    return f"{chat_id}::{mid}"


def _fts_hits(db, term: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM teams_messages_fts WHERE teams_messages_fts MATCH ?", (term,)
    ).fetchone()[0]


def test_stored_html_is_converted_from_the_kept_html_and_the_index_follows(db):
    chat = _chat(db)
    marked = _store_as_before(db, chat, "1", HTML)
    plain = _store_as_before(db, chat, "2", "a < b && c > d", kind="Text")
    call = _store_as_before(db, chat, "3", CALL_XML, kind="Event/Call", system=1)
    assert _fts_hits(db, "itemtype") == 1, "the index holds the attribute names"

    counts = strip_stored_html(db)

    assert (counts["scanned"], counts["changed"], counts["unchanged"]) == (2, 1, 1)
    _assert_converted(_stored(db, marked)["content_text"])
    assert _stored(db, marked)["content_html"] == HTML
    assert _stored(db, plain)["content_text"] == "a < b && c > d"
    assert _stored(db, call)["content_text"] == CALL_XML
    assert _fts_hits(db, "itemtype") == 0
    assert _fts_hits(db, "nbsp") == 0
    assert _fts_hits(db, "agrees") == 1


def test_a_second_run_changes_nothing_even_where_the_text_shows_markup(db):
    """It converts the kept HTML, never the text it wrote: converting that text
    again would strip the markup someone typed and decode their entities."""
    chat = _chat(db)
    marked = _store_as_before(db, chat, "1", HTML)
    typed = _store_as_before(db, chat, "2", TYPED_MARKUP)
    assert strip_stored_html(db)["changed"] == 2
    converted = _stored(db, marked)["content_text"]

    counts = strip_stored_html(db)

    assert (counts["changed"], counts["unchanged"]) == (0, 2)
    assert _stored(db, typed)["content_text"] == TYPED_MARKUP_TEXT
    assert _stored(db, marked)["content_text"] == converted


def test_a_dry_run_counts_and_writes_nothing(db):
    chat = _chat(db)
    marked = _store_as_before(db, chat, "1", HTML)

    counts = strip_stored_html(db, dry_run=True)

    assert counts["changed"] == 1
    assert _stored(db, marked)["content_text"] == HTML
    assert not db.in_transaction


def test_the_limit_caps_the_rows_changed_and_a_rerun_takes_the_rest(db):
    """A row already converted is scanned but not counted against the limit, so
    a run always gets further than the one before."""
    chat = _chat(db)
    _store_as_before(db, chat, "0", TYPED_MARKUP, text=TYPED_MARKUP_TEXT)
    rows = [_store_as_before(db, chat, str(n), HTML) for n in (1, 2, 3)]

    first = strip_stored_html(db, limit=2)
    second = strip_stored_html(db, limit=2)

    assert (first["changed"], first["stopped_at_limit"]) == (2, True)
    assert (second["changed"], second["stopped_at_limit"]) == (1, False)
    for row in rows:
        _assert_converted(_stored(db, row)["content_text"])


def test_nothing_goes_back_to_extraction_or_embedding(db):
    chat = _chat(db)
    db.execute(
        "INSERT INTO teams_threads (id, chat_id, thread_kind, anchor_message_id, started_at,"
        " ended_at, message_count, extraction_status, extracted_at, embedding_at, summary)"
        " VALUES (7, ?, 'chat_session', '1', '2026-10-01T09:00:00Z', '2026-10-01T09:00:00Z',"
        " 1, 'extracted', '2026-10-02T00:00:00', '2026-10-02T00:00:01', 'A summary.')",
        (chat,),
    )
    marked = _store_as_before(db, chat, "1", HTML)
    db.execute("UPDATE teams_messages SET thread_id = 7 WHERE teams_message_id = ?", (marked,))
    db.commit()

    strip_stored_html(db)

    thread = db.execute(
        "SELECT extraction_status, extracted_at, embedding_at FROM teams_threads WHERE id = 7"
    ).fetchone()
    assert tuple(thread) == ("extracted", "2026-10-02T00:00:00", "2026-10-02T00:00:01")


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


def test_the_command_converts_and_prints_counts_never_text(db, monkeypatch, capsys):
    chat = _chat(db)
    marked = _store_as_before(db, chat, "1", HTML)

    assert _run_cli(monkeypatch, _db_path(db), "teams-strip-html") == 0

    out = capsys.readouterr().out
    assert re.search(r"changed\s*:\s*1\b", out)
    for word in ("agrees", "Example Person", "q3-figures"):
        assert word not in out, "the output holds counts, never message text"
    _assert_converted(_stored(db, marked)["content_text"])


def test_the_command_stops_at_its_limit_and_says_so(db, monkeypatch, capsys):
    chat = _chat(db)
    rows = [_store_as_before(db, chat, str(n), HTML) for n in (1, 2)]

    assert _run_cli(monkeypatch, _db_path(db), "teams-strip-html", "--limit", "1") == 0

    assert "again" in capsys.readouterr().out
    assert [_stored(db, r)["content_text"] == HTML for r in rows] == [False, True]


def test_the_dry_run_command_writes_nothing(db, monkeypatch, capsys):
    chat = _chat(db)
    marked = _store_as_before(db, chat, "1", HTML)

    assert _run_cli(monkeypatch, _db_path(db), "teams-strip-html", "--dry-run") == 0

    assert re.search(r"would change\s*:\s*1\b", capsys.readouterr().out)
    assert _stored(db, marked)["content_text"] == HTML


@pytest.mark.parametrize("flags", [[], ["--dry-run"]])
def test_the_command_refuses_a_replica(db, monkeypatch, capsys, flags):
    chat = _chat(db)
    marked = _store_as_before(db, chat, "1", HTML)
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    assert _run_cli(monkeypatch, _db_path(db), "teams-strip-html", *flags) == 2

    assert "BRAIN_ROLE=replica" in capsys.readouterr().err
    assert _stored(db, marked)["content_text"] == HTML
