"""WhatsApp: a message the bridge files under both a contact's number and anonymous id
is held once (VPS incident 2026-11-26).

After a bridge restart its history sync re-filed months of two direct chats under the
contacts' numbers, while the same messages were already held under their anonymous
ids (LIDs). The chats were merged (_moved_chats), but a message was matched on the
JID it came under, so each arrived a second time: 679 copies with no session. Their
ids were already session anchors in the merged chat, so bound_threads failed on the
UNIQUE (chat_id, anchor_message_id) every hour, and 225 genuinely new messages
behind them were never bounded.

Every number here is invented, from the ranges conftest uses.
"""

from src.export.whatsapp_export import ingest_snapshot
from src.extract.whatsapp_threads import bound_threads
from tests.whatsapp.conftest import ALICE, DIRECT_JID, OWNER, build_snapshot

ALICE_LID = "100000000000011"
LID_JID = f"{ALICE_LID}@lid"
CONTACTS = [(f"{ALICE}@s.whatsapp.net", "Alice Example", None, None)]
CHATS = [(DIRECT_JID, "Alice Example"), (LID_JID, "Alice Example")]
PAIRS = [(ALICE_LID, ALICE)]


def _msg(mid, chat, sender, content, ts, from_me=0):
    return (mid, chat, sender, content, ts, from_me, "", "")


HELD = [
    _msg("b1", LID_JID, ALICE_LID, "After the move", "2026-10-01 10:00:00+03:00"),
    _msg("b2", LID_JID, OWNER, "Still after", "2026-10-01 10:05:00+03:00", 1),
]
REFILED = [
    _msg("b1", DIRECT_JID, ALICE, "After the move", "2026-10-01 10:00:00+03:00"),
    _msg("b2", DIRECT_JID, OWNER, "Still after", "2026-10-01 10:05:00+03:00", 1),
]


def _count(db, sql):
    return db.execute(sql).fetchone()[0]


def test_a_message_refiled_under_the_number_is_not_stored_again(db, tmp_path):
    ingest_snapshot(db, build_snapshot(tmp_path / "a", HELD, CHATS, CONTACTS, lid_map=PAIRS))
    bound_threads(db)

    out = ingest_snapshot(
        db, build_snapshot(tmp_path / "b", HELD + REFILED, CHATS, CONTACTS, lid_map=PAIRS)
    )

    assert out["messages_inserted"] == 0
    assert out["messages_updated"] == 0
    assert _count(db, "SELECT COUNT(*) FROM whatsapp_messages") == 2
    assert bound_threads(db)["threads_created"] == 0


def test_copies_already_stored_without_a_session_are_removed(db, tmp_path):
    ingest_snapshot(db, build_snapshot(tmp_path / "a", HELD, CHATS, CONTACTS, lid_map=PAIRS))
    bound_threads(db)
    chat = _count(db, "SELECT id FROM whatsapp_chats")
    # The state the VPS was in: each message a second time, under the number, unbounded.
    for mid in ("b1", "b2"):
        db.execute(
            "INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, sent_at, content)"
            " SELECT ?, message_id, chat_id, sent_at, content FROM whatsapp_messages"
            " WHERE message_id = ?",
            (DIRECT_JID, mid),
        )
    new = _msg("c1", LID_JID, ALICE_LID, "A new day", "2026-11-26 10:00:00+03:00")
    db.commit()

    out = ingest_snapshot(
        db, build_snapshot(tmp_path / "b", HELD + REFILED + [new], CHATS, CONTACTS, lid_map=PAIRS)
    )

    assert out["duplicates_removed"] == 2
    assert _count(db, "SELECT COUNT(*) FROM whatsapp_messages") == 3
    assert _count(db, f"SELECT COUNT(*) FROM whatsapp_messages WHERE chat_id = {chat}") == 3
    assert bound_threads(db)["threads_created"] == 1  # only the new day's session
    assert _count(db, "SELECT COUNT(*) FROM whatsapp_messages WHERE thread_id IS NULL") == 0
