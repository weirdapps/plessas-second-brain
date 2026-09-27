"""An event Outlook no longer lists stops being reported as a meeting (teams-calendar-2).

cmd_calendar_sync only upserted what it listed, so an event deleted or re-created
in Outlook stayed in calendar_events for good, and person_context, recall and
query_calendar_events reported meetings that no longer exist. After a complete
listing, stored events in the window that were not listed are now marked
cancelled, and every reader leaves cancelled events out.
"""

import logging
import sqlite3
import types
from datetime import UTC, datetime, timedelta

import pytest

from src.store.schema import create_database, get_connection

_ETAG = 'W/"1"'


def _graph(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000")


def _store(conn, event_id: str, start: datetime, *, cancelled: int = 0) -> None:
    conn.execute(
        "INSERT INTO calendar_events (outlook_event_id, subject, start_at, end_at, is_cancelled, "
        "ingested_at, llm_status, change_key) VALUES (?, ?, ?, ?, ?, '2026-09-01', 'skipped', ?)",
        (
            event_id,
            f"Meeting {event_id}",
            _graph(start) + "Z",
            _graph(start + timedelta(hours=1)) + "Z",
            cancelled,
            _ETAG,
        ),
    )


def _listed(event_id: str, start: datetime) -> dict:
    """One entry of list-calendar's answer: Id, times and etag, no attendees."""
    return {
        "Id": event_id,
        "Subject": f"Meeting {event_id}",
        "Start": {"DateTime": _graph(start), "TimeZone": "UTC"},
        "End": {"DateTime": _graph(start + timedelta(hours=1)), "TimeZone": "UTC"},
        "@odata.etag": _ETAG,
    }


@pytest.fixture
def store(tmp_path):
    """A store holding three meetings Outlook still lists and one it does not,
    all in the coming days, and an old one well before the window."""
    path = str(tmp_path / "brain.db")
    conn = create_database(path)
    soon = datetime.now(UTC).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    starts = {name: soon + timedelta(days=n) for n, name in enumerate(("a", "b", "c", "gone"), 1)}
    for name, start in starts.items():
        _store(conn, name, start)
    _store(conn, "old", soon - timedelta(days=40))
    conn.commit()
    conn.close()
    return path, starts


def _run(monkeypatch, path, listing, body=None) -> int:
    from src import cli
    from src.export import calendar_export

    monkeypatch.setattr(calendar_export, "list_events", listing)
    monkeypatch.setattr(
        calendar_export,
        "get_event_body",
        body or (lambda event_id: pytest.fail(f"unchanged {event_id} fetched")),
    )
    return cli.cmd_calendar_sync(
        types.SimpleNamespace(db=path, backfill=False, since=None, until=None, skip_extraction=True)
    )


def _cancelled(path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    rows = dict(conn.execute("SELECT outlook_event_id, is_cancelled FROM calendar_events"))
    conn.close()
    return rows


def test_an_event_outlook_no_longer_lists_is_marked_cancelled(monkeypatch, store):
    path, starts = store
    listing = [_listed(name, starts[name]) for name in ("a", "b", "c")]

    rc = _run(monkeypatch, path, lambda since, until, failures=None: listing)

    assert rc == 0
    assert _cancelled(path) == {"a": 0, "b": 0, "c": 0, "gone": 1, "old": 0}


def test_nothing_is_marked_after_a_listing_that_was_not_complete(monkeypatch, store):
    """A failed or truncated span is exactly a window whose missing events say
    nothing about what Outlook holds."""
    path, starts = store
    listing = [_listed(name, starts[name]) for name in ("a", "b", "c")]

    def truncated(since, until, failures=None):
        failures.append("2026-10-05 09:00..2026-10-05 10:00: a full page of 10 events")
        return listing

    assert _run(monkeypatch, path, truncated) == 1
    assert _cancelled(path)["gone"] == 0


def test_nothing_is_marked_when_the_session_expires_mid_run(monkeypatch, store):
    from src.export.outlook_cli import OutlookCliAuthRequired

    path, starts = store
    edited = {**_listed("a", starts["a"]), "@odata.etag": 'W/"2"'}
    listing = [edited] + [_listed(name, starts[name]) for name in ("b", "c")]

    def expired(event_id):
        raise OutlookCliAuthRequired("session expired")

    assert _run(monkeypatch, path, lambda since, until, failures=None: listing, expired) == 4
    assert _cancelled(path)["gone"] == 0


def test_more_than_half_the_window_going_at_once_marks_nothing(monkeypatch, store, caplog):
    """Three of four meetings vanishing in one run is far likelier to be a bad
    answer than a cleared calendar, and marking them would hide real meetings."""
    path, starts = store
    listing = [_listed("a", starts["a"])]

    with caplog.at_level(logging.WARNING):
        rc = _run(monkeypatch, path, lambda since, until, failures=None: listing)

    assert rc == 0
    assert set(_cancelled(path).values()) == {0}
    assert "not marking" in caplog.text


def test_an_event_listed_again_comes_back(monkeypatch, store):
    """Marked with its etag cleared, it is fetched again when it reappears, and
    the upsert takes is_cancelled from Outlook's own answer."""
    path, starts = store
    three = [_listed(name, starts[name]) for name in ("a", "b", "c")]
    _run(monkeypatch, path, lambda since, until, failures=None: three)
    assert _cancelled(path)["gone"] == 1

    four = three + [_listed("gone", starts["gone"])]
    fetched: list[str] = []

    def body(event_id):
        fetched.append(event_id)
        return {**four[3], "IsCancelled": False, "Body": {"Content": ""}}

    assert _run(monkeypatch, path, lambda since, until, failures=None: four, body) == 0
    assert fetched == ["gone"]
    assert _cancelled(path)["gone"] == 0


def test_outlooks_own_cancellation_is_stored(monkeypatch, store):
    path, starts = store
    edited = {**_listed("a", starts["a"]), "@odata.etag": 'W/"2"'}
    listing = [edited] + [_listed(name, starts[name]) for name in ("b", "c", "gone")]

    def body(event_id):
        return {**edited, "IsCancelled": True, "Body": {"Content": ""}}

    _run(monkeypatch, path, lambda since, until, failures=None: listing, body)

    assert _cancelled(path)["a"] == 1


# ------------------------------------------------------------------ the readers


@pytest.fixture
def readers(tmp_path, monkeypatch):
    """One live and one cancelled meeting with the same person, before and after now."""
    from src import mcp_server

    path = str(tmp_path / "brain.db")
    conn = create_database(path)
    conn.execute(
        "INSERT INTO people (id, name, email) VALUES (1, 'Dana Duarte', 'dana@example.com')"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        ("past-live", now - timedelta(days=3), 0),
        ("past-cancelled", now - timedelta(days=1), 1),
        ("next-cancelled", now + timedelta(days=1), 1),
        ("next-live", now + timedelta(days=3), 0),
    ]
    for event_id, start, cancelled in rows:
        _store(conn, event_id, start, cancelled=cancelled)
        conn.execute(
            "INSERT INTO event_attendees (event_id, person_id, email, name, response_status) "
            "SELECT id, 1, 'dana@example.com', 'Dana Duarte', 'Accepted' FROM calendar_events "
            "WHERE outlook_event_id = ?",
            (event_id,),
        )
    conn.commit()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(path))
    return conn, mcp_server


def test_query_calendar_events_leaves_cancelled_events_out(readers):
    _conn, mcp_server = readers

    out = mcp_server.query_calendar_events(keyword="Meeting")

    assert sorted(e["subject"] for e in out["events"]) == [
        "Meeting next-live",
        "Meeting past-live",
    ]


def test_person_context_leaves_cancelled_events_out(readers):
    from src.store.context import get_person_context

    conn, _ = readers

    ctx = get_person_context(conn, "dana@example.com")

    assert ctx["last_met"]["subject"] == "Meeting past-live"
    assert ctx["next_meeting"]["subject"] == "Meeting next-live"
    assert ctx["meeting_count_30d"] == 2


def test_recalls_calendar_bucket_leaves_cancelled_events_out(readers):
    from src.store.recall import _search_calendar_events

    conn, _ = readers

    found = _search_calendar_events(conn, "Meeting", 10)

    assert sorted(r["subject"] for r in found) == ["Meeting next-live", "Meeting past-live"]
