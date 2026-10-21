"""WhatsApp: a changed time is applied, and a contact WhatsApp moved to an anonymous id
is one chat (audit 2026-10-20).

- A message whose bridge time changed kept the old one: the update compared text,
  sender and media, never the time.
- WhatsApp moved contacts to anonymous ids (LIDs), and the bridge began a second
  direct chat under the id, so one contact's conversation sat in two chats: months
  under the number, then everything after under the id. The snapshot now carries the
  id-number pairs, and a direct chat under a number whose person also has a chat
  under the id is kept as that one: its messages and sessions move there, and every
  such contact heals on the next sync.

Every number here is invented, from the ranges conftest uses.
"""

from src.export.whatsapp_export import ingest_snapshot
from src.extract.whatsapp_threads import bound_threads
from tests.whatsapp.conftest import ALICE, DIRECT_JID, OWNER, build_snapshot

ALICE_LID = "100000000000011"
LID_JID = f"{ALICE_LID}@lid"
CONTACTS = [(f"{ALICE}@s.whatsapp.net", "Alice Example", None, None)]


def _msg(mid, chat, sender, content, ts, from_me=0):
    return (mid, chat, sender, content, ts, from_me, "", "")


def _before_and_after():
    return [
        _msg("a1", DIRECT_JID, ALICE, "Before the move", "2026-09-01 10:00:00+03:00"),
        _msg("a2", DIRECT_JID, OWNER, "Still before", "2026-09-01 10:05:00+03:00", 1),
        _msg("b1", LID_JID, ALICE_LID, "After the move", "2026-10-01 10:00:00+03:00"),
        _msg("b2", LID_JID, OWNER, "Still after", "2026-10-01 10:05:00+03:00", 1),
    ]


CHATS = [(DIRECT_JID, "Alice Example"), (LID_JID, "Alice Example")]


def _chats(db):
    return [r[0] for r in db.execute("SELECT chat_jid FROM whatsapp_chats ORDER BY id")]


def test_a_changed_bridge_time_is_applied(db, tmp_path):
    first = [_msg("m1", DIRECT_JID, ALICE, "Sail on Saturday?", "2026-09-01 10:00:00+03:00")]
    ingest_snapshot(db, build_snapshot(tmp_path / "a", first, chats=[(DIRECT_JID, "Alice")]))
    bound_threads(db)
    db.execute("UPDATE whatsapp_threads SET extraction_status = 'extracted'")
    db.commit()

    moved = [_msg("m1", DIRECT_JID, ALICE, "Sail on Saturday?", "2026-09-01 11:30:00+03:00")]
    out = ingest_snapshot(db, build_snapshot(tmp_path / "b", moved, chats=[(DIRECT_JID, "Alice")]))

    assert out["messages_updated"] == 1
    assert db.execute("SELECT sent_at FROM whatsapp_messages").fetchone()[0] == (
        "2026-09-01T08:30:00Z"
    )
    thread = db.execute(
        "SELECT started_at, ended_at, extraction_status FROM whatsapp_threads"
    ).fetchone()
    assert tuple(thread) == ("2026-09-01T08:30:00Z", "2026-09-01T08:30:00Z", "pending")
    again = ingest_snapshot(
        db, build_snapshot(tmp_path / "c", moved, chats=[(DIRECT_JID, "Alice")])
    )
    assert again["messages_updated"] == 0


def test_a_contact_moved_to_an_anonymous_id_is_one_chat(db, tmp_path):
    snapshot = build_snapshot(
        tmp_path, _before_and_after(), CHATS, CONTACTS, lid_map=[(ALICE_LID, ALICE)]
    )

    out = ingest_snapshot(db, snapshot)

    assert _chats(db) == [LID_JID]
    assert out["chats_merged"] == 0  # nothing stored was split: it never was
    chat_ids = {r[0] for r in db.execute("SELECT chat_id FROM whatsapp_messages")}
    assert len(chat_ids) == 1
    # Each message keeps the JID the bridge filed it under, so the next sync matches it.
    assert ingest_snapshot(db, snapshot)["messages_inserted"] == 0


def test_a_contact_already_split_in_the_store_is_joined_with_its_sessions(db, tmp_path):
    # Stored before the snapshot carried the pairs: two chats, a session in each.
    ingest_snapshot(db, build_snapshot(tmp_path / "a", _before_and_after(), CHATS))
    bound_threads(db)
    assert _chats(db) == [DIRECT_JID, LID_JID]
    sessions = db.execute("SELECT COUNT(*) FROM whatsapp_threads").fetchone()[0]

    snapshot = build_snapshot(
        tmp_path / "b", _before_and_after(), CHATS, CONTACTS, lid_map=[(ALICE_LID, ALICE)]
    )
    out = ingest_snapshot(db, snapshot)

    assert out["chats_merged"] == 1
    assert _chats(db) == [LID_JID]
    lid_chat = db.execute("SELECT id FROM whatsapp_chats").fetchone()[0]
    assert {r[0] for r in db.execute("SELECT chat_id FROM whatsapp_messages")} == {lid_chat}
    assert {r[0] for r in db.execute("SELECT chat_id FROM whatsapp_threads")} == {lid_chat}
    assert db.execute("SELECT COUNT(*) FROM whatsapp_threads").fetchone()[0] == sessions
    last = db.execute("SELECT last_message_at FROM whatsapp_chats").fetchone()[0]
    assert last == "2026-10-01T07:05:00Z"

    again = ingest_snapshot(db, snapshot)
    assert again["chats_merged"] == 0
    assert again["messages_inserted"] == again["messages_updated"] == 0


def test_a_number_whose_id_has_no_chat_keeps_its_chat(db, tmp_path):
    only_before = _before_and_after()[:2]
    snapshot = build_snapshot(
        tmp_path, only_before, [(DIRECT_JID, "Alice Example")], CONTACTS, [(ALICE_LID, ALICE)]
    )

    ingest_snapshot(db, snapshot)

    assert _chats(db) == [DIRECT_JID]
