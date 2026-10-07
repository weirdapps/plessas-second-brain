"""An email loaded as a stub can be extracted again, and the stub replaced.

After EMAIL_MAX_ATTEMPTS runs in which its extraction failed, an email is loaded
without one (src/extract/local.py "GAVE UP"): an empty summary and nothing else,
and nothing ever offered it to the model again. On 2026-10-07 the producer held
33 such emails, some from a day the cloud provider refused every request.
"""

import json
import sys

import pytest

from src.extract import local, stub_retry
from src.extract.extraction_files import extraction_path
from src.store.loader import load_single_email
from src.store.schema import create_database, get_connection

STUB = {
    "message_id": "AAMk-stub",
    "internet_message_id": "<stub@example.com>",
    "date_received": "2026-10-06T08:00:00Z",
    "subject": "Quarterly figures",
    "sender": {"name": "Alex Doe", "address": "alex@example.com"},
    "to_recipients": [{"name": "Sam Roe", "address": "sam@example.com"}],
    "cc_recipients": [],
    "mailbox_name": "Archive",
    "content": "<p>The figures are attached.</p>",
}
ANSWER = {
    "summary": "Alex sends the quarterly figures.",
    "topics": ["quarterly figures"],
    "decisions": [{"decision": "Publish on Friday"}],
    "action_items": [{"task": "Review the figures", "owner": "Sam Roe"}],
    "commitments": [],
    "people_roles": {"Alex Doe": ["sender"], "Pat Moe": ["approver"]},
    "sentiment": "neutral",
    "urgency": "medium",
    "language": "english",
    "key_facts": ["Figures are final"],
}


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    assert load_single_email(conn, STUB, local._stub_extraction("AAMk-stub"))
    assert load_single_email(
        conn,
        {**STUB, "message_id": "AAMk-fine", "internet_message_id": "<fine@example.com>"},
        {**ANSWER, "summary": "already extracted"},
    )
    for message_id, mailbox in (("news:1", "News"), ("-5", "External")):
        conn.execute(
            "INSERT INTO emails (message_id, date_received, subject, summary, mailbox_name)"
            " VALUES (?, '2026-10-06', 's', '', ?)",
            (message_id, mailbox),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def model(monkeypatch):
    """The model as extract_inline reaches it: answers ANSWER, or what .outcomes says."""
    seen: list[dict] = []
    outcomes: dict[str, tuple] = {}

    def extract(email, api_key, engine="claude"):
        seen.append(email)
        if email["message_id"] in outcomes:
            return outcomes[email["message_id"]]
        return email["message_id"], {**ANSWER, "message_id": email["message_id"]}, False, None

    monkeypatch.setattr(local, "extract_inline", extract)
    extract.seen = seen
    extract.outcomes = outcomes
    return extract


def _row(conn, message_id):
    return conn.execute(
        "SELECT id, summary, sentiment, urgency FROM emails WHERE message_id = ?", (message_id,)
    ).fetchone()


def test_only_mail_with_an_empty_summary_is_a_stub(store):
    conn = get_connection(str(store))
    assert [m for _, m in stub_retry.select_stubs(conn)] == ["AAMk-stub"]
    assert stub_retry.select_stubs(conn, since="2026-10-07") == []
    conn.close()


def test_a_stub_gets_its_extraction_in_place(store, model, tmp_path):
    conn = get_connection(str(store))
    email_id = _row(conn, "AAMk-stub")[0]

    stats = stub_retry.retry_stubs(conn, tmp_path / "extracted", engine="claude")

    assert stats == {"stubs": 1, "replaced": 1, "failed": 0, "stopped": False}
    row = _row(conn, "AAMk-stub")
    assert tuple(row) == (email_id, ANSWER["summary"], "neutral", "medium"), "same row, same id"
    assert [
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM email_topics et JOIN topics t ON t.id = et.topic_id"
            " WHERE et.email_id = ?",
            (email_id,),
        )
    ] == ["quarterly figures"]
    assert (
        conn.execute("SELECT count(*) FROM decisions WHERE email_id = ?", (email_id,)).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) FROM key_facts WHERE email_id = ?", (email_id,)).fetchone()[0]
        == 1
    )
    roles = {
        r[0]
        for r in conn.execute(
            "SELECT role_in_email FROM email_people WHERE email_id = ?", (email_id,)
        )
    }
    assert {"sender", "recipient", "approver"} <= roles
    # The model read the stored email: its body and its recipients.
    asked = model.seen[0]
    assert "figures are attached" in asked["content"]
    assert asked["to_recipients"] == [{"name": "Sam Roe", "address": "sam@example.com"}]
    written = json.loads(extraction_path(tmp_path / "extracted", "AAMk-stub").read_text())
    assert written["summary"] == ANSWER["summary"]
    conn.close()


def test_a_failure_leaves_the_stub_and_the_run_goes_on(store, model, tmp_path):
    conn = get_connection(str(store))
    conn.execute("UPDATE emails SET summary = '' WHERE message_id = 'AAMk-fine'")  # a second stub
    conn.commit()
    model.outcomes["AAMk-stub"] = ("AAMk-stub", None, False, local.FAULT)

    stats = stub_retry.retry_stubs(conn, tmp_path / "extracted", engine="claude")

    assert stats == {"stubs": 2, "replaced": 1, "failed": 1, "stopped": False}
    assert _row(conn, "AAMk-stub")[1] == ""
    assert _row(conn, "AAMk-fine")[1] == ANSWER["summary"]
    conn.close()


def test_quota_stops_the_run(store, model, tmp_path):
    conn = get_connection(str(store))
    conn.execute("UPDATE emails SET summary = '' WHERE message_id = 'AAMk-fine'")
    conn.commit()
    model.outcomes["AAMk-fine"] = ("AAMk-fine", None, True, None)
    model.outcomes["AAMk-stub"] = ("AAMk-stub", None, True, None)

    stats = stub_retry.retry_stubs(conn, tmp_path / "extracted", engine="claude")

    assert stats["stopped"] is True
    assert len(model.seen) == 1
    conn.close()


def test_the_cli_retries_stubs_on_the_producer(store, model, monkeypatch, capsys):
    from src import cli

    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(store), "retry-stubs", "--dry-run"])
    cli.main()
    assert "1 stub" in capsys.readouterr().out
    assert model.seen == [], "a dry run asks the model nothing"

    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(store), "retry-stubs"])
    cli.main()
    conn = get_connection(str(store))
    assert _row(conn, "AAMk-stub")[1] == ANSWER["summary"]
    conn.close()
