"""brain whatsapp-sync, step 1: the snapshot into whatsapp_chats and whatsapp_messages."""

import pytest

from src.export.whatsapp_export import SnapshotUnavailable, ingest_snapshot
from tests.whatsapp.conftest import ALICE, BOB, DIRECT_JID, GROUP_JID, OWNER, build_snapshot


def _msg(mid, chat, sender, content, ts, from_me=0, media="", filename=""):
    return (mid, chat, sender, content, ts, from_me, media, filename)


def _basic():
    return [
        _msg("m1", DIRECT_JID, ALICE, "Shall we sail on Saturday?", "2026-09-01 10:00:00+03:00"),
        _msg("m2", DIRECT_JID, OWNER, "Yes, weather permitting", "2026-09-01 10:02:00+03:00", 1),
        _msg(
            "m3", GROUP_JID, f"{BOB}@s.whatsapp.net", "Dinner at eight", "2026-09-02 19:00:00+03:00"
        ),
        _msg("m4", GROUP_JID, ALICE, "", "2026-09-02 19:01:00+03:00", 0, "image", "a.jpg"),
    ]


def _count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_every_message_and_chat_lands(db, tmp_path):
    out = ingest_snapshot(db, build_snapshot(tmp_path, _basic()))
    assert out["messages_inserted"] == 4
    assert _count(db, "whatsapp_messages") == 4
    assert _count(db, "whatsapp_chats") == 2


def test_dedupe_holds_across_two_syncs(db, tmp_path):
    snapshot = build_snapshot(tmp_path, _basic())
    ingest_snapshot(db, snapshot)
    second = ingest_snapshot(db, snapshot)
    assert second["messages_inserted"] == 0
    assert second["messages_updated"] == 0
    assert _count(db, "whatsapp_messages") == 4


def test_times_are_stored_as_utc(db, tmp_path):
    ingest_snapshot(db, build_snapshot(tmp_path, _basic()))
    sent = db.execute("SELECT sent_at FROM whatsapp_messages WHERE message_id='m1'").fetchone()[0]
    assert sent == "2026-09-01T07:00:00Z"


def test_the_bridge_time_formats_all_parse(db, tmp_path):
    rows = [
        _msg("a", DIRECT_JID, ALICE, "x", "2026-09-01 10:00:00.123456789+03:00"),
        _msg("b", DIRECT_JID, ALICE, "x", "2026-09-01T07:00:01Z"),
        _msg("c", DIRECT_JID, ALICE, "x", "2026-09-01 10:00:02+0300"),
        _msg("d", DIRECT_JID, ALICE, "x", "not a time"),
    ]
    out = ingest_snapshot(db, build_snapshot(tmp_path, rows))
    assert out["messages_inserted"] == 3
    assert out["skipped"] == 1


def test_senders_are_named(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_USER_NAME", "Owner Example")
    ingest_snapshot(db, build_snapshot(tmp_path, _basic()))
    names = dict(db.execute("SELECT message_id, sender_name FROM whatsapp_messages").fetchall())
    assert names["m1"] == "Alice Example"  # the direct chat's name
    assert names["m2"] == "Owner Example"  # from me
    assert names["m3"] == BOB  # a group member with no chat of their own
    assert names["m4"] == "Alice Example"  # a group member we also chat with directly


def test_chat_kinds(db, tmp_path):
    ingest_snapshot(db, build_snapshot(tmp_path, _basic()))
    kinds = dict(db.execute("SELECT chat_jid, chat_kind FROM whatsapp_chats").fetchall())
    assert kinds == {DIRECT_JID: "direct", GROUP_JID: "group"}


def test_status_updates_are_not_conversations(db, tmp_path):
    rows = _basic() + [
        _msg("s1", "status@broadcast", ALICE, "my holiday", "2026-09-03 09:00:00+03:00")
    ]
    chats = [(DIRECT_JID, "Alice Example"), (GROUP_JID, "Chat A"), ("status@broadcast", "")]
    ingest_snapshot(db, build_snapshot(tmp_path, rows, chats))
    assert _count(db, "whatsapp_messages") == 4


def test_credentials_are_redacted_on_the_way_in(db, tmp_path):
    key = "AIza" + "B" * 35
    rows = [_msg("k", DIRECT_JID, ALICE, f"the key is {key}", "2026-09-01 10:00:00+03:00")]
    ingest_snapshot(db, build_snapshot(tmp_path, rows))
    content = db.execute("SELECT content FROM whatsapp_messages").fetchone()[0]
    assert key not in content


def test_an_edited_message_updates_and_its_thread_is_re_extracted(db, tmp_path):
    from src.extract.whatsapp_threads import bound_threads

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ingest_snapshot(db, build_snapshot(tmp_path / "a", _basic()))
    bound_threads(db)
    db.execute("UPDATE whatsapp_threads SET extraction_status = 'extracted'")
    db.commit()
    edited = [list(r) for r in _basic()]
    edited[0][3] = "Shall we sail on Sunday instead?"
    out = ingest_snapshot(db, build_snapshot(tmp_path / "b", [tuple(r) for r in edited]))
    assert out["messages_updated"] == 1
    status = db.execute(
        "SELECT t.extraction_status FROM whatsapp_threads t JOIN whatsapp_messages m "
        "ON m.thread_id = t.id WHERE m.message_id = 'm1'"
    ).fetchone()[0]
    assert status == "pending"


def test_a_missing_snapshot_is_reported_not_treated_as_empty(db, tmp_path):
    with pytest.raises(SnapshotUnavailable):
        ingest_snapshot(db, tmp_path / "absent.db")


def test_the_last_message_time_is_kept_per_chat(db, tmp_path):
    ingest_snapshot(db, build_snapshot(tmp_path, _basic()))
    last = db.execute(
        "SELECT last_message_at FROM whatsapp_chats WHERE chat_jid = ?", (GROUP_JID,)
    ).fetchone()[0]
    assert last == "2026-09-02T16:01:00Z"
