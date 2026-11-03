"""The sync's extraction run gives each new email its thread's stored state (issue 164).

Every value is invented.
"""

import pytest

from src.extract import local
from src.store.schema import create_database
from tests import factories as f

OKAPI = "conv-okapi"


@pytest.fixture
def run(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )
    seen: dict[str, dict] = {}

    def extract(email, api_key, engine="claude"):
        seen[email["message_id"]] = email
        return email["message_id"], {"summary": "s"}, False, None

    monkeypatch.setattr(local, "extract_inline", extract)

    def go(emails, **kwargs):
        monkeypatch.setattr(local, "collect_emails", lambda: emails)
        local.run_extraction(workers=1, deadline_s=600.0, **kwargs)
        return seen

    return go


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "b.db"
    conn = create_database(str(path))
    earlier = f.email(
        conn, subject="Okapi", conversation_id=OKAPI, date_received="2026-10-01T09:00:00Z"
    )
    f.decision(conn, "Launch in April", email_id=earlier, status="proposed")
    conn.close()
    return path


def _mail(message_id, conversation, day):
    return {
        "message_id": message_id,
        "conversation_id": conversation,
        "date_received": f"2026-10-0{day}T09:00:00Z",
        "subject": "RE: Okapi",
        "content": "ok",
        "mailbox_name": "Inbox",
    }


def test_each_pending_email_carries_the_state_of_its_thread(run, db):
    seen = run(
        [_mail("reply", "conv-okapi", 5), _mail("fresh", "conv-new", 5)],
        db_path=db,
    )

    state = seen["reply"]["thread_state"]
    assert state["earlier_messages"] == 1
    assert [i["handle"] for i in state["items"]] == ["D1"]
    assert "thread_state" not in seen["fresh"]


def test_an_earlier_message_staged_in_the_same_run_counts(run, db):
    seen = run([_mail("first", "conv-new", 3), _mail("second", "conv-new", 5)], db_path=db)

    assert "thread_state" not in seen["first"]
    assert seen["second"]["thread_state"]["earlier_messages"] == 1


def test_without_a_store_no_state_is_given(run):
    seen = run([_mail("reply", "conv-okapi", 5)])

    assert "thread_state" not in seen["reply"]
