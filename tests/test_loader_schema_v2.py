"""The loader takes schema v1 and schema v2 extractions side by side, forever.

Schema v2 (src/extract/schema_v2.py) keeps v1's names for what the tables already
hold, so decisions, action items, commitments, key facts and topics load as before. Its
`people` array replaces `people_roles`: a person's address is taken from the model only
when it is one of the email's own header addresses, so neither a hallucination nor a
planted line in a body can tie a name to an address. Its ISO language code is stored as
the Teams and WhatsApp threads store theirs. What has no column yet (evidence,
owner_role, decision status, direction, item kind, the Latin name) stays in the
extraction file on disk.
"""

import pytest

from src.store.loader import load_single_email, replace_extraction
from src.store.schema import create_database


def _meta(message_id=1):
    return {
        "message_id": message_id,
        "date_received": "2026-10-05T09:00:00",
        "sender": {"name": "Sam Carter", "address": "sam.carter@example.com"},
        "subject": "Q4 vendor review",
        "content": "body",
        "mailbox_name": "Inbox",
        "to_recipients": [{"name": "Owner Person", "address": "owner@example.com"}],
        "cc_recipients": [{"name": "Dana Lee", "address": "dana.lee@example.com"}],
    }


def _v2(**over):
    extraction = {
        "item_kind": "work_thread",
        "language": "el",
        "summary": "Περίληψη.",
        "topics": ["vendor review"],
        "sentiment": "collaborative",
        "urgency": "medium",
        "decisions": [
            {
                "evidence": "εγκρίθηκε η λίστα",
                "decision": "Έγκριση της λίστας",
                "decided_by": "Audit team",
                "owner_role": "owner_fyi",
                "status": "made",
                "evidence_found": True,
            }
        ],
        "action_items": [
            {
                "evidence": "στείλε σχόλια",
                "task": "Αποστολή σχολίων",
                "owner": "Owner Person",
                "direction": "owner_owes",
                "deadline": "2026-10-16",
                "evidence_found": True,
            }
        ],
        "commitments": [
            {"evidence": "θα το στείλω", "commitment": "Αποστολή", "by": "Sam", "to": "Owner"}
        ],
        "key_facts": ["40.000 ευρώ"],
        "people": [],
        "extraction_schema": 2,
    }
    extraction.update(over)
    return extraction


@pytest.fixture
def conn():
    c = create_database(":memory:")
    yield c
    c.close()


def _people(conn, role=None):
    sql = (
        "SELECT p.name, p.email, ep.role_in_email FROM email_people ep"
        " JOIN people p ON p.id = ep.person_id"
    )
    rows = conn.execute(sql).fetchall()
    return sorted(
        (tuple(r) for r in rows if role is None or r[2] == role), key=lambda r: (r[0], r[2])
    )


def _rows(conn, sql):
    return [tuple(r) for r in conn.execute(sql).fetchall()]


def test_a_v2_extraction_writes_the_rows_a_v1_one_does(conn):
    assert load_single_email(conn, _meta(), _v2())

    assert _rows(conn, "SELECT summary, sentiment, urgency, language FROM emails") == [
        ("Περίληψη.", "collaborative", "medium", "el")
    ]
    assert _rows(conn, "SELECT decision, decided_by FROM decisions") == [
        ("Έγκριση της λίστας", "Audit team")
    ]
    assert _rows(conn, "SELECT task, owner, deadline, status FROM action_items") == [
        ("Αποστολή σχολίων", "Owner Person", "2026-10-16", "open")
    ]
    assert _rows(conn, "SELECT commitment, by_person, to_person FROM commitments") == [
        ("Αποστολή", "Sam", "Owner")
    ]
    assert _rows(conn, "SELECT fact FROM key_facts") == [("40.000 ευρώ",)]
    assert _rows(conn, "SELECT name FROM topics") == [("vendor review",)]


def test_a_person_the_model_gives_a_header_address_joins_that_header_person(conn):
    """The Greek name in the body and the Latin display name in the headers are one
    person: the row keeps the header's name and address, and gains the model's role."""
    people = [
        {
            "name_as_written": "Ντάνα Λι",
            "name_latin": "Dana Lee",
            "email": "Dana.Lee@example.com",
            "role": "approver",
        }
    ]

    load_single_email(conn, _meta(), _v2(people=people))

    assert ("Dana Lee", "dana.lee@example.com", "approver") in _people(conn)
    assert conn.execute("SELECT COUNT(*) FROM people WHERE name = 'Ντάνα Λι'").fetchone()[0] == 0


def test_an_address_the_headers_do_not_hold_is_not_taken_from_the_model(conn):
    """A body can carry any address, a planted one included: the person is kept by name."""
    people = [
        {
            "name_as_written": "Γιώργος Νικολάου",
            "name_latin": "Giorgos Nikolaou",
            "email": "giorgos@attacker.example",
            "role": "requester",
        }
    ]

    load_single_email(conn, _meta(), _v2(people=people))

    assert ("Γιώργος Νικολάου", None, "requester") in _people(conn)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM people WHERE email = 'giorgos@attacker.example'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("field", ["name_as_written", "name_latin"])
def test_a_person_without_an_address_is_matched_to_a_header_name_in_either_script(conn, field):
    person = {"name_as_written": "Σαμ Κάρτερ", "name_latin": "Sam Carter", "email": "", "role": "r"}
    person[field] = "Sam Carter"

    load_single_email(conn, _meta(), _v2(people=[person]))

    assert ("Sam Carter", "sam.carter@example.com", "r") in _people(conn)


def test_a_v1_extraction_still_loads_its_people_roles(conn):
    v1 = {
        "summary": "s",
        "sentiment": "informational",
        "urgency": "low",
        "language": "english",
        "topics": [],
        "decisions": [{"decision": "d", "decided_by": "x"}],
        "action_items": [],
        "commitments": [],
        "people_roles": {"Dana Lee": "reviewer", "Someone Else": ["informed", "cc"]},
        "key_facts": [],
        "references": [],
    }

    load_single_email(conn, _meta(), v1)

    assert ("Dana Lee", "dana.lee@example.com", "reviewer") in _people(conn)
    assert ("Someone Else", None, "cc") in _people(conn)
    assert conn.execute("SELECT language FROM emails").fetchone()[0] == "english"


def test_a_v1_extraction_with_a_stray_people_key_is_read_as_v1(conn):
    """v1's parser keeps keys it does not know; only the v2 mark makes an extraction v2."""
    v1 = {
        "summary": "s",
        "language": "english",
        "people_roles": {"Dana Lee": "reviewer"},
        "people": [{"name_as_written": "Not Loaded", "role": "x"}],
    }

    load_single_email(conn, _meta(), v1)

    assert ("Dana Lee", "dana.lee@example.com", "reviewer") in _people(conn)
    assert not [p for p in _people(conn) if p[0] == "Not Loaded"]


def test_replace_extraction_takes_a_v2_extraction(conn):
    """The stub retry writes whatever extract_one returns, v2 under call shape v2."""
    meta = _meta()
    stub = {"summary": "", "topics": [], "decisions": [], "people_roles": {}}
    load_single_email(conn, meta, stub)
    email_id = conn.execute("SELECT id FROM emails").fetchone()[0]
    people = [{"name_as_written": "Dana", "name_latin": "Dana", "email": "", "role": "approver"}]

    replace_extraction(conn, email_id, meta, wrong=stub, right=_v2(people=people))

    assert _rows(conn, "SELECT language, summary FROM emails") == [("el", "Περίληψη.")]
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    assert ("Dana", None, "approver") in _people(conn)
