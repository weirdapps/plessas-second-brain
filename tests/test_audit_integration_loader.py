"""The loader hands the normaliser what it now needs, and reads old files safely.

find_or_create_person lets only a display name from the message headers rename a
person, and only one whose stored name is an address, empty or garbled; the
loader never said which names those were, so a person first seen under their
address kept it (schema-load-1, mcp-2). And the loader reads conversation
extractions from disk without the parser's list normalisation, so a file written
before it still stored a dict's repr as a fact, or stopped every conversation
load on a null (extract-core-2). All names and addresses are synthetic.
"""

from src.store.loader import load_single_conversation, load_single_email
from src.store.schema import create_database


def _extraction(people_roles=None):
    return {
        "summary": "s",
        "sentiment": "informational",
        "urgency": "low",
        "language": "english",
        "topics": [],
        "decisions": [],
        "action_items": [],
        "people_roles": people_roles or {},
        "key_facts": [],
    }


def _meta(message_id, sender, to):
    return {
        "message_id": message_id,
        "date_received": f"2026-09-0{message_id}T10:00:00Z",
        "sender": sender,
        "subject": f"s{message_id}",
        "content": "c",
        "mailbox": "Inbox",
        "to": to,
        "cc": [],
    }


def _name(conn, address):
    return conn.execute("SELECT name FROM people WHERE email = ?", (address,)).fetchone()[0]


OWNER = {"name": "Owner", "address": "owner@example.com"}


def test_a_recipients_display_name_replaces_the_address_they_were_stored_under():
    conn = create_database(":memory:")
    load_single_email(conn, _meta(1, OWNER, [{"address": "jane.roe@example.com"}]), _extraction())
    assert _name(conn, "jane.roe@example.com") == "jane.roe@example.com"

    load_single_email(
        conn,
        _meta(2, OWNER, [{"name": "Jane Roe", "address": "jane.roe@example.com"}]),
        _extraction(),
    )

    assert _name(conn, "jane.roe@example.com") == "Jane Roe"


def test_a_senders_display_name_replaces_the_address_they_were_stored_under():
    conn = create_database(":memory:")
    load_single_email(conn, _meta(1, OWNER, [{"address": "sam.poe@example.com"}]), _extraction())

    load_single_email(
        conn, _meta(2, {"name": "Sam Poe", "address": "sam.poe@example.com"}, []), _extraction()
    )

    assert _name(conn, "sam.poe@example.com") == "Sam Poe"


def test_a_people_roles_name_does_not_rename_a_person_found_by_address():
    conn = create_database(":memory:")
    to = [{"name": "Jane Roe", "address": "jane.roe@example.com"}]
    load_single_email(conn, _meta(1, OWNER, to), _extraction())

    # The model's longer label for the same recipient, matched to her address.
    load_single_email(
        conn,
        _meta(2, OWNER, [{"name": "Jane Roe (Legal)", "address": "jane.roe@example.com"}]),
        _extraction({"Jane Roe (Legal)": "FYI"}),
    )

    assert _name(conn, "jane.roe@example.com") == "Jane Roe"


def _conversation(tmp_path, extraction):
    conn = create_database(str(tmp_path / "brain.db"))
    metadata = {
        "session_id": "session-1",
        "started_at": "2026-09-01T10:00:00",
        "ended_at": "2026-09-01T11:00:00",
        "turns": [{"speaker": "user", "content": "hello", "timestamp": ""}],
    }
    assert load_single_conversation(conn, metadata, {"summary": "s", **extraction})
    return [r[0] for r in conn.execute("SELECT fact FROM key_facts ORDER BY id")]


def test_an_old_file_with_dict_decisions_loads_their_text(tmp_path):
    facts = _conversation(tmp_path, {"technical_decisions": [{"decision": "chose SQLite"}]})

    assert facts == ["[TECHNICAL] chose SQLite"]


def test_an_old_file_with_null_fields_still_loads(tmp_path):
    facts = _conversation(
        tmp_path, {"preferences_expressed": None, "technical_decisions": None, "key_facts": None}
    )

    assert facts == []


def test_a_recipient_whose_name_is_null_loads():
    conn = create_database(":memory:")
    to = [{"name": "Jane Roe", "address": "jane.roe@example.com"}]
    load_single_email(conn, _meta(1, OWNER, to), _extraction())
    meta = _meta(2, OWNER, [{"name": None, "address": "jane.roe@example.com"}])
    meta["cc"] = [{"name": None, "address": "jane.roe@example.com"}]

    assert load_single_email(conn, meta, _extraction())
    assert _name(conn, "jane.roe@example.com") == "Jane Roe"


def test_an_old_file_with_null_topics_decisions_and_actions_still_loads(tmp_path):
    facts = _conversation(tmp_path, {"topics": None, "decisions": None, "action_items": None})

    assert facts == []


def test_a_null_name_loads_beside_names_the_model_extracted():
    """people_roles looks each name up among the sender and recipients first, and
    that lookup called .lower() on a name staged as null, which is nearly every
    real extraction's path."""
    conn = create_database(":memory:")
    meta = _meta(1, {"name": None, "address": "sam.poe@example.com"}, [])
    meta["to"] = [{"name": None, "address": "jane.roe@example.com"}]

    assert load_single_email(conn, meta, _extraction({"Someone Else": "FYI"}))
