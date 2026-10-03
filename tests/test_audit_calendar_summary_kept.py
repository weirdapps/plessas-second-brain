"""Only a new extraction replaces an event's summary, as only it replaces the decisions."""

import pytest

from src.store.calendar_loader import load_event
from src.store.schema import create_database

_EVENT = {
    "outlook_event_id": "evt-kept",
    "subject": "Quarterly review",
    "organizer_email": "chair@example.com",
    "start_at": "2026-08-12T09:00:00.0000000Z",
    "end_at": "2026-08-12T10:00:00.0000000Z",
    "attendees": [],
}
_GOOD = {
    "body_summary": "Reviewed the quarter",
    "decisions": [{"decision": "Hire two analysts"}],
    "action_items": [],
}
_EMPTY = {"body_summary": "", "decisions": [], "action_items": []}


@pytest.fixture
def conn(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _row(conn):
    return conn.execute(
        "SELECT body_summary, body_extracted_at, llm_status, "
        "(SELECT COUNT(*) FROM decisions WHERE event_id = calendar_events.id) AS decisions "
        "FROM calendar_events WHERE outlook_event_id = 'evt-kept'"
    ).fetchone()


@pytest.mark.parametrize("status", ["failed", "pending", "skipped"])
def test_a_non_extraction_keeps_the_summary_it_found(conn, status):
    """schema-load-2 / teams-calendar-8: a re-extraction that failed wrote '' over
    a good summary while the decisions from the same extraction were kept, and a
    'failed' row is not offered again until the event is next edited.

    'skipped' belongs with them: it is the fetched body now being too short to
    summarise, which says nothing new about the meeting the kept decisions and
    the kept summary came from."""
    load_event(conn, _EVENT, _GOOD, llm_status="extracted")
    stamped = _row(conn)["body_extracted_at"]

    load_event(conn, {**_EVENT, "change_key": "k2"}, _EMPTY, llm_status=status)

    row = _row(conn)
    assert row["body_summary"] == "Reviewed the quarter"
    assert row["body_extracted_at"] == stamped
    assert row["llm_status"] == status
    assert row["decisions"] == 1


def test_keeping_the_extraction_refreshes_the_facts_only(conn):
    """An etag change with the same prompt: the facts move, the extraction stays."""
    load_event(conn, _EVENT, _GOOD, llm_status="extracted", extraction_hash="h1")
    stamped = _row(conn)["body_extracted_at"]

    load_event(
        conn,
        {**_EVENT, "change_key": "k2", "response_status": "accepted"},
        _EMPTY,
        llm_status="extracted",
        extraction_hash="h1",
        keep_extraction=True,
    )

    row = _row(conn)
    assert row["body_summary"] == "Reviewed the quarter"
    assert row["body_extracted_at"] == stamped
    assert row["llm_status"] == "extracted"
    assert row["decisions"] == 1
    facts = conn.execute(
        "SELECT change_key, response_status, extraction_hash FROM calendar_events "
        "WHERE outlook_event_id = 'evt-kept'"
    ).fetchone()
    assert (facts["change_key"], facts["response_status"], facts["extraction_hash"]) == (
        "k2",
        "accepted",
        "h1",
    )


def test_a_new_extraction_still_replaces_the_summary(conn):
    load_event(conn, _EVENT, _GOOD, llm_status="extracted")

    load_event(
        conn,
        _EVENT,
        {"body_summary": "Moved to Friday", "decisions": [], "action_items": []},
        llm_status="extracted",
    )

    row = _row(conn)
    assert (row["body_summary"], row["decisions"]) == ("Moved to Friday", 0)


def test_a_first_load_without_an_extraction_has_no_summary(conn):
    load_event(conn, _EVENT, _EMPTY, llm_status="pending")

    row = _row(conn)
    assert (row["body_summary"], row["body_extracted_at"]) == ("", None)
