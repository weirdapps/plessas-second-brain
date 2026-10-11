"""calendar-sync --backfill fills the months coverage flags, a month at a time.

The first load kept ten events of each month, and the backfill that followed
listed the 365 days before it ran, so May to September 2025 stayed as they were
(data-coverage-02). A backfill now takes the months `stats` coverage flags, lists
each completely before the next, records it, and a run that stops is resumed by
the next one. --dry-run lists and counts, and writes nothing.
"""

import sqlite3
import sys
import types
from datetime import datetime, timedelta

import pytest

from src.export import calendar_export
from src.export.outlook_cli import OutlookCliAuthRequired
from src.store.calendar_coverage import density, listed_months
from tests.patching import unset_env
from tests.test_audit_calendar_listing import _FMT, _span, _utc

_FULL = 48  # events in a month held in full


def _event(event_id: str, start: datetime) -> dict:
    """An event as get-event returns it; list-calendar returns a subset."""
    return {
        "Id": event_id,
        "Subject": f"Meeting {event_id}",
        "Type": "SingleInstance",
        "Start": {"DateTime": start.strftime(_FMT), "TimeZone": "UTC"},
        "End": {"DateTime": (start + timedelta(hours=1)).strftime(_FMT), "TimeZone": "UTC"},
        "Organizer": {"EmailAddress": {"Address": "chair@example.com", "Name": "Chair"}},
        "ResponseStatus": {"Response": "Accepted"},
        "Attendees": [],
        "Body": {"Content": ""},
        "@odata.etag": "e1",
    }


def _month(month: str) -> list[dict]:
    """_FULL events between the 2nd and the 25th, clear of the month's edges in
    any host time zone, since outlook-cli reads a window in local time."""
    first = datetime.fromisoformat(f"{month}-02")
    return [
        _event(f"{month}-{n}", first + timedelta(days=n // 2, hours=9 + 5 * (n % 2)))
        for n in range(_FULL)
    ]


_MONTHS = ["2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08"]
_CAPPED = ["2025-03", "2025-04"]


@pytest.fixture
def calendar():
    """Six months of meetings in Outlook."""
    return {e["Id"]: e for month in _MONTHS for e in _month(month)}


@pytest.fixture
def store(tmp_path, calendar):
    """A store whose first load kept ten events of March and April and every event
    of May to August, as calendar-sync stores them."""
    from src.export.calendar_export import parse_event
    from src.store.calendar_loader import load_event
    from src.store.schema import create_database

    path = str(tmp_path / "brain.db")
    conn = create_database(path)
    for month in _MONTHS:
        kept = _month(month)[:10] if month in _CAPPED else _month(month)
        for raw in kept:
            load_event(conn, parse_event(raw), {}, llm_status="skipped")
    conn.close()
    return path


class Outlook:
    """outlook-cli over `calendar`: every event a window overlaps, and get-event."""

    def __init__(self, calendar: dict, fail_month=None, expire_on=None):
        self.calendar = calendar
        self.windows: list[tuple[datetime, datetime]] = []
        self.fetched: list[str] = []
        self.fail_month = fail_month
        self.expire_on = expire_on

    def __call__(self, args):
        if args[0] == "get-event":
            if args[1] == self.expire_on:
                raise OutlookCliAuthRequired("session expired")
            self.fetched.append(args[1])
            return self.calendar[args[1]]
        start, end = _utc(args[args.index("--from") + 1]), _utc(args[args.index("--to") + 1])
        self.windows.append((start, end))
        if f"{start + (end - start) / 2:%Y-%m}" == self.fail_month:
            raise RuntimeError("upstream error")
        listed = ("Id", "Subject", "Start", "End", "Organizer", "@odata.etag")
        return [
            {k: v for k, v in e.items() if k in listed}
            for e in self.calendar.values()
            if _span(e)[0] < end and _span(e)[1] > start
        ]


def _backfill(monkeypatch, path: str, outlook: Outlook, **flags) -> int:
    from src import cli

    monkeypatch.setattr(calendar_export, "run_outlook_cli", outlook)
    args = types.SimpleNamespace(
        db=path,
        backfill=True,
        since=None,
        until=None,
        skip_extraction=True,
        dry_run=False,
    )
    for name, value in flags.items():
        setattr(args, name, value)
    return cli.cmd_calendar_sync(args)


def _held(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    held = dict(
        conn.execute(
            "SELECT substr(start_at, 1, 7), COUNT(*) FROM calendar_events "
            "WHERE is_cancelled = 0 GROUP BY 1"
        ).fetchall()
    )
    conn.close()
    return held


def _months_listed(outlook: Outlook) -> list[str]:
    """The month each list-calendar window falls in, by its midpoint."""
    return [f"{start + (end - start) / 2:%Y-%m}" for start, end in outlook.windows]


def _flagged(path: str) -> dict:
    conn = sqlite3.connect(path)
    flags = density(conn)["incomplete"]
    conn.close()
    return flags


def test_the_store_starts_with_the_capped_months_flagged(store):
    assert _flagged(store) == {"2025-03": "capped", "2025-04": "capped"}


def test_a_backfill_fills_the_flagged_months_and_only_them(monkeypatch, store, calendar):
    outlook = Outlook(calendar)

    rc = _backfill(monkeypatch, store, outlook)

    assert rc == 0
    assert _months_listed(outlook) == _CAPPED
    # The ten each month held are unchanged and are not fetched again.
    assert len(outlook.fetched) == 2 * (_FULL - 10)
    assert _held(store) == dict.fromkeys(_MONTHS, _FULL)
    assert _flagged(store) == {}
    conn = sqlite3.connect(store)
    assert listed_months(conn) == set(_CAPPED)
    conn.close()


def test_a_dry_run_counts_what_it_would_fetch_and_writes_nothing(
    monkeypatch, store, calendar, capsys
):
    outlook = Outlook(calendar)
    before = sqlite3.connect(store).execute("SELECT COUNT(*) FROM calendar_events").fetchone()

    rc = _backfill(monkeypatch, store, outlook, dry_run=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert _months_listed(outlook) == _CAPPED
    assert outlook.fetched == []
    assert f"{2 * (_FULL - 10)} event(s) to fetch" in out
    for month in _CAPPED:
        assert month in out
    conn = sqlite3.connect(store)
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone() == before
    assert listed_months(conn) == set()
    conn.close()


def test_a_stopped_backfill_resumes_with_the_months_it_did_not_finish(
    monkeypatch, tmp_path, calendar
):
    from src.store.schema import create_database

    path = str(tmp_path / "brain.db")
    create_database(path).close()
    window = {"since": "2025-05-01", "until": "2025-08-01"}
    june = Outlook(calendar, fail_month="2025-06")

    assert _backfill(monkeypatch, path, june, **window) == 1
    assert _months_listed(june) == ["2025-05", "2025-06", "2025-07"]
    conn = sqlite3.connect(path)
    assert listed_months(conn) == {"2025-05", "2025-07"}
    conn.close()
    assert _flagged(path) == {"2025-06": "partial"}

    again = Outlook(calendar)
    assert _backfill(monkeypatch, path, again, **window) == 0

    assert _months_listed(again) == ["2025-06"]
    assert _held(path) == {"2025-05": _FULL, "2025-06": _FULL, "2025-07": _FULL}


def test_a_month_that_still_owes_an_extraction_is_listed_again(monkeypatch, tmp_path, calendar):
    from src.store.schema import create_database

    path = str(tmp_path / "brain.db")
    create_database(path).close()
    window = {"since": "2025-05-01", "until": "2025-07-01"}
    _backfill(monkeypatch, path, Outlook(calendar), **window)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE calendar_events SET llm_status = 'pending' WHERE outlook_event_id = '2025-06-3'"
    )
    conn.commit()
    conn.close()

    again = Outlook(calendar)
    _backfill(monkeypatch, path, again, **window)

    assert _months_listed(again) == ["2025-06"]


def test_a_session_that_expires_mid_backfill_leaves_the_rest_to_the_next_run(
    monkeypatch, store, calendar
):
    expired = Outlook(calendar, expire_on="2025-04-20")

    assert _backfill(monkeypatch, store, expired) == 4
    conn = sqlite3.connect(store)
    assert listed_months(conn) == {"2025-03"}
    conn.close()
    # April is half filled now, enough to pass for a full month: it stays flagged.
    assert _held(store)["2025-04"] > 10
    assert _flagged(store) == {"2025-04": "partial"}

    again = Outlook(calendar)
    assert _backfill(monkeypatch, store, again) == 0
    assert _months_listed(again) == ["2025-04"]
    assert _flagged(store) == {}


def test_a_backfill_of_past_months_leaves_the_hourly_heartbeat_alone(monkeypatch, store, calendar):
    """Its listing never reached the present, so it says nothing about the sync."""
    _backfill(monkeypatch, store, Outlook(calendar))

    row = (
        sqlite3.connect(store)
        .execute("SELECT value FROM sync_metadata WHERE key = 'calendar_last_listed'")
        .fetchone()
    )
    assert row is None


def test_every_backfill_month_gets_the_backfill_call_bound(monkeypatch, tmp_path):
    from src import cli
    from src.store.schema import create_database

    path = str(tmp_path / "brain.db")
    create_database(path).close()
    bounds: list = []

    def record(since, until, failures=None, max_calls=calendar_export.DEFAULT_LIST_CALLS):
        bounds.append(max_calls)
        return []

    monkeypatch.setattr(calendar_export, "list_events", record)
    args = types.SimpleNamespace(
        db=path, backfill=True, since="2025-05-01", until="2025-08-01", skip_extraction=True
    )
    cli.cmd_calendar_sync(args)

    assert bounds == [calendar_export.BACKFILL_LIST_CALLS] * 3


def test_a_plain_dry_run_fetches_and_writes_nothing(monkeypatch, tmp_path, calendar, capsys):
    from src.store.schema import create_database

    path = str(tmp_path / "brain.db")
    create_database(path).close()
    soon = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    ahead = {f"ahead-{n}": _event(f"ahead-{n}", soon + timedelta(hours=n)) for n in range(3)}
    outlook = Outlook(ahead)

    rc = _backfill(monkeypatch, path, outlook, backfill=False, dry_run=True)

    assert rc == 0
    assert outlook.fetched == []
    assert "3 event(s) to fetch" in capsys.readouterr().out
    assert _held(path) == {}


def test_a_dry_run_opens_the_store_read_only(monkeypatch, store, calendar):
    """Not even the journal mode a writer's connection sets on open is written."""
    conn = sqlite3.connect(store)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()

    assert _backfill(monkeypatch, store, Outlook(calendar), dry_run=True) == 0

    conn = sqlite3.connect(store)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_a_replica_refuses_a_backfill_even_a_dry_run(monkeypatch, tmp_path, capsys):
    from src import cli, config

    stamp = tmp_path / "db-pull.stamp"
    stamp.write_text("x")
    monkeypatch.setattr(config, "REPLICA_STAMP", stamp)
    unset_env(monkeypatch, "BRAIN_ROLE")
    called: list = []
    monkeypatch.setattr(cli, "cmd_calendar_sync", lambda args: called.append(args))
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["brain", "--db", str(tmp_path / "brain.db"), "calendar-sync", "--backfill", "--dry-run"],
    )

    with pytest.raises(SystemExit) as exit_:
        cli.main()

    assert (exit_.value.code, called) == (2, [])
    assert "BRAIN_ROLE=producer" in capsys.readouterr().err
