"""Every search or list row says what it stands for: kind, mailbox, who, ref and its dates.

Rows came back in the shape of the table they were read from. A search_emails row
was email_id, date, subject, summary, snippet and `source`, and `source` named the
field the words matched in, so a news article, a standalone document and an internal
email looked alike, and on the replica most "email" hits were news and documents.
Nothing said which mailbox a row sat in or who sent it: the model spent 263 sql_query
calls fetching senders. Teams rows were dated `started_at`, calendar rows
`start_at`, and none said which id the tool that reads the item takes.
"""

import numpy as np
import pytest

from src import mcp_server
from src.store.schema import get_connection
from tests import seeded_store

ENVELOPE = {"kind", "who", "ref", "date"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def _by(rows, key, value):
    return next(row for row in rows if row.get(key) == value)


# ------------------------------------------------------------------- email rows


def test_an_email_row_says_its_kind_mailbox_sender_and_ref(store):
    row = _by(mcp_server.search_emails("okapi plan")["result"], "email_id", 2)

    assert row["kind"] == "email"
    assert row["mailbox"] == "Sent Items"
    assert row["who"] == "Bob Jones <bob@example.com>"
    assert row["ref"] == row["email_id"] == 2
    assert "ingested_at" not in row  # the store keeps no ingest time for mail


def test_news_and_documents_say_what_they_are(store):
    rows = mcp_server.search_emails("okapi", include_news=True)["result"]

    news, document = _by(rows, "email_id", 3), _by(rows, "email_id", 4)
    assert (news["kind"], news["mailbox"]) == ("news", "News")
    assert news["who"] == seeded_store.NEWS_DESK
    assert (document["kind"], document["mailbox"]) == ("document", "External")
    assert document["who"] == "documents@example.com"


def test_matched_in_names_the_field_the_words_matched_and_source_stays(store):
    """`source` is kept for one release beside `matched_in`, which says the same."""
    for row in mcp_server.search_emails("okapi", include_news=True)["result"]:
        assert row["matched_in"] == row["source"]
        assert row["matched_in"] in {"subject", "summary", "content", "mixed", "key_fact"}


def test_the_thread_and_the_filters_list_rows_with_the_envelope(store):
    thread = mcp_server.email_thread(email_id=1)["emails"]
    listed = mcp_server.query_emails(keyword="okapi")["result"]

    for row in thread + listed:
        assert ENVELOPE <= set(row), row
        assert row["ref"] == row["email_id"]
        assert "matched_in" not in row  # listed by filter, not matched
    assert _by(thread, "email_id", 1)["who"] == seeded_store.ALICE


# -------------------------------------------------------------- other kinds


def test_an_attachment_row_names_its_email_and_when_it_was_exported(store):
    row = mcp_server.search_attachments("okapi")["result"][0]

    assert row["kind"] == "attachment"
    assert row["ref"] == row["attachment_id"] == 1
    assert row["mailbox"] == "Inbox"
    assert row["who"] == seeded_store.ALICE
    assert row["date"] == row["email_date"]
    assert row["ingested_at"] == seeded_store.EXPORTED_AT


def test_a_teams_row_names_who_wrote_and_the_thread_to_read(store):
    row = mcp_server.search_teams("okapi", kind="thread")["results"][0]

    assert (row["kind"], row["ref"], row["date"]) == ("teams", 1, row["started_at"])
    # The thread matched: who started it.
    assert row["who"] == "Bob Jones"
    assert row["matched_in"] == row["match"] == "thread"
    assert "mailbox" not in row


def test_a_teams_message_match_names_its_sender(store):
    row = mcp_server.search_teams("numbers", kind="message")["results"][0]

    assert row["who"] == row["matched_sender"] == "Alice Smith"
    assert row["matched_in"] == "message"


def test_a_whatsapp_row_names_who_wrote(store):
    session = mcp_server.search_whatsapp("visit")["results"][0]
    message = mcp_server.search_whatsapp("tickets")["results"][0]

    assert (session["kind"], session["ref"]) == ("whatsapp", 1)
    assert session["who"] == "Carol Example"
    assert message["who"] == "Dan Example"
    assert message["matched_in"] == "message"


def test_a_calendar_row_names_its_organiser_and_when_it_was_ingested(store):
    row = _by(mcp_server.query_calendar_events(keyword="okapi")["events"], "id", 1)

    assert (row["kind"], row["ref"], row["date"]) == ("calendar", 1, row["start_at"])
    assert row["who"] == seeded_store.ORGANISER
    assert row["ingested_at"] == seeded_store.EVENT_INGESTED_AT


def test_a_conversation_row_gives_the_session_id_conversation_context_takes(store):
    row = mcp_server.search_conversations("okapi")["result"][0]

    assert (row["kind"], row["ref"]) == ("conversation", "s1")
    assert row["ingested_at"] == seeded_store.CONVERSATION_CREATED_AT
    assert row["matched_in"] == row["match_type"]
    assert row["who"] is None


def test_an_agent_session_keeps_the_kind_it_already_had(store):
    """Since #193 a conversation row says 'owner' or 'agent' as its kind when agent
    sessions are included; that contract stays."""
    row = mcp_server.search_conversations("okapi", include_agents=True)["result"][0]

    assert row["kind"] == "owner"
    assert row["ref"] == "s1"


def test_an_image_row_names_the_email_it_first_appeared_in(store):
    row = mcp_server.attachment_image_search("okapi")["results"][0]

    assert (row["kind"], row["ref"]) == ("image", "ab12")
    assert row["who"] == seeded_store.ALICE
    assert row["date"]


# ------------------------------------------------- what was extracted from items


def test_a_decision_row_says_where_it_was_taken(store):
    rows = mcp_server.query_decisions()["result"]

    by_email = _by(rows, "decision", "Adopt the okapi plan")
    by_teams = _by(rows, "decision", "Pilot the okapi tracker")
    assert (by_email["kind"], by_email["ref"], by_email["who"]) == ("email", 1, seeded_store.ALICE)
    assert by_email["mailbox"] == "Inbox"
    assert (by_teams["kind"], by_teams["ref"], by_teams["who"]) == ("teams", 1, "Bob Jones")
    # Its own date stays; `source` keeps the parent's kind it always gave.
    assert by_teams["source"] == "teams"
    assert "matched_in" not in by_email


def test_action_rows_say_where_they_were_given(store):
    rows = mcp_server.query_actions()["result"]
    overdue = mcp_server.stale_threads()["overdue_actions"]

    assert {(r["task"], r["kind"], r["ref"]) for r in rows} == {
        ("Draft the okapi budget", "email", 1),
        ("Book the okapi room", "teams", 1),
    }
    assert [(r["kind"], r["ref"]) for r in overdue] == [("teams", 1)]


def test_every_recall_row_carries_the_envelope(store):
    out = mcp_server.recall("okapi", include_context=True)

    for bucket in mcp_server._RECALL_KINDS:
        assert out[bucket], bucket
        for row in out[bucket]:
            assert ENVELOPE <= set(row), (bucket, row)
    assert out["commitments"][0]["date"]  # a commitment had no date at all
    assert _by(out["decisions"], "decision", "Pilot the okapi tracker")["kind"] == "teams"
    for key in ("recent_emails", "decisions", "open_actions"):
        assert out["topic_context"][key], key
        for row in out["topic_context"][key]:
            assert ENVELOPE <= set(row), (key, row)


def test_dossier_rows_carry_the_envelope(store):
    person = mcp_server.person_context("alice@example.com")
    topic = mcp_server.topic_context("okapi")
    prep = mcp_server.meeting_prep("alice@example.com", topic="okapi")

    attendee = prep["attendees"][0]
    lists = [
        person["recent_emails"],
        person["decisions"],
        person["open_actions"],
        topic["recent_emails"],
        topic["key_facts"],
        attendee["emails"],
        attendee["decisions"],
        prep["topic_context"]["key_facts"],
        prep["meetings"]["last"],
        prep["meetings"]["next"],
    ]
    for rows in lists:
        assert rows
        for row in rows:
            assert ENVELOPE <= set(row), row
    assert prep["meetings"]["next"][0]["kind"] == "calendar"
    assert prep["meetings"]["next"][0]["who"] == seeded_store.ALICE


def test_a_persons_teams_and_whatsapp_threads_carry_the_envelope(store):
    person = mcp_server.person_context("alice@example.com")

    teams = person["teams"]["recent_threads"][0]
    whatsapp = person["whatsapp"]["recent_threads"][0]
    assert (teams["kind"], teams["ref"]) == ("teams", 1)
    assert (whatsapp["kind"], whatsapp["ref"]) == ("whatsapp", 1)


def test_a_chats_threads_carry_the_envelope(store):
    thread = mcp_server.teams_chat_summary(chat_id=1, days=3650)["threads"][0]

    assert (thread["kind"], thread["ref"], thread["who"]) == ("teams", 1, "Bob Jones")


# ----------------------------------------------------------------- semantic rows


def test_semantic_rows_of_every_kind_carry_the_envelope(store, monkeypatch):
    import src.store.embeddings as embeddings

    conn = get_connection(str(store))
    content_id = conn.execute("SELECT id FROM attachment_content").fetchone()[0]
    conn.close()
    index = store.parent / "emb.npz"
    ids = [
        1,
        -content_id,
        embeddings.TEAMS_THREAD_ID_OFFSET - 1,
        embeddings.WHATSAPP_THREAD_ID_OFFSET - 1,
        embeddings.CONVERSATION_ID_OFFSET - 1,
    ]
    np.savez(
        str(index),
        ids=np.array(ids, dtype=np.int64),
        vectors=np.ones((len(ids), 3), dtype=np.float32),
    )
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", index)
    monkeypatch.setattr(embeddings, "embed_query", lambda texts: np.ones((1, 3), np.float32))

    rows = mcp_server.search_emails("okapi", search_type="semantic")["result"]

    kinds = {row["type"]: row for row in rows}
    assert set(kinds) == {"email", "attachment", "teams_thread", "whatsapp_thread", "conversation"}
    expected = {
        "email": ("email", 1),
        "attachment": ("attachment", 1),
        "teams_thread": ("teams", 1),
        "whatsapp_thread": ("whatsapp", 1),
        "conversation": ("conversation", "s1"),
    }
    for type_, (kind, ref) in expected.items():
        assert (kinds[type_]["kind"], kinds[type_]["ref"]) == (kind, ref), type_
        assert kinds[type_]["matched_in"] == "semantic"
