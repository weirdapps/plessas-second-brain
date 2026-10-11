"""tests/factories.py: the rows a test needs, in one call, through the real schema."""

import sqlite3

import pytest

from src.store.schema import create_database, subject_to_conversation_id
from tests import factories as f


@pytest.fixture
def conn(tmp_path):
    connection = create_database(str(tmp_path / "brain.db"))
    yield connection
    connection.close()


def _roles(conn, email_id):
    return sorted(
        (role, address)
        for role, address in conn.execute(
            "SELECT ep.role_in_email, p.email FROM email_people ep"
            " JOIN people p ON p.id = ep.person_id WHERE ep.email_id = ?",
            (email_id,),
        )
    )


def test_an_email_comes_with_its_sender_and_recipients(conn):
    email_id = f.email(
        conn,
        sender=("Alice Smith", "alice@example.com"),
        to=[("Bob Jones", "bob@example.com")],
        cc=[("Carol White", "carol@example.com")],
    )

    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    assert (row["sender_name"], row["sender_address"]) == ("Alice Smith", "alice@example.com")
    assert _roles(conn, email_id) == [
        ("cc", "carol@example.com"),
        ("recipient", "bob@example.com"),
        ("sender", "alice@example.com"),
    ]


def test_a_reply_joins_the_thread_of_the_mail_it_answers(conn):
    first = f.email(conn, subject="Okapi budget")
    reply = f.email(conn, subject="Re: Okapi budget")
    other = f.email(conn, subject="Something else")

    threads = dict(conn.execute("SELECT id, conversation_id FROM emails"))
    assert threads[first] == threads[reply] == subject_to_conversation_id("Okapi budget")
    assert threads[other] != threads[first]


def test_a_person_is_one_row_however_many_mails_name_them(conn):
    """Found by address, case aside, as the loader finds them."""
    f.email(conn, sender=("Alice Smith", "alice@example.com"))
    f.email(conn, sender=("Alice Smith", "Alice@Example.com"), to=[])

    assert f.person(conn, "A. Smith", "ALICE@example.com") == f.person(conn)
    count = conn.execute("SELECT COUNT(*) FROM people WHERE lower(email) = 'alice@example.com'")
    assert count.fetchone()[0] == 1


def test_every_email_gets_a_message_id_of_its_own(conn):
    ids = {f.email(conn) for _ in range(3)}

    message_ids = [r[0] for r in conn.execute("SELECT message_id FROM emails")]
    assert len(ids) == 3
    assert len(set(message_ids)) == 3


def test_keyword_overrides_reach_the_row(conn):
    email_id = f.email(conn, mailbox_name="Archive", summary="the plan", source_class="news")

    row = conn.execute(
        "SELECT mailbox_name, summary, source_class FROM emails WHERE id = ?", (email_id,)
    ).fetchone()
    assert tuple(row) == ("Archive", "the plan", "news")


def test_a_column_the_table_does_not_have_is_refused(conn):
    """An override that names no column must fail, not be dropped: a test that sets a
    renamed column would otherwise go on passing without the value it meant to set."""
    with pytest.raises(sqlite3.OperationalError):
        f.email(conn, mailbox="Archive")


def test_topics_are_linked_to_the_email(conn):
    email_id = f.email(conn, topics=["okapi", "budget"])

    names = sorted(
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM email_topics et JOIN topics t ON t.id = et.topic_id"
            " WHERE et.email_id = ?",
            (email_id,),
        )
    )
    assert names == ["budget", "okapi"]


def test_an_email_is_found_by_its_full_text_index(conn):
    email_id = f.email(conn, subject="Okapi plan", content="the okapi figures")

    hits = [
        r[0] for r in conn.execute("SELECT rowid FROM emails_fts WHERE emails_fts MATCH 'okapi'")
    ]
    assert hits == [email_id]


def test_an_attachment_carries_its_extracted_content(conn):
    email_id = f.email(conn)
    attachment_id = f.attachment(conn, email_id=email_id, text="the okapi figures")

    row = conn.execute(
        "SELECT a.email_id, a.message_id, ac.extracted_text, ac.extraction_status"
        " FROM attachments a JOIN attachment_content ac ON ac.attachment_id = a.id"
        " WHERE a.id = ?",
        (attachment_id,),
    ).fetchone()
    message_id = conn.execute("SELECT message_id FROM emails WHERE id = ?", (email_id,)).fetchone()[
        0
    ]
    assert tuple(row) == (email_id, message_id, "the okapi figures", "extracted")
    assert conn.execute(
        "SELECT rowid FROM attachment_content_fts WHERE attachment_content_fts MATCH 'okapi'"
    ).fetchall()


def test_an_attachment_without_an_email_gets_one(conn):
    attachment_id = f.attachment(conn)

    assert conn.execute(
        "SELECT e.id FROM attachments a JOIN emails e ON e.id = a.email_id WHERE a.id = ?",
        (attachment_id,),
    ).fetchone()


def test_an_attachment_can_be_registered_with_no_content_yet(conn):
    attachment_id = f.attachment(conn, text=None)

    assert not conn.execute(
        "SELECT 1 FROM attachment_content WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()


def test_a_summary_marks_the_content_summarised(conn):
    attachment_id = f.attachment(conn, summary="Okapi figures")

    assert (
        conn.execute(
            "SELECT llm_status FROM attachment_content WHERE attachment_id = ?", (attachment_id,)
        ).fetchone()[0]
        == "extracted"
    )
    assert (
        conn.execute(
            "SELECT llm_status FROM attachment_content WHERE attachment_id = ?",
            (f.attachment(conn),),
        ).fetchone()[0]
        == "pending"
    )


def test_a_teams_message_comes_with_its_chat_and_thread(conn):
    message_id = f.teams_message(conn, content_text="the okapi numbers")

    row = conn.execute(
        "SELECT m.chat_id, m.thread_id, t.chat_id AS thread_chat FROM teams_messages m"
        " JOIN teams_threads t ON t.id = m.thread_id WHERE m.id = ?",
        (message_id,),
    ).fetchone()
    assert row["chat_id"] == row["thread_chat"]
    assert conn.execute(
        "SELECT rowid FROM teams_messages_fts WHERE teams_messages_fts MATCH 'okapi'"
    ).fetchall()


def test_messages_given_a_thread_land_in_it_and_in_its_chat(conn):
    thread_id = f.teams_thread(conn, title="Okapi thread")
    first = f.teams_message(conn, thread_id=thread_id)
    second = f.teams_message(conn, thread_id=thread_id)

    rows = conn.execute(
        "SELECT DISTINCT chat_id, thread_id FROM teams_messages WHERE id IN (?, ?)",
        (first, second),
    ).fetchall()
    chat_id = conn.execute(
        "SELECT chat_id FROM teams_threads WHERE id = ?", (thread_id,)
    ).fetchone()[0]
    assert [tuple(r) for r in rows] == [(chat_id, thread_id)]


def test_a_calendar_event_lists_its_organiser_and_attendees(conn):
    event_id = f.calendar_event(
        conn,
        subject="Okapi review",
        organizer=("Olga Organiser", "olga@example.com"),
        attendees=[("Alice Smith", "alice@example.com")],
    )

    rows = sorted(
        tuple(r)
        for r in conn.execute(
            "SELECT email, is_organizer FROM event_attendees WHERE event_id = ?", (event_id,)
        )
    )
    assert rows == [("alice@example.com", 0), ("olga@example.com", 1)]
    row = conn.execute(
        "SELECT organizer_email, start_at < end_at FROM calendar_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert tuple(row) == ("olga@example.com", 1)


def test_decisions_and_actions_come_from_an_email_unless_told_otherwise(conn):
    decision_id = f.decision(conn, "Adopt the okapi plan")
    action_id = f.action_item(conn, "Draft the okapi budget")

    for table, row_id in (("decisions", decision_id), ("action_items", action_id)):
        email_id = conn.execute(f"SELECT email_id FROM {table} WHERE id = ?", (row_id,)).fetchone()[
            0
        ]
        assert conn.execute("SELECT 1 FROM emails WHERE id = ?", (email_id,)).fetchone(), table


def test_a_decision_from_a_teams_thread_has_no_email(conn):
    thread_id = f.teams_thread(conn)

    decision_id = f.decision(conn, "Pilot the tracker", teams_thread_id=thread_id)
    action_id = f.action_item(conn, "Book the room", teams_thread_id=thread_id, status="expired")

    decision = conn.execute(
        "SELECT email_id, teams_thread_id FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    action = conn.execute(
        "SELECT email_id, status FROM action_items WHERE id = ?", (action_id,)
    ).fetchone()
    assert tuple(decision) == (None, thread_id)
    assert tuple(action) == (None, "expired")
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0


def test_recall_finds_a_row_of_every_kind_written_with_the_defaults(tmp_path, monkeypatch):
    """The defaults are rows the tools read, not merely rows the schema accepts."""
    from src import mcp_server
    from tests import seeded_store

    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    email_id = f.email(conn, subject="Okapi plan", content="the okapi figures")
    f.attachment(conn, email_id=email_id, text="the okapi budget", summary="Okapi budget")
    f.teams_thread(conn, title="Okapi thread", summary="The okapi thread")
    f.calendar_event(conn, subject="Okapi review", body_summary="the okapi review")
    f.decision(conn, "Adopt the okapi plan", email_id=email_id)
    f.action_item(conn, "Draft the okapi budget", email_id=email_id)
    conn.close()
    seeded_store.serve(path, monkeypatch)

    result = mcp_server.recall(query="okapi")

    kinds = ("emails", "attachments", "teams", "calendar_events", "decisions", "actions")
    assert [kind for kind in kinds if not result[kind]] == []


def test_ago_is_an_iso_utc_timestamp_in_the_past():
    stamp = f.ago(days=2)

    assert stamp.endswith("Z") and "T" in stamp
    assert stamp < f.ago()
