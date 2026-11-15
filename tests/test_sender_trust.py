"""Every extracted item says who wrote the source it came from: `sender_trust`.

The 2026-11-01 security audit asked for extracted items to be tagged by sender, so a
reader can tell a decision the owner or a colleague wrote from an "action" a stranger's
mail planted. Tag only: nothing is hidden or filtered, extraction and storage are
unchanged, and the label is computed when the item is read, from its parent:

    owner     the owner wrote it (his address; his Claude Code sessions)
    internal  a colleague: an address at one of the owner's domains, or a Teams thread
              with no sender resolved outside them
    known     an outside address the owner has written to; inbound mail never counts
    external  anyone else: a new outside address, news, a Teams guest, a WhatsApp
              session not all his
    unknown   no sender can be told (a document, no address, no parent)

The label is the stated sender's: a From header is not authenticated.

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
    # The owner has written to Rita, so she is known.
    f.email(conn, subject="Partner note", sender=OWNER, to=(REGULAR,))
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


def test_inbound_mail_never_makes_an_address_known(db):
    """An attacker can send three mails; only the owner writing to them makes them known."""
    for n in range(person_identity.TRUSTED_MIN_EMAILS + 2):
        f.email(db, subject=f"Urgent {n}", sender=STRANGER, to=(OWNER,), cc=(COLLEAGUE,))
    f.email(db, subject="Fwd", sender=COLLEAGUE, to=(STRANGER,))  # a colleague, not him

    assert sender_trust.of_addresses(db, [STRANGER[1]]) == {STRANGER[1]: "external"}
    assert sender_trust.written_to(db, [REGULAR[1].upper(), STRANGER[1]]) == {REGULAR[1]}


def test_a_cc_from_the_owner_makes_an_address_known(db):
    f.email(db, subject="Intro", sender=OWNER, to=(COLLEAGUE,), cc=(STRANGER,))

    assert sender_trust.of_addresses(db, [STRANGER[1]]) == {STRANGER[1]: "known"}


def test_auto_merge_trust_is_unchanged(db):
    """person_identity.trusted_address keeps its own rule (inbound volume counts there)."""
    for n in range(person_identity.TRUSTED_MIN_EMAILS):
        f.email(db, subject=f"Note {n}", sender=STRANGER, to=(OWNER,))
    sam = db.execute("SELECT id FROM people WHERE email = ?", (STRANGER[1],)).fetchone()[0]

    assert person_identity.trusted_address(db, sam)
    assert sender_trust.of_addresses(db, [STRANGER[1]]) == {STRANGER[1]: "external"}


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


def test_news_is_external_and_documents_are_unknown(db):
    """A document (an upload, a meeting transcript) is stored by the owner, not his words."""
    news = _decision_from(
        db, email_id=f.email(db, sender=COLLEAGUE, source_class="news", mailbox_name="News")
    )
    document = _decision_from(db, email_id=f.email(db, sender=STRANGER, source_class="document"))

    assert _labels(db, {news, document}) == {news: "external", document: "unknown"}


def test_a_teams_thread_is_internal_unless_a_sender_resolves_outside(db):
    colleague_only = f.teams_thread(db)
    f.teams_message(db, thread_id=colleague_only, sender_person_id=f.person(db, *COLLEAGUE))
    f.teams_message(db, thread_id=colleague_only, sender_mri="8:orgid:unresolved")
    with_guest = f.teams_thread(db)
    f.teams_message(db, thread_id=with_guest, sender_person_id=f.person(db, *COLLEAGUE))
    f.teams_message(db, thread_id=with_guest, sender_person_id=f.person(db, *STRANGER))
    by_mri = f.teams_thread(db)
    db.execute(
        "INSERT INTO teams_mri_resolution (mri, email, status) VALUES (?, ?, 'resolved')",
        ("8:orgid:guest", REGULAR[1]),
    )
    f.teams_message(db, thread_id=by_mri, sender_mri="8:orgid:guest")
    ids = {
        "internal": _decision_from(db, teams_thread_id=colleague_only),
        "external": _decision_from(db, teams_thread_id=with_guest),
        "by_mri": _decision_from(db, teams_thread_id=by_mri),
    }

    labels = _labels(db, set(ids.values()))

    assert labels[ids["internal"]] == "internal"
    assert labels[ids["external"]] == "external"
    assert labels[ids["by_mri"]] == "external"  # a known outsider is still not a colleague


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
    assert "stated sender, not a verified one" in mcp_server._INSTRUCTIONS


def test_the_module_says_the_sender_is_not_verified():
    assert "STATED sender, not a verified one" in (sender_trust.__doc__ or "")
