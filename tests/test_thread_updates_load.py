"""A new email's thread updates, written by the loader through the consolidation checks
(issue 164): a decision superseded, an action done, and nothing a hostile email asks for.

Every value is invented: example addresses and made-up names.
"""

import sqlite3

import pytest

from src import config
from src.store import consolidate
from src.store.loader import load_single_email
from src.store.schema import create_database
from tests import factories as f

OWNER = ("Olga Owner", "olga@corp.example")
ALICE = ("Alice Smith", "alice@corp.example")
MALLORY = ("Mallory Stranger", "mallory@evil.example")
CONV = "conv-okapi"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", OWNER[0])
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])
    db = create_database(str(tmp_path / "brain.db"))
    yield db
    db.close()


def _row(conn, table, item_id):
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return dict(cur.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone())


def _earlier(conn):
    """Alice proposes April and asks the owner for the budget; the owner had written to her."""
    first = f.email(
        conn,
        subject="Okapi plan",
        sender=OWNER,
        to=(ALICE,),
        conversation_id=CONV,
        date_received="2026-10-04T09:00:00Z",
    )
    second = f.email(
        conn,
        subject="RE: Okapi plan",
        sender=ALICE,
        to=(OWNER,),
        conversation_id=CONV,
        date_received="2026-10-05T09:00:00Z",
        content="Shall we launch in April? Please send the budget.",
    )
    proposal = f.decision(
        conn, "Launch the okapi plan in April", email_id=second, status="proposed"
    )
    budget = f.action_item(conn, "Send the okapi budget", email_id=second, owner=OWNER[0])
    return first, second, proposal, budget


NEWEST = "Agreed, we launch in March. I sent the budget this morning."


def _metadata(sender=OWNER, message_id="new-1", content=NEWEST, date="2026-10-06T09:00:00Z"):
    return {
        "message_id": message_id,
        "conversation_id": CONV,
        "date_received": date,
        "sender": {"name": sender[0], "address": sender[1]},
        "to_recipients": [{"name": ALICE[0], "address": ALICE[1]}],
        "subject": "RE: Okapi plan",
        "content": content,
        "mailbox_name": "Inbox",
    }


def _extraction(proposal, budget, updates=None, **over):
    extraction = {
        "extraction_schema": 2,
        "summary": "The owner agrees to March and has sent the budget.",
        "topics": [],
        "decisions": [
            {
                "evidence": "Agreed, we launch in March.",
                "evidence_found": True,
                "decision": "Launch the okapi plan in March",
                "decided_by": OWNER[0],
                "owner_role": "owner_decided",
                "status": "made",
            }
        ],
        "action_items": [],
        "commitments": [],
        "key_facts": [],
        "people": [],
        "thread_updates": updates
        if updates is not None
        else [
            {
                "evidence": "Agreed, we launch in March.",
                "evidence_found": True,
                "item": "D1",
                "change": "superseded",
                "replaced_by": "1",
            },
            {
                "evidence": "I sent the budget this morning.",
                "evidence_found": True,
                "item": "A1",
                "change": "done",
                "replaced_by": "",
            },
        ],
        "thread_state_ids": {"D1": ["decisions", proposal], "A1": ["action_items", budget]},
    }
    extraction.update(over)
    return extraction


def _new_email(conn, message_id="new-1"):
    return conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[0]


def test_the_owner_settles_a_proposal_and_closes_his_action(conn):
    _, _, proposal, budget = _earlier(conn)

    assert load_single_email(conn, _metadata(), _extraction(proposal, budget))

    new_id = _new_email(conn)
    (march,) = [
        r[0] for r in conn.execute("SELECT id FROM decisions WHERE email_id = ?", (new_id,))
    ]
    old = _row(conn, "decisions", proposal)
    assert old["superseded_by"] == march
    assert old["invalid_at"] == "2026-10-06T09:00:00Z"
    action = _row(conn, "action_items", budget)
    assert action["status"] == "done"
    assert action["closed_by"] == f"email:{new_id}"
    logged = [
        tuple(r)
        for r in conn.execute(
            "SELECT label, method, thread_key, evidence FROM item_labels ORDER BY id"
        )
    ]
    assert logged == [
        ("superseded_by", "extraction", f"email:{CONV}", "Agreed, we launch in March."),
        ("completed_by", "extraction", f"email:{CONV}", "I sent the budget this morning."),
    ]


def test_the_updates_of_a_load_can_be_reverted(conn):
    _, _, proposal, budget = _earlier(conn)
    load_single_email(conn, _metadata(), _extraction(proposal, budget))

    assert consolidate.revert(conn, f"email:{CONV}") == 2

    assert _row(conn, "decisions", proposal)["superseded_by"] is None
    assert _row(conn, "action_items", budget)["status"] == "open"


def test_a_stranger_new_to_the_thread_changes_nothing(conn):
    """A hostile email that claims the owner's task done and his decision replaced."""
    _, _, proposal, budget = _earlier(conn)

    load_single_email(conn, _metadata(sender=MALLORY), _extraction(proposal, budget))

    assert _row(conn, "decisions", proposal)["superseded_by"] is None
    assert _row(conn, "action_items", budget)["status"] == "open"
    assert conn.execute("SELECT COUNT(*) FROM item_labels").fetchone()[0] == 0


def test_someone_on_the_thread_cannot_close_the_owners_action(conn):
    """Only the owner closes an action: it names no party by identifier."""
    _, _, proposal, budget = _earlier(conn)
    updates = [
        {
            "evidence": "I sent the budget this morning.",
            "evidence_found": True,
            "item": "A1",
            "change": "done",
            "replaced_by": "",
        }
    ]

    load_single_email(conn, _metadata(sender=ALICE), _extraction(proposal, budget, updates))

    assert _row(conn, "action_items", budget)["status"] == "open"


def test_a_quote_the_email_does_not_hold_changes_nothing(conn):
    _, _, proposal, budget = _earlier(conn)
    updates = [
        {
            "evidence": "We cancel the whole plan.",
            "evidence_found": True,
            "item": "D1",
            "change": "superseded",
            "replaced_by": "1",
        }
    ]

    load_single_email(conn, _metadata(), _extraction(proposal, budget, updates))

    assert _row(conn, "decisions", proposal)["superseded_by"] is None


def test_a_handle_naming_an_item_of_another_thread_changes_nothing(conn):
    _, _, proposal, budget = _earlier(conn)
    elsewhere = f.email(conn, subject="Other", conversation_id="conv-other")
    other = f.decision(conn, "Unrelated", email_id=elsewhere, status="proposed")

    load_single_email(
        conn,
        _metadata(),
        _extraction(
            proposal,
            budget,
            thread_state_ids={"D1": ["decisions", other], "A1": ["action_items", budget]},
        ),
    )

    assert _row(conn, "decisions", other)["superseded_by"] is None
    assert _row(conn, "action_items", budget)["status"] == "done"


def test_a_replacement_number_with_no_item_supersedes_nothing(conn):
    _, _, proposal, budget = _earlier(conn)
    updates = [
        {
            "evidence": "Agreed, we launch in March.",
            "evidence_found": True,
            "item": "D1",
            "change": "superseded",
            "replaced_by": "4",
        }
    ]

    load_single_email(conn, _metadata(), _extraction(proposal, budget, updates))

    assert _row(conn, "decisions", proposal)["superseded_by"] is None


def test_an_update_whose_quote_was_not_found_is_not_applied(conn):
    _, _, proposal, budget = _earlier(conn)
    updates = [
        {
            "evidence": "I sent the budget this morning.",
            "evidence_found": False,
            "item": "A1",
            "change": "done",
            "replaced_by": "",
        }
    ]

    load_single_email(conn, _metadata(), _extraction(proposal, budget, updates))

    assert _row(conn, "action_items", budget)["status"] == "open"


def test_a_v1_extraction_or_one_without_updates_changes_nothing(conn):
    _, _, proposal, budget = _earlier(conn)
    v1 = _extraction(proposal, budget, extraction_schema=None)

    load_single_email(conn, _metadata(), v1)

    assert _row(conn, "decisions", proposal)["superseded_by"] is None
    assert _row(conn, "action_items", budget)["status"] == "open"
