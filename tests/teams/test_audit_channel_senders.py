"""A channel post records who sent it.

Chat messages give the sender as a contacts URL ('.../contacts/8:orgid:<oid>') and
channel posts give the bare MRI, and only the URL was read: every one of the 4,817
channel posts on the replica of 2026-10-20 had no sender_mri and so no person, and
person_context never showed one (audit S2-3). Their payloads keep the MRI, so
backfill_senders fills the stored rows and links them through the MRI cache, as
resolve_mris does for new ones. Synthetic people only.
"""

import json
from unittest.mock import patch

from src.export.teams_export import _extract_mri, backfill_senders, pull_messages

OID = "8:orgid:00000000-0000-0000-0000-00000000aaaa"
BOT = "28:00000000-0000-0000-0000-00000000b0b0"


def _seed_channel(db):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, team_uuid, team_name,"
        " channel_id, first_seen_at) VALUES ('19:chan@thread.tacv2', 'channel', 'Ops',"
        " 'team-uuid', 'Team', '19:chan@thread.tacv2', '2026-08-01T00:00:00')"
    )
    db.commit()
    return db.execute("SELECT id FROM teams_chats").fetchone()[0]


def _post(post_id, sender, name="Novak, Maria", replies=()):
    return {
        "containerId": "19:chan@thread.tacv2",
        "id": post_id,
        "latestMessageTime": "2026-10-01T09:00:00.000Z",
        "message": {
            "messageType": "RichText/Html",
            "content": "<p>Quarterly plan attached, comments by Friday please.</p>",
            "imDisplayName": name,
            "fromDisplayNameInToken": name,
            "from": sender,
            "composeTime": "2026-10-01T08:00:00.000Z",
            "id": post_id,
        },
        "replies": {"messages": list(replies)},
    }


def _reply(reply_id, sender, name):
    return {
        "id": reply_id,
        "messageType": "RichText/Html",
        "content": "<p>Looks good to me.</p>",
        "imDisplayName": name,
        "from": sender,
        "composeTime": "2026-10-01T08:30:00.000Z",
    }


def test_a_bare_mri_is_read_as_well_as_a_contacts_url():
    assert _extract_mri(OID) == OID
    assert (
        _extract_mri(f"https://emea.ng.msg.teams.microsoft.com/v1/users/ME/contacts/{OID}") == OID
    )
    assert _extract_mri(BOT) == BOT
    assert _extract_mri("19:abc@thread.tacv2") == "19:abc@thread.tacv2"
    assert _extract_mri("") is None
    assert _extract_mri(None) is None
    assert _extract_mri("Novak, Maria") is None


def test_a_pulled_channel_post_and_its_reply_record_their_senders(db):
    _seed_channel(db)
    payload = {"posts": [_post("1001", OID, replies=[_reply("1002", BOT, "Planner")])]}

    with patch("src.export.teams_export.run_teams_cli", return_value=payload):
        pull_messages(db, concurrency=1)

    rows = db.execute(
        "SELECT teams_message_id, sender_mri FROM teams_messages ORDER BY composed_at"
    ).fetchall()
    assert [r["sender_mri"] for r in rows] == [OID, BOT]


def test_a_sender_with_no_im_name_takes_the_name_in_the_token(db):
    _seed_channel(db)
    post = _post("1001", OID)
    del post["message"]["imDisplayName"]
    with patch("src.export.teams_export.run_teams_cli", return_value={"posts": [post]}):
        pull_messages(db, concurrency=1)

    assert db.execute("SELECT sender_display_name FROM teams_messages").fetchone()[0] == (
        "Novak, Maria"
    )


def _store_as_before(db, chat_id, post_id, sender, name):
    """A post as the old parser stored it: payload kept, no sender MRI."""
    raw = _post(post_id, sender, name)
    if name is None:
        del raw["message"]["imDisplayName"]
        del raw["message"]["fromDisplayNameInToken"]
    db.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, sender_mri,"
        " sender_display_name, composed_at, content_text, raw_json)"
        " VALUES (?, ?, NULL, ?, '2026-10-01T08:00:00.000Z', 'Quarterly plan', ?)",
        (f"{chat_id}::{post_id}", chat_id, name, json.dumps(raw)),
    )
    db.commit()


def _resolved(db, mri, person_id, name="Novak, Maria"):
    db.execute(
        "INSERT INTO teams_mri_resolution (mri, email, display_name, person_id, status,"
        " resolved_at, last_attempt_at) VALUES (?, 'novak@example.com', ?, ?, 'resolved',"
        " '2026-09-01', '2026-09-01')",
        (mri, name, person_id),
    )
    db.commit()


def test_backfill_fills_the_sender_links_the_person_and_names_a_nameless_post(db):
    chat_id = _seed_channel(db)
    db.execute("INSERT INTO people (name, email) VALUES ('Novak, Maria', 'novak@example.com')")
    person = db.execute("SELECT id FROM people").fetchone()[0]
    _resolved(db, OID, person)
    _store_as_before(db, chat_id, "1001", OID, "Novak, Maria")
    _store_as_before(db, chat_id, "1003", OID, None)
    _store_as_before(db, chat_id, "1004", "8:orgid:00000000-0000-0000-0000-0000000000cc", "Other")

    counts = backfill_senders(db)

    rows = db.execute(
        "SELECT sender_mri, sender_display_name, sender_person_id FROM teams_messages ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (OID, "Novak, Maria", person),
        (OID, "Novak, Maria", person),
        # Not resolved yet: the hourly resolve_mris looks it up and links it.
        ("8:orgid:00000000-0000-0000-0000-0000000000cc", "Other", None),
    ]
    assert counts["senders_filled"] == 3
    assert counts["persons_linked"] == 2
    assert counts["names_filled"] == 1


def test_backfill_is_idempotent_and_a_dry_run_writes_nothing(db):
    chat_id = _seed_channel(db)
    _store_as_before(db, chat_id, "1001", OID, "Novak, Maria")

    dry = backfill_senders(db, dry_run=True)
    assert dry["senders_filled"] == 1
    assert db.execute("SELECT sender_mri FROM teams_messages").fetchone()[0] is None

    backfill_senders(db)
    again = backfill_senders(db)
    assert again["senders_filled"] == 0
    assert again["persons_linked"] == 0
    assert again["names_filled"] == 0


def test_backfill_leaves_a_row_without_a_payload_alone(db):
    chat_id = _seed_channel(db)
    db.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, composed_at, content_text)"
        " VALUES (?, ?, '2026-10-01T08:00:00Z', 'x')",
        (f"{chat_id}::9", chat_id),
    )
    db.commit()

    counts = backfill_senders(db)

    assert counts["senders_filled"] == 0
    assert counts["without_sender"] == 1
