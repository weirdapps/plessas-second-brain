"""Every extracted item says who wrote the source it came from: `sender_trust`.

The 2026-11-01 security audit asked for extracted items to be tagged by sender, so a
reader can tell a decision the owner or a colleague wrote from an "action" a stranger's
mail planted. Tag only: nothing is hidden or filtered, extraction and storage are
unchanged, and the label is computed when the item is read, from its parent:

    owner     the owner wrote it (his address; his Claude Code sessions and documents)
    internal  a colleague: an address at one of the owner's domains, or a Teams thread
    known     an outside address seen in TRUSTED_MIN_EMAILS mails or more
    external  anyone else: a new outside address, news, a WhatsApp session not all his
    unknown   no sender can be told (no address, no parent)

Every value is invented.
"""

import sqlite3

import pytest

from src import config, mcp_budget, mcp_server
from src.store import envelope, person_identity, sender_trust
from src.store.context import get_person_context, get_topic_context
from src.store.query import find_overdue_actions, query_action_items, query_decisions
from src.store.schema import create_database
from tests import factories as f
from tests import seeded_store

OWNER = ("Olga Owner", "olga@corp.example")
COLLEAGUE = ("Carl Colleague", "carl@corp.example")
REGULAR = ("Rita Regular", "rita@partner.example")
STRANGER = ("Sam Stranger", "sam@elsewhere.example")


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", OWNER[0])
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", f"{OWNER[1]}, o.alias@other.example")


@pytest.fixture
def db(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    conn.row_factory = sqlite3.Row
    # Rita has written to the owner often enough to be known; Sam once.
    for n in range(person_identity.TRUSTED_MIN_EMAILS):
        f.email(conn, subject=f"Partner note {n}", sender=REGULAR, to=(OWNER,))
    return conn


# --------------------------------------------------------------- by address


def test_each_address_gets_its_label(db):
    f.email(db, subject="Hello", sender=STRANGER, to=(OWNER,))

    labels = sender_trust.of_addresses(
        db, [OWNER[1], "O.ALIAS@other.example", COLLEAGUE[1], REGULAR[1], STRANGER[1], "", None]
    )

    assert labels[OWNER[1]] == "owner"
    assert labels["o.alias@other.example"] == "owner"
    assert labels[COLLEAGUE[1]] == "internal"
    assert labels[REGULAR[1]] == "known"
    assert labels[STRANGER[1]] == "external"
    assert sender_trust.of_address(labels, "") == "unknown"
    assert sender_trust.of_address(labels, None) == "unknown"


def test_an_alias_domain_is_not_internal(db):
    """The owner's alias at another domain makes him the owner there, not everyone at it:
    only the domains of his addresses count, and other.example is one, so a colleague
    there is internal too. A domain he holds no address at is not."""
    labels = sender_trust.of_addresses(db, ["someone@other.example", "x@corp.example.net"])

    assert labels["someone@other.example"] == "internal"
    assert labels["x@corp.example.net"] == "external"


def test_known_is_the_rule_trusted_address_uses(db):
    """One rule for a trusted outside address: seen in TRUSTED_MIN_EMAILS mails."""
    rita = db.execute("SELECT id FROM people WHERE email = ?", (REGULAR[1],)).fetchone()[0]
    assert person_identity.trusted_address(db, rita)
    assert person_identity.well_seen(db, [REGULAR[1].upper()]) == {REGULAR[1]}
    f.email(db, subject="Hello", sender=STRANGER, to=(OWNER,))
    assert person_identity.well_seen(db, [STRANGER[1]]) == set()


# ------------------------------------------------------------ by parent kind


def _decision_from(db, **parent) -> int:
    return f.decision(db, "Approve the plan", **parent)


def _labels(db, ids):
    rows = query_decisions(db, limit=100, include_news=True, include_automation=True)
    return {row["decision_id"]: row["sender_trust"] for row in rows if row["decision_id"] in ids}


def test_email_items_take_their_senders_label(db):
    ids = {
        "owner": _decision_from(db, email_id=f.email(db, sender=OWNER)),
        "internal": _decision_from(db, email_id=f.email(db, sender=COLLEAGUE)),
        "known": _decision_from(db, email_id=f.email(db, sender=REGULAR)),
        "external": _decision_from(db, email_id=f.email(db, sender=STRANGER)),
        "unknown": _decision_from(db, email_id=f.email(db, sender=None)),
    }

    labels = _labels(db, set(ids.values()))

    assert {label: labels[i] for label, i in ids.items()} == {k: k for k in ids}


def test_an_attachment_item_takes_its_emails_sender(db):
    email_id = f.email(db, sender=STRANGER)
    attachment_id = f.attachment(db, email_id=email_id)
    decision = _decision_from(db, email_id=email_id, attachment_id=attachment_id)

    assert _labels(db, {decision}) == {decision: "external"}


def test_news_is_external_and_documents_are_the_owners(db):
    news = _decision_from(
        db, email_id=f.email(db, sender=COLLEAGUE, source_class="news", mailbox_name="News")
    )
    document = _decision_from(db, email_id=f.email(db, sender=STRANGER, source_class="document"))

    assert _labels(db, {news, document}) == {news: "external", document: "owner"}


def test_teams_items_are_internal(db):
    chat = f.teams_chat(db)
    thread = f.teams_thread(db, chat_id=chat)
    decision = _decision_from(db, teams_thread_id=thread)

    assert _labels(db, {decision}) == {decision: "internal"}


def _whatsapp_thread(db, *from_me: int) -> int:
    db.execute(
        "INSERT OR IGNORE INTO whatsapp_chats (id, chat_jid, name, chat_kind, first_seen_at)"
        " VALUES (1, '120000000000000001@g.us', 'Chat A', 'group', '2026-10-01T09:00:00Z')"
    )
    thread = db.execute(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at,"
        " message_count) VALUES (1, ?, '2026-10-01T09:00:00Z', '2026-10-01T09:00:00Z', ?)",
        (f"anchor-{len(from_me)}-{sum(from_me)}", len(from_me)),
    ).lastrowid
    for n, mine in enumerate(from_me):
        db.execute(
            "INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, thread_id,"
            " sender_jid, sender_name, is_from_me, sent_at, content)"
            " VALUES ('120000000000000001@g.us', ?, 1, ?, '30000000001@s.whatsapp.net',"
            " 'Carol Example', ?, '2026-10-01T09:00:00Z', 'hi')",
            (f"m{thread}-{n}", thread, mine),
        )
    db.commit()
    return thread


def test_a_whatsapp_session_is_the_owners_only_when_he_wrote_all_of_it(db):
    his = _decision_from(db, whatsapp_thread_id=_whatsapp_thread(db, 1, 1))
    mixed = _decision_from(db, whatsapp_thread_id=_whatsapp_thread(db, 1, 0))

    assert _labels(db, {his, mixed}) == {his: "owner", mixed: "external"}


def test_a_meeting_item_takes_its_organisers_label(db):
    by_colleague = _decision_from(db, event_id=f.calendar_event(db, organizer=COLLEAGUE))
    by_stranger = _decision_from(db, event_id=f.calendar_event(db, organizer=STRANGER))

    assert _labels(db, {by_colleague, by_stranger}) == {
        by_colleague: "internal",
        by_stranger: "external",
    }


def test_a_conversation_item_is_the_owners(db):
    conversation = db.execute(
        "INSERT INTO conversations (session_id, started_at, created_at)"
        " VALUES ('s1', '2026-10-01T09:00:00Z', '2026-10-01T09:00:00Z')"
    ).lastrowid
    turn = db.execute(
        "INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker,"
        " content) VALUES (?, 0, '2026-10-01T09:00:00Z', 'user', 'q')",
        (conversation,),
    ).lastrowid
    db.commit()
    decision = _decision_from(db, conversation_turn_id=turn)

    assert _labels(db, {decision}) == {decision: "owner"}


def test_an_orphan_is_unknown(db):
    decision = _decision_from(db, email_id=None)

    rows = envelope.extracted(db, [{"id": decision}], "decisions", "id")

    assert rows[0]["sender_trust"] == "unknown"


# ------------------------------------------------------ every list that serves them


def test_actions_and_overdue_actions_carry_the_label(db):
    f.action_item(db, "Pay the invoice", email_id=f.email(db, sender=STRANGER))
    f.action_item(db, "Send the slides", email_id=f.email(db, sender=OWNER), deadline="2020-01-01")

    open_ = {r["task"]: r["sender_trust"] for r in query_action_items(db, limit=50)}
    overdue = {r["task"]: r["sender_trust"] for r in find_overdue_actions(db, limit=50)}

    assert open_["Pay the invoice"] == "external"
    assert overdue == {"Send the slides": "owner"}


def test_person_and_topic_context_label_their_items(db):
    email_id = f.email(db, sender=STRANGER, to=(OWNER,), topics=("okapi",))
    f.decision(db, "Adopt the okapi plan", email_id=email_id)
    f.action_item(db, "Count the okapis", email_id=email_id)

    for context in (
        get_person_context(db, STRANGER[1]),
        get_topic_context(db, "okapi"),
    ):
        assert [d["sender_trust"] for d in context["decisions"]] == ["external"]
        assert [a["sender_trust"] for a in context["open_actions"]] == ["external"]
        assert all("sender_trust" not in e for e in context["recent_emails"])


def test_one_lookup_per_kind_however_many_items(db):
    """Batched: labelling ten email items costs what labelling one does."""

    def statements(n: int) -> int:
        rows = [{"kind": "email", "ref": f.email(db, sender=STRANGER)} for _ in range(n)]
        seen: list[str] = []
        db.set_trace_callback(seen.append)
        sender_trust.label(db, rows)
        db.set_trace_callback(None)
        return len(seen)

    assert statements(1) == statements(10)


# ------------------------------------------------------------- the MCP tools


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = seeded_store.build(tmp_path / "seeded.db")
    seeded_store.serve(path, monkeypatch)
    # Bob is the owner, so Alice, at his domain, is a colleague.
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", "bob@example.com")
    return path


def test_query_items_and_recall_label_every_item(store):
    decisions = seeded_store.call("query_items", {"kind": "decisions"})["result"]
    actions = seeded_store.call("query_items", {"kind": "actions"})["result"]
    recalled = seeded_store.call("recall", {"query": "okapi"})

    assert {r["sender_trust"] for r in decisions + actions} == {"internal"}
    for bucket in ("decisions", "actions", "commitments"):
        assert recalled[bucket], bucket
        assert {r["sender_trust"] for r in recalled[bucket]} == {"internal"}, bucket


def test_concise_rows_keep_the_label(store):
    recalled = seeded_store.call("recall", {"query": "okapi", "detail": "concise"})

    assert "sender_trust" in mcp_budget.CONCISE_KEYS
    assert {r["sender_trust"] for r in recalled["decisions"]} == {"internal"}


def test_meeting_prep_labels_its_items(store):
    prep = seeded_store.call("meeting_prep")

    dossier = prep["attendees"][0]
    assert {d["sender_trust"] for d in dossier["decisions"]} == {"internal"}
    assert {a["sender_trust"] for a in dossier["open_actions"]} == {"internal"}
    assert {d["sender_trust"] for d in prep["topic_context"]["decisions"]} == {"internal"}


def test_the_instructions_say_what_the_label_means():
    assert "sender_trust" in mcp_server._INSTRUCTIONS
    assert "`external`" in mcp_server._INSTRUCTIONS
