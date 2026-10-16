"""Who may close or supersede an item: identifiers on the stored rows, order and dates known.

The owner is an email from an address BRAIN_USER_EMAIL_PATTERN lists, in any folder (he
copies himself and clears Sent Items); never a Teams message, since the store keeps no MRI
of the signed-in owner. A counterparty is the From of the item's own email, named on it.
Unknown means refused. Every value is invented.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from src import config
from src.extract import claude_extract
from src.llm_cost import TokenBudget
from src.store import consolidate as c
from src.store.schema import create_database
from tests import factories as f

OWNER = ("Olga Owner", "olga@corp.example")
ALICE = ("Alice Smith", "alice@corp.example")
BOB = ("Bob Jones", "bob@corp.example")
MALLORY = ("Mallory Stranger", "mallory@evil.example")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", OWNER[0])
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])
    db = create_database(str(tmp_path / "brain.db"))
    yield db
    db.close()


@pytest.fixture
def model(monkeypatch, tmp_path):
    stub = SimpleNamespace(labels=lambda user: [])

    def create(client, *, after_refusal=None, **kwargs):
        labels = stub.labels(kwargs["messages"][0]["content"])
        return SimpleNamespace(
            id="msg",
            content=[SimpleNamespace(type="text", text=json.dumps({"labels": labels}))],
            stop_reason="end_turn",
            stop_details=None,
            model="claude-sonnet-5-5",
            usage=None,
        )

    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (object(), "m"))
    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", create)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    return stub


def _mail(conn, sender, to, days, summary, mailbox="Inbox"):
    return f.email(
        conn,
        subject="Okapi",
        sender=sender,
        to=to,
        conversation_id="conv",
        date_received=f.ago(days=days),
        summary=summary,
        content=summary,
        mailbox_name=mailbox,
    )


def _row(conn, table, item_id):
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return dict(cur.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone())


def _pid(conn, contact):
    return conn.execute("SELECT id FROM people WHERE email = ?", (contact[1],)).fetchone()[0]


def _close(prefix, target, evidence):
    def labels(user):
        handle = next(line.split()[0] for line in user.splitlines() if line.startswith(prefix))
        return [{"item": handle, "label": "completed_by", "target": target, "evidence": evidence}]

    return labels


def _run(conn):
    return c.consolidate(conn, since=f.ago(days=365)[:10], budget=TokenBudget(100_000))


# --- the owner, by his address, in any folder ------------------------------------------------


@pytest.mark.parametrize("folder", ["Inbox", "Archive", "Sent Items"])
def test_the_owners_own_copy_closes_his_action_in_any_folder(conn, model, folder):
    request = _mail(conn, ALICE, (OWNER,), 3, "Olga, please send the deck")
    _mail(conn, OWNER, (ALICE, OWNER), 2, "The deck was sent this morning.", folder)
    action = f.action_item(conn, "Send the deck", email_id=request, owner=OWNER[0])
    model.labels = _close("A", "M2", "The deck was sent")

    _run(conn)

    assert _row(conn, "action_items", action)["status"] == "done"


def test_a_spoofed_display_name_with_a_foreign_address_is_not_the_owner(conn, model):
    request = _mail(conn, ALICE, (OWNER,), 3, "Olga, please send the deck")
    _mail(conn, (OWNER[0], "olga@evil.example"), (ALICE,), 2, "The deck was sent.")
    action = f.action_item(conn, "Send the deck", email_id=request, owner=OWNER[0])
    model.labels = _close("A", "M2", "The deck was sent")

    _run(conn)

    assert _row(conn, "action_items", action)["status"] == "open"


# --- Teams: no stored self MRI, so never the owner -----------------------------------------


def test_a_teams_message_under_the_owners_resolved_mri_is_not_the_owner(conn, model):
    conn.execute(
        "INSERT INTO teams_mri_resolution (mri, email, display_name, status)"
        " VALUES ('8:orgid:olga', ?, ?, 'resolved')",
        (OWNER[1], OWNER[0]),
    )
    thread = f.teams_thread(conn, ended_at=f.ago(days=1))
    for days, sender, mri, text in (
        (3, OWNER, "8:orgid:olga", "I will send the deck"),
        (1, OWNER, "8:orgid:olga", "Deck sent just now."),
    ):
        f.teams_message(
            conn, sender=sender, thread_id=thread, sender_mri=mri,
            composed_at=f.ago(days=days), content_text=text,
        )  # fmt: skip
    action = f.action_item(conn, "Send the deck", teams_thread_id=thread, owner=OWNER[0])
    model.labels = _close("A", "M2", "Deck sent")

    _run(conn)

    assert _row(conn, "action_items", action)["status"] == "open"


# --- order and dates must be known, and strictly later ---------------------------------------


@pytest.mark.parametrize("closing_date", ["", "same"], ids=["no date", "the same date"])
def test_a_close_with_an_unknown_or_equal_date_is_refused(conn, model, closing_date):
    request = _mail(conn, ALICE, (OWNER,), 3, "Olga, please send the deck")
    closing = _mail(conn, OWNER, (ALICE,), 2, "The deck was sent this morning.")
    when = (
        ""
        if closing_date == ""
        else conn.execute("SELECT date_received FROM emails WHERE id = ?", (request,)).fetchone()[0]
    )
    conn.execute("UPDATE emails SET date_received = ? WHERE id = ?", (when, closing))
    conn.commit()
    action = f.action_item(conn, "Send the deck", email_id=request, owner=OWNER[0])
    model.labels = _close("A", "M1" if closing_date == "" else "M2", "The deck was sent")

    _run(conn)

    assert _row(conn, "action_items", action)["status"] == "open"


def test_a_whatsapp_item_from_no_one_message_is_never_closed(conn, model):
    chat = conn.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at)"
        " VALUES ('30000000001@s.whatsapp.net', 'Alice', 'direct', ?)",
        (f.ago(days=5),),
    ).lastrowid
    thread = conn.execute(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at)"
        " VALUES (?, 'm1', ?, ?)",
        (chat, f.ago(days=3), f.ago(days=1)),
    ).lastrowid
    for n, (me, text, days) in enumerate(
        ((0, "Can you send the deck?", 3), (1, "Deck sent!", 1)), 1
    ):
        conn.execute(
            "INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, thread_id, sender_jid,"
            " sender_name, is_from_me, sent_at, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("30000000001@s.whatsapp.net", f"m{n}", chat, thread,
             None if me else "30000000001@s.whatsapp.net", "Alice", me, f.ago(days=days), text),
        )  # fmt: skip
    conn.commit()
    action = f.action_item(conn, "Send the deck", whatsapp_thread_id=thread, owner=OWNER[0])
    model.labels = _close("A", "M2", "Deck sent!")

    _run(conn)

    assert _row(conn, "action_items", action)["status"] == "open"


# --- a counterparty is the From of the item's own email, named on it ------------------------


def _promise(conn, email_id, **columns):
    values = {"email_id": email_id, "commitment": "Send the okapi figures", **columns}
    cur = conn.execute(
        f"INSERT INTO commitments ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        tuple(values.values()),
    )
    conn.commit()
    return cur.lastrowid


def test_the_party_who_wrote_the_promise_closes_it(conn, model):
    promised = _mail(conn, ALICE, (OWNER,), 3, "I will send the okapi figures")
    _mail(conn, ALICE, (OWNER,), 2, "Figures sent, see attached.")
    promise = _promise(conn, promised, by_person=ALICE[0], by_person_id=_pid(conn, ALICE))
    model.labels = _close("C", "M2", "Figures sent")

    _run(conn)

    assert _row(conn, "commitments", promise)["status"] == "done"


def test_a_recipient_named_on_a_promise_is_no_counterparty(conn, model):
    promised = _mail(conn, ALICE, (OWNER, BOB), 3, "I will send Bob the okapi figures")
    _mail(conn, BOB, (ALICE,), 2, "Got the figures, thanks.")
    promise = _promise(conn, promised, to_person=BOB[0], to_person_id=_pid(conn, BOB))
    model.labels = _close("C", "M2", "Got the figures")

    _run(conn)

    assert _row(conn, "commitments", promise)["status"] is None


def test_a_planted_promise_naming_an_outsider_gives_them_nothing(conn, model):
    promised = _mail(conn, ALICE, (OWNER, MALLORY), 3, "Mallory will send the figures")
    _mail(conn, MALLORY, (ALICE,), 2, "Figures sent.")
    promise = _promise(conn, promised, by_person=MALLORY[0], by_person_id=_pid(conn, MALLORY))
    model.labels = _close("C", "M2", "Figures sent")

    _run(conn)

    assert _row(conn, "commitments", promise)["status"] is None


def test_a_party_whose_name_disagrees_with_the_header_is_none(conn, model):
    promised = _mail(conn, ALICE, (OWNER,), 3, "I will send the okapi figures")
    _mail(conn, ALICE, (OWNER,), 2, "Figures sent, see attached.")
    promise = _promise(conn, promised, by_person="Zoe Other", by_person_id=_pid(conn, ALICE))
    model.labels = _close("C", "M2", "Figures sent")

    _run(conn)

    assert _row(conn, "commitments", promise)["status"] is None


# --- the owner's decisions --------------------------------------------------------------------


def _supersede(user):
    handles = [line.split()[0] for line in user.splitlines() if line.startswith("D")]
    return [
        {"item": handles[0], "label": "superseded_by", "target": handles[1], "evidence": "cancel"}
    ]


def test_someone_elses_later_decision_contests_the_owners_but_hides_nothing(conn, model):
    first = _mail(conn, OWNER, (ALICE,), 3, "We adopt the okapi plan")
    second = _mail(conn, ALICE, (OWNER,), 2, "We cancel the okapi plan")
    owners = f.decision(conn, "Adopt the okapi plan", email_id=first, status="made")
    f.decision(conn, "We cancel the okapi plan", email_id=second, status="made")
    model.labels = _supersede

    _run(conn)

    assert _row(conn, "decisions", owners)["superseded_by"] is None
    assert c.hidden_ids(conn, "decisions", [owners]) == set()
    label = conn.execute("SELECT label FROM item_labels WHERE item_id = ?", (owners,)).fetchone()
    assert label[0] == "contested_by"


def test_the_owner_taking_it_up_afterwards_lets_it_supersede(conn, model):
    first = _mail(conn, OWNER, (ALICE,), 4, "We adopt the okapi plan")
    second = _mail(conn, ALICE, (OWNER,), 3, "We cancel the okapi plan")
    _mail(conn, OWNER, (ALICE,), 2, "Agreed, cancelled.")
    owners = f.decision(conn, "Adopt the okapi plan", email_id=first, status="made")
    other = f.decision(conn, "We cancel the okapi plan", email_id=second, status="made")
    model.labels = _supersede

    _run(conn)

    assert _row(conn, "decisions", owners)["superseded_by"] == other


def test_a_rule_duplicate_never_hides_the_owners_copy(conn):
    first = _mail(conn, ALICE, (OWNER,), 3, "plan")
    second = _mail(conn, OWNER, (ALICE,), 2, "plan")
    earlier = f.decision(conn, "Adopt the okapi plan", email_id=first)
    owners = f.decision(conn, "Adopt the okapi plan", email_id=second)

    c.consolidate(conn, since=f.ago(days=365)[:10], model=False)

    assert _row(conn, "decisions", owners)["canonical_id"] is None
    assert _row(conn, "decisions", earlier)["canonical_id"] == owners


def test_a_negation_is_never_a_duplicate(conn):
    first = _mail(conn, ALICE, (OWNER,), 3, "plan")
    second = _mail(conn, BOB, (OWNER,), 2, "plan")
    yes = f.decision(conn, "We approve the okapi plan", email_id=first)
    no = f.decision(conn, "We do not approve the okapi plan", email_id=second)

    c.consolidate(conn, since=f.ago(days=365)[:10], model=False)

    assert _row(conn, "decisions", yes)["canonical_id"] is None
    assert _row(conn, "decisions", no)["canonical_id"] is None
