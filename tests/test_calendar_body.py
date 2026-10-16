"""An event's text is stored as a reader sees it, in calendar_events.body.

The column was in the schema from the first release, and nothing ever wrote it: calendar-sync
fetched every event's text, read it into a summary or not, and threw it away, so all 3,701 events
on the producer had an empty body and an agent querying it found nothing (audit 2026-10-16). 79% of
events have no summary (an invitation with nothing but joining details, or too short to read), and
for those the agenda was nowhere. The body is now stored with the event's other facts: HTML
converted, the online-meeting joining details left out, secrets masked. `brain maint
calendar-bodies` fetches the text of the events stored before.
"""

import sqlite3
import types

import pytest

from src.extract.calendar_extractor import event_body_text
from src.store.calendar_loader import load_event
from src.store.schema import create_database
from tests.test_calendar_invites import _TEAMS_BLOCK
from tests.test_calendar_same_input_skip import _event, _sync

_AGENDA = "Agenda: the branch network plan, the card pricing, and the steering dates."


def _body(tmp_path, event_id="evt-1"):
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    row = conn.execute(
        "SELECT body FROM calendar_events WHERE outlook_event_id = ?", (event_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def test_the_body_is_the_text_a_reader_sees():
    html = f"<html><body><p>{_AGENDA}</p><pre>{_TEAMS_BLOCK}</pre></body></html>"

    text = event_body_text(html)

    assert _AGENDA in text
    assert "<p>" not in text and "meetup-join" not in text and "Passcode" not in text
    assert event_body_text(None) is None
    assert event_body_text("") == ""


def test_a_short_body_is_kept_though_it_is_not_summarised():
    assert event_body_text("Room 4, bring the slides") == "Room 4, bring the slides"


def test_a_synced_event_stores_its_body(monkeypatch, tmp_path):
    calls: list = []

    _sync(monkeypatch, tmp_path, _event("e1", body=f"<p>{_AGENDA}</p>\n{_TEAMS_BLOCK}"), calls)

    body = _body(tmp_path)
    assert body is not None and _AGENDA in body and "teams.microsoft.com" not in body


def test_an_event_whose_body_was_cleared_stores_an_empty_one(monkeypatch, tmp_path):
    _sync(monkeypatch, tmp_path, _event("e1", body=_AGENDA), [])
    _sync(monkeypatch, tmp_path, _event("e2", body=""), [])

    assert _body(tmp_path) == ""


def _stored(conn, body=None) -> None:
    event = {
        "outlook_event_id": "evt-9",
        "subject": "Steering",
        "start_at": "2026-10-20T08:00:00Z",
        "end_at": "2026-10-20T09:00:00Z",
        "attendees": [],
    }
    if body is not None:
        event["body"] = body
    load_event(
        conn,
        event,
        {"body_summary": "", "decisions": [], "action_items": []},
        user_email_pattern="",
        llm_status="skipped",
    )
    conn.commit()


def test_an_upsert_with_no_body_keeps_the_one_stored(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    _stored(conn, body=_AGENDA)
    _stored(conn)  # an event given without its text

    assert conn.execute("SELECT body FROM calendar_events").fetchone()[0] == _AGENDA
    conn.close()


# --- calendar-bodies: the events stored before --------------------------------------------------


@pytest.fixture
def stored(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    for n, cancelled in ((1, 0), (2, 0), (3, 1)):
        conn.execute(
            "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at,"
            " is_cancelled, ingested_at) VALUES (?, 'm', ?, ?, ?, '2026-10-01T00:00:00Z')",
            (f"evt-{n}", f"2026-10-0{n}T08:00:00Z", f"2026-10-0{n}T09:00:00Z", cancelled),
        )
    conn.commit()
    conn.close()
    return tmp_path / "brain.db"


def _fake_outlook(monkeypatch, answers):
    from src.export import calendar_export

    asked = []

    def run(args):
        asked.append(args[1])
        answer = answers.get(args[1])
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(calendar_export, "run_outlook_cli", run)
    return asked


def _run(path, **kw):
    from src.pipeline.calendar_bodies import run

    args = types.SimpleNamespace(**{"db": str(path), "limit": 0, "dry_run": False, **kw})
    return run(args)


def test_the_backfill_stores_the_text_of_events_stored_before(monkeypatch, stored, tmp_path):
    asked = _fake_outlook(
        monkeypatch,
        {"evt-1": {"Body": {"Content": f"<p>{_AGENDA}</p>"}}, "evt-2": None},
    )

    assert _run(stored) == 0

    assert sorted(asked) == ["evt-1", "evt-2"]  # never the cancelled one
    assert _AGENDA in _body(tmp_path, "evt-1")
    assert _body(tmp_path, "evt-2") is None  # a failed fetch is offered again next run


def test_the_backfill_dry_run_fetches_nothing(monkeypatch, stored, tmp_path, capsys):
    asked = _fake_outlook(monkeypatch, {})

    assert _run(stored, dry_run=True) == 0

    assert asked == [] and "2 event(s)" in capsys.readouterr().out


def test_the_backfill_stops_when_outlook_needs_a_login(monkeypatch, stored, tmp_path):
    from src.export.outlook_cli import OutlookCliAuthRequired

    _fake_outlook(monkeypatch, {"evt-2": OutlookCliAuthRequired("log in"), "evt-1": None})

    assert _run(stored) == 4


def test_the_backfill_runs_as_a_maint_command(monkeypatch, capsys, stored):
    from tests.brain_cli import brain

    _fake_outlook(monkeypatch, {"evt-1": {"Body": {"Content": _AGENDA}}, "evt-2": None})
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(stored), "maint", "calendar-bodies", "--limit", "5"
    )

    assert code == 0 and "1 stored" in out
