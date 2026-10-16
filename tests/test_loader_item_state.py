"""The loader stores what schema v2 says about each item (schema v35), and takes no
decision, action or commitment from news or the owner's automation mail.

Of 147,770 decisions on the replica of 2026-10-16, 25,021 came from automation mail and
19,606 from news: a job's report or a news digest decides nothing for the owner. Their key
facts and topics stay, marked by the email's source class as before.
"""

import pytest

import src.config as config
from src.extract.parser import parse_extraction_v2
from src.store import item_cleanup
from src.store.dedup_people import merge_person
from src.store.loader import load_single_email, replace_extraction, stored_email
from src.store.schema import create_database

OWNER = {"name": "Owner Person", "address": "owner@example.com"}
COLLEAGUE = {"name": "Ann Example", "address": "ann@example.com"}

BODY = (
    "We approved the pilot budget.\nI will send the signed copy by 6 March.\n"
    "Ann, please book the room by Friday."
)


def metadata(**over):
    out = {
        "message_id": "m-1",
        "internet_message_id": "<m-1@example.com>",
        "date_received": "2026-03-02T10:15:00Z",
        "sender": OWNER,
        "to_recipients": [COLLEAGUE],
        "cc_recipients": [],
        "subject": "RE: Pilot budget",
        "content": BODY,
        "mailbox_name": "Sent Items",
        "conversation_id": "conv-1",
    }
    out.update(over)
    return out


def v2_reply(**over):
    answer = {
        "item_kind": "work_thread",
        "language": "en",
        "summary": "The pilot budget is approved.",
        "topics": ["pilot programme"],
        "sentiment": "directive",
        "urgency": "medium",
        "decisions": [
            {
                "evidence": "We approved the pilot budget.",
                "decision": "Approve the pilot budget",
                "decided_by": "Owner Person",
                "owner_role": "owner_decided",
                "status": "made",
            },
            {
                "evidence": "a quote the email does not hold",
                "decision": "Hire two analysts",
                "decided_by": "Owner Person",
                "owner_role": "owner_decided",
                "status": "proposed",
            },
        ],
        "action_items": [
            {
                "evidence": "Ann, please book the room by Friday.",
                "task": "Book the room",
                "owner": "Ann Example",
                "direction": "owed_to_owner",
                "deadline": "2026-03-06",
            }
        ],
        "commitments": [
            {
                "evidence": "I will send the signed copy by 6 March.",
                "commitment": "Send the signed copy",
                "by": "Owner Person",
                "to": "Ann Example",
                "direction": "owner_owes",
                "due": "2026-03-06",
            }
        ],
        "key_facts": ["The pilot budget is approved"],
        "people": [
            {
                "name_as_written": "Owner Person",
                "name_latin": "Owner Person",
                "email": "owner@example.com",
                "role": "approver",
            },
            {
                "name_as_written": "Ann Example",
                "name_latin": "Ann Example",
                "email": "ann@example.com",
                "role": "assignee",
            },
        ],
    }
    answer.update(over)
    import json

    return parse_extraction_v2(json.dumps(answer), source=f"Subject: x\n\n{BODY}")


V1_EXTRACTION = {
    "summary": "s",
    "topics": ["pilot"],
    "decisions": [{"decision": "Approve the pilot budget", "decided_by": "Owner Person"}],
    "action_items": [{"task": "Book the room", "owner": "Ann", "deadline": None}],
    "commitments": [{"commitment": "Send the copy", "by": "Owner Person", "to": "Ann"}],
    "people_roles": {"Ann Example": "assignee"},
    "sentiment": "directive",
    "urgency": "low",
    "language": "english",
    "key_facts": ["A fact"],
    "references": [],
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", "owner@example.com")
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _rows(conn, sql):
    return [tuple(r) for r in conn.execute(sql)]


# --- schema v2 items carry their state -------------------------------------------------


def test_a_v2_decision_keeps_its_status_role_quote_and_the_date_of_its_message(store):
    load_single_email(store, metadata(), v2_reply())

    assert _rows(
        store, "SELECT decision, status, owner_role, evidence, decision_date FROM decisions"
    ) == [
        (
            "Approve the pilot budget",
            "made",
            "owner_decided",
            "We approved the pilot budget.",
            "2026-03-02",
        ),
        # A quote the email does not hold is not kept as evidence; the item stays.
        ("Hire two analysts", "proposed", "owner_decided", None, "2026-03-02"),
    ]


def test_a_v2_action_keeps_its_direction_and_quote(store):
    load_single_email(store, metadata(), v2_reply())

    assert _rows(store, "SELECT task, direction, evidence, status FROM action_items") == [
        ("Book the room", "owed_to_owner", "Ann, please book the room by Friday.", "open")
    ]


def test_a_v2_commitment_keeps_its_due_date_direction_status_and_people(store):
    load_single_email(store, metadata(), v2_reply())

    [row] = store.execute(
        "SELECT c.due, c.direction, c.status, c.evidence, b.email, t.email FROM commitments c"
        " LEFT JOIN people b ON b.id = c.by_person_id"
        " LEFT JOIN people t ON t.id = c.to_person_id"
    ).fetchall()
    assert tuple(row) == (
        "2026-03-06",
        "owner_owes",
        "open",
        "I will send the signed copy by 6 March.",
        "owner@example.com",
        "ann@example.com",
    )


def test_a_commitment_to_someone_the_email_does_not_name_has_no_person(store):
    extraction = v2_reply()
    extraction["commitments"][0]["to"] = "The steering committee"

    load_single_email(store, metadata(), extraction)

    assert _rows(store, "SELECT by_person_id IS NOT NULL, to_person_id FROM commitments") == [
        (1, None)
    ]


def test_a_v1_extraction_leaves_the_new_columns_empty(store):
    load_single_email(store, metadata(), V1_EXTRACTION)

    assert _rows(store, "SELECT status, owner_role, evidence, decision_date FROM decisions") == [
        (None, None, None, None)
    ]
    assert _rows(store, "SELECT direction, evidence FROM action_items") == [(None, None)]
    assert _rows(
        store, "SELECT due, direction, status, by_person_id, evidence FROM commitments"
    ) == [(None, None, None, None, None)]


def test_merging_people_moves_their_commitments(store):
    load_single_email(store, metadata(), v2_reply())
    ann = store.execute("SELECT id FROM people WHERE email = 'ann@example.com'").fetchone()[0]
    store.execute("INSERT INTO people (name, email) VALUES ('Ann E.', 'ann.e@example.com')")
    other = store.execute("SELECT id FROM people WHERE email = 'ann.e@example.com'").fetchone()[0]

    merge_person(store, other, ann)

    assert _rows(store, "SELECT to_person_id FROM commitments") == [(other,)]


# --- news and automation give no items --------------------------------------------------

AUTOMATION = {
    "sender": OWNER,
    "to_recipients": [OWNER],
    "subject": "[nightly] backup report 2026-03-02",
    "mailbox_name": "Inbox",
}
NEWS = {"sender": {"name": "Feed", "address": "feed@news.local"}, "mailbox_name": "News"}


@pytest.mark.parametrize("kind", [AUTOMATION, NEWS], ids=["automation", "news"])
@pytest.mark.parametrize("extraction", [V1_EXTRACTION, None], ids=["v1", "v2"])
def test_news_and_automation_give_no_decisions_actions_or_commitments(store, kind, extraction):
    load_single_email(store, metadata(**kind), extraction or v2_reply())

    assert store.execute("SELECT source_class FROM emails").fetchone()[0] in ("automation", "news")
    for table in ("decisions", "action_items", "commitments"):
        assert store.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert store.execute("SELECT COUNT(*) FROM key_facts").fetchone()[0] == 1
    assert store.execute("SELECT COUNT(*) FROM email_topics").fetchone()[0] == 1


def test_a_replaced_extraction_of_automation_mail_gives_no_items_either(store):
    load_single_email(store, metadata(**AUTOMATION), {"summary": ""})
    email_id = store.execute("SELECT id FROM emails").fetchone()[0]

    replace_extraction(store, email_id, stored_email(store, email_id), wrong={}, right=v2_reply())

    assert store.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert store.execute("SELECT COUNT(*) FROM key_facts").fetchone()[0] == 1


# --- boilerplate -------------------------------------------------------------------------


def test_a_new_fact_with_known_boilerplate_text_is_marked_as_it_is_stored(store):
    store.execute(
        "INSERT INTO boilerplate_fact_keys (key, threads, marked_at) VALUES (?, 7, 'x')",
        (item_cleanup.fact_key("This e-mail is marked RESTRICTED USE."),),
    )
    extraction = v2_reply(key_facts=["This e-mail is marked  restricted use", "Budget: 40,000"])

    load_single_email(store, metadata(), extraction)

    assert _rows(store, "SELECT fact, boilerplate FROM key_facts ORDER BY id") == [
        ("This e-mail is marked  restricted use", 1),
        ("Budget: 40,000", 0),
    ]
