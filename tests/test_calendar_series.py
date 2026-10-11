"""Recurring occurrences carry their series, and a re-sync never stores one twice.

parse_event read `IsRecurring`, which neither list-calendar nor get-event sends,
so `is_recurring` was 0 on every stored row while `SeriesMasterId` arrived on
two thirds of them (data-coverage-05). get-event returns the whole event, whose
`Type` is SingleInstance, Occurrence, Exception or SeriesMaster.
"""

import sqlite3
import types
from datetime import datetime, timedelta

from src.export.calendar_export import parse_event

_FMT = "%Y-%m-%dT%H:%M:%S.0000000"


def _raw(**fields) -> dict:
    """One event as get-event returns it, a daily call by default."""
    start = datetime(2026, 9, 14, 6, 15)
    base = {
        "Id": "occ-1",
        "Subject": "Daily call",
        "Organizer": {"EmailAddress": {"Address": "chair@example.com", "Name": "Chair"}},
        "Start": {"DateTime": start.strftime(_FMT), "TimeZone": "UTC"},
        "End": {"DateTime": (start + timedelta(minutes=15)).strftime(_FMT), "TimeZone": "UTC"},
    }
    return {**base, **fields}


def test_an_occurrence_is_recurring_and_names_its_series():
    parsed = parse_event(_raw(Type="Occurrence", SeriesMasterId="master-1"))

    assert parsed["is_recurring"] is True
    assert parsed["recurrence_master_id"] == "master-1"


def test_an_occurrence_edited_on_its_own_stays_in_its_series():
    parsed = parse_event(_raw(Type="Exception", SeriesMasterId="master-1"))

    assert (parsed["is_recurring"], parsed["recurrence_master_id"]) == (True, "master-1")


def test_a_single_meeting_is_not_recurring():
    parsed = parse_event(_raw(Type="SingleInstance", SeriesMasterId=None))

    assert (parsed["is_recurring"], parsed["recurrence_master_id"]) == (False, None)


def test_a_series_master_is_its_own_series():
    parsed = parse_event(_raw(Id="master-1", Type="SeriesMaster"))

    assert (parsed["is_recurring"], parsed["recurrence_master_id"]) == (True, "master-1")


def test_an_occurrence_without_its_master_id_gets_a_derived_series_key():
    """When the adapter does not say which series, organiser and subject do."""
    first = parse_event(_raw(Id="occ-1", Type="Occurrence"))
    next_day = parse_event(
        _raw(
            Id="occ-2",
            Type="Occurrence",
            Start={"DateTime": "2026-09-15T06:15:00.0000000", "TimeZone": "UTC"},
        )
    )
    other = parse_event(_raw(Id="occ-3", Type="Occurrence", Subject="Weekly review"))

    assert first["is_recurring"] is True
    assert first["recurrence_master_id"]
    assert first["recurrence_master_id"] == next_day["recurrence_master_id"]
    assert first["recurrence_master_id"] != other["recurrence_master_id"]
    assert first["recurrence_master_id"] not in {"occ-1", "occ-2", "occ-3"}


def test_a_list_entry_says_nothing_about_recurrence():
    """list-calendar's $select carries neither Type nor SeriesMasterId."""
    parsed = parse_event(_raw())

    assert (parsed["is_recurring"], parsed["recurrence_master_id"]) == (False, None)


# ------------------------------------------------------------ through calendar-sync


def _occurrence(event_id: str, start: datetime, etag: str, attendees: int = 3) -> dict:
    return _raw(
        Id=event_id,
        Type="Occurrence",
        SeriesMasterId="master-1",
        Start={"DateTime": start.strftime(_FMT), "TimeZone": "UTC"},
        End={"DateTime": (start + timedelta(minutes=15)).strftime(_FMT), "TimeZone": "UTC"},
        Attendees=[
            {
                "EmailAddress": {"Address": f"a{n}@example.com", "Name": f"Attendee {n}"},
                "Status": {"Response": "Accepted"},
            }
            for n in range(attendees)
        ],
        ResponseStatus={"Response": "Accepted"},
        Body={"Content": ""},
        **{"@odata.etag": etag},
    )


def _sync(monkeypatch, db_path: str, events: list[dict]) -> int:
    """One calendar-sync over the coming days; list-calendar lists `events` without
    the fields its $select leaves out, get-event returns them whole."""
    from src import cli
    from src.export import calendar_export

    by_id = {e["Id"]: e for e in events}
    listed_keys = ("Id", "Subject", "Start", "End", "Organizer", "@odata.etag")

    def run(args):
        if args[0] == "get-event":
            return by_id[args[1]]
        return [{k: v for k, v in e.items() if k in listed_keys} for e in events]

    monkeypatch.setattr(calendar_export, "run_outlook_cli", run)
    args = types.SimpleNamespace(
        db=db_path, backfill=False, since=None, until=None, skip_extraction=True
    )
    return cli.cmd_calendar_sync(args)


def _store(tmp_path) -> str:
    from src.store.schema import create_database

    path = str(tmp_path / "brain.db")
    create_database(path).close()
    return path


def _rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ce.outlook_event_id, ce.is_recurring, ce.recurrence_master_id, ce.is_cancelled, "
        "(SELECT COUNT(*) FROM event_attendees a WHERE a.event_id = ce.id) AS attendees "
        "FROM calendar_events ce ORDER BY ce.outlook_event_id"
    ).fetchall()
    conn.close()
    return rows


def test_a_re_sync_updates_each_occurrence_in_place(monkeypatch, tmp_path):
    path = _store(tmp_path)
    soon = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    days = [soon + timedelta(days=n) for n in range(3)]

    _sync(monkeypatch, path, [_occurrence(f"occ-{n}", d, "e1") for n, d in enumerate(days)])
    # The etag moves on every occurrence and one more attendee is invited.
    _sync(
        monkeypatch,
        path,
        [_occurrence(f"occ-{n}", d, "e2", attendees=4) for n, d in enumerate(days)],
    )

    rows = _rows(path)
    assert [r["outlook_event_id"] for r in rows] == ["occ-0", "occ-1", "occ-2"]
    assert {(r["is_recurring"], r["recurrence_master_id"]) for r in rows} == {(1, "master-1")}
    assert [r["attendees"] for r in rows] == [4, 4, 4]


def test_occurrences_stored_before_are_flagged_by_the_next_sync(monkeypatch, tmp_path):
    """An occurrence is fetched again only when it changes, and every row stored
    before held is_recurring 0 beside the series id that came with it."""
    path = _store(tmp_path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at, "
        "is_recurring, recurrence_master_id, ingested_at) "
        "VALUES (?, 'Call', '2025-01-06T09:00:00Z', '2025-01-06T10:00:00Z', 0, ?, '2025-01-06')",
        [("old-occurrence", "master-1"), ("old-single", None)],
    )
    conn.commit()
    conn.close()

    _sync(monkeypatch, path, [])

    flags = dict(
        sqlite3.connect(path)
        .execute("SELECT outlook_event_id, is_recurring FROM calendar_events")
        .fetchall()
    )
    assert flags == {"old-occurrence": 1, "old-single": 0}


def test_an_occurrence_listed_under_a_new_id_keeps_its_row(monkeypatch, tmp_path):
    """Outlook can issue an occurrence a new id (its series re-sent). Same series,
    same start, and the old id no longer listed: the same meeting, not a second one."""
    path = _store(tmp_path)
    start = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    _sync(monkeypatch, path, [_occurrence("old-id", start, "e1")])

    _sync(monkeypatch, path, [_occurrence("new-id", start, "e2")])

    rows = _rows(path)
    assert [(r["outlook_event_id"], r["is_cancelled"]) for r in rows] == [("new-id", 0)]


def test_two_listed_occurrences_at_one_time_stay_two(monkeypatch, tmp_path):
    """One occurrence moved onto another's slot: both are listed, both are kept."""
    path = _store(tmp_path)
    start = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    _sync(monkeypatch, path, [_occurrence("occ-a", start, "e1")])

    _sync(
        monkeypatch,
        path,
        [_occurrence("occ-a", start, "e1"), _occurrence("occ-b", start, "e1")],
    )

    rows = _rows(path)
    assert [(r["outlook_event_id"], r["is_cancelled"]) for r in rows] == [
        ("occ-a", 0),
        ("occ-b", 0),
    ]
