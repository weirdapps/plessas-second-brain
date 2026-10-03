"""An etag change that leaves the model's input alone costs no extraction.

The change detector keys on the event's etag, and Outlook moves the etag on every
edit, including ones the extraction never sees: an attendee answering, a room
change. In the week to 2026-10-03 that meant 1,687 extractions for a window of
about 250 events, most of them re-reading an identical prompt. The prompt is now
hashed and stored with the extraction; an event whose new etag comes with the
same prompt keeps the extraction it has, while its facts are still refreshed.
"""

import sqlite3
import types
from datetime import datetime, timedelta

from src.extract.calendar_extractor import event_prompt_hash
from tests.test_audit_calendar_listing import _FMT, _span, _utc

_BODY = "Agenda: review the Q3 numbers, agree the budget, and assign the follow-ups. " * 2
_EXTRACTION = {
    "body_summary": "Reviewed the quarter",
    "decisions": [{"decision": "Hire two analysts"}],
    "action_items": [],
}


def _event(etag: str, body: str = _BODY, response: str = "none") -> dict:
    start = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    return {
        "Id": "evt-1",
        "Subject": "Quarterly review",
        "Start": {"DateTime": start.strftime(_FMT), "TimeZone": "UTC"},
        "End": {"DateTime": (start + timedelta(hours=1)).strftime(_FMT), "TimeZone": "UTC"},
        "Organizer": {"EmailAddress": {"Name": "Chair", "Address": "chair@example.com"}},
        "Attendees": [{"EmailAddress": {"Name": "Ana", "Address": "ana@example.com"}}],
        "ResponseStatus": {"Response": response},
        "@odata.etag": etag,
        "Body": {"Content": body},
    }


def _sync(monkeypatch, tmp_path, event: dict, calls: list) -> int:
    from src import cli
    from src.export import calendar_export
    from src.store.schema import create_database

    db_path = str(tmp_path / "brain.db")
    if not (tmp_path / "brain.db").exists():
        create_database(db_path).close()

    def run(args):
        if args[0] == "get-event":
            return event
        start, end = _utc(args[args.index("--from") + 1]), _utc(args[args.index("--to") + 1])
        listed = {k: v for k, v in event.items() if k != "Body"}
        s, e = _span(event)
        return [listed] if s < end and e > start else []

    def fake_extract(ev, body):
        calls.append(ev["outlook_event_id"])
        return _EXTRACTION

    monkeypatch.setattr(calendar_export, "run_outlook_cli", run)
    monkeypatch.setattr("src.extract.calendar_extractor.extract_event", fake_extract)
    args = types.SimpleNamespace(
        db=db_path, backfill=False, since=None, until=None, skip_extraction=False
    )
    return cli.cmd_calendar_sync(args)


def _row(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT body_summary, llm_status, change_key, response_status, extraction_hash, "
        "(SELECT COUNT(*) FROM decisions WHERE event_id = calendar_events.id) AS decisions "
        "FROM calendar_events WHERE outlook_event_id = 'evt-1'"
    ).fetchone()
    conn.close()
    return row


def test_the_hash_ignores_what_the_model_never_sees():
    event = {"subject": "Q3", "attendees": [{"name": "Ana"}], "start_at": "2026-10-05"}
    same = {**event, "response_status": "accepted", "location": "Room 2", "change_key": "k2"}
    assert event_prompt_hash(event, _BODY) == event_prompt_hash(same, _BODY)
    assert event_prompt_hash(event, _BODY) != event_prompt_hash(event, _BODY + " Also: hiring.")
    assert event_prompt_hash(event, "too short") is None


def test_an_etag_change_that_leaves_the_prompt_alone_costs_no_call(monkeypatch, tmp_path):
    calls: list = []
    _sync(monkeypatch, tmp_path, _event("e1"), calls)
    assert calls == ["evt-1"]

    _sync(monkeypatch, tmp_path, _event("e2", response="accepted"), calls)

    assert calls == ["evt-1"]  # no second extraction
    row = _row(tmp_path)
    assert row["llm_status"] == "extracted"
    assert row["body_summary"] == "Reviewed the quarter"
    assert row["decisions"] == 1
    assert row["change_key"] == "e2"  # the facts and the etag still move
    assert row["extraction_hash"]


def test_a_body_change_is_extracted_again(monkeypatch, tmp_path):
    calls: list = []
    _sync(monkeypatch, tmp_path, _event("e1"), calls)
    _sync(monkeypatch, tmp_path, _event("e2", body=_BODY + " New item: hiring plan."), calls)
    assert calls == ["evt-1", "evt-1"]
