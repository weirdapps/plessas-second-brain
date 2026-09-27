"""calendar_export.list_events never takes a possibly truncated page as the whole span.

outlook-cli's list-calendar returns one page of Outlook's calendarview, the ten
earliest events of the window, however many it holds (mcp-1, teams-calendar-1,
vps-runtime-1). The fake below behaves the same way over a synthetic calendar.
"""

from datetime import datetime, timedelta

from src.export import calendar_export
from src.export.calendar_export import OUTLOOK_PAGE_SIZE

_FMT = "%Y-%m-%dT%H:%M:%S.0000000"


def _event(event_id: str, start: datetime, minutes: int) -> dict:
    return {
        "Id": event_id,
        "Subject": event_id,
        "Start": {"DateTime": start.strftime(_FMT), "TimeZone": "UTC"},
        "End": {"DateTime": (start + timedelta(minutes=minutes)).strftime(_FMT), "TimeZone": "UTC"},
    }


def _calendar() -> list[dict]:
    """About sixty events over thirty days, one of them a day with twenty-five,
    plus a week-long event and one across the month boundary, both of which
    overlap every split near them."""
    first = datetime(2026, 9, 20)
    busy = first + timedelta(days=10)
    events = []
    for day in range(30):
        date = first + timedelta(days=day)
        if date == busy:
            events += [
                _event(f"busy-{n}", date + timedelta(hours=8, minutes=30 * n), 30)
                for n in range(25)
            ]
            continue
        events.append(_event(f"d{day}-am", date + timedelta(hours=9), 60))
        if day % 4 == 0:
            events.append(_event(f"d{day}-pm", date + timedelta(hours=15), 60))
    events.append(_event("offsite", first + timedelta(days=2, hours=7), 4 * 24 * 60))
    events.append(_event("midnight", datetime(2026, 9, 30, 23, 30), 60))
    return events


def _fake_cli(calendar: list[dict], calls: list[tuple[datetime, datetime]]):
    """list-calendar as outlook-cli answers it today: the events overlapping the
    window, earliest first, cut at one page."""

    def run(args):
        assert args[0] == "list-calendar"
        start = datetime.fromisoformat(args[args.index("--from") + 1])
        end = datetime.fromisoformat(args[args.index("--to") + 1])
        calls.append((start, end))
        overlapping = [
            e
            for e in calendar
            if datetime.fromisoformat(e["Start"]["DateTime"]) < end
            and datetime.fromisoformat(e["End"]["DateTime"]) > start
        ]
        overlapping.sort(key=lambda e: e["Start"]["DateTime"])
        return overlapping[:OUTLOOK_PAGE_SIZE]

    return run


def test_every_event_comes_back_exactly_once(monkeypatch):
    calendar = _calendar()
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))
    failures: list[str] = []

    events = calendar_export.list_events(
        datetime(2026, 9, 20), datetime(2026, 10, 20), failures=failures
    )

    ids = [e["Id"] for e in events]
    assert failures == []
    assert sorted(ids) == sorted(e["Id"] for e in calendar)
    assert len(ids) == len(set(ids)), "an event across a split is listed once"
    starts = [e["Start"]["DateTime"] for e in events]
    assert starts == sorted(starts)
    assert len(calls) <= calendar_export.DEFAULT_LIST_CALLS


def test_a_page_that_is_not_full_is_not_split(monkeypatch):
    """Once outlook-cli pages for itself it returns more than ten, and a chunk
    costs one call again."""
    calendar = _calendar()
    calls: list = []

    def everything(args):
        calls.append(args)
        return list(calendar)

    monkeypatch.setattr(calendar_export, "run_outlook_cli", everything)
    failures: list[str] = []

    events = calendar_export.list_events(
        datetime(2026, 9, 20), datetime(2026, 10, 20), failures=failures
    )

    assert len(calls) == 2  # September and October
    assert (failures, len(events)) == ([], len(calendar))


def test_an_hour_holding_more_than_a_page_is_a_failure(monkeypatch, caplog):
    """No split can separate eleven events that share an hour, and a full page
    of them cannot be told from a complete one, so the run must not pass for
    complete: the caller turns failures into a non-zero exit and keeps
    calendar_last_listed where it was."""
    start = datetime(2026, 10, 5, 10)
    calendar = [_event(f"crowd-{n}", start, 45) for n in range(11)]
    calendar.append(_event("later", datetime(2026, 10, 7, 9), 60))
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))
    failures: list[str] = []

    events = calendar_export.list_events(
        datetime(2026, 10, 1), datetime(2026, 10, 10), failures=failures
    )

    # Every short span the crowd fills is one, and they are all on that day.
    assert failures
    assert all(f.startswith("2026-10-05") for f in failures)
    # What was listed is still returned: those events are real.
    assert {e["Id"] for e in events} >= {"later"}
    assert len([e for e in events if e["Id"].startswith("crowd-")]) == OUTLOOK_PAGE_SIZE
    assert "splitting" in caplog.text or "halves" in caplog.text


def test_the_call_bound_is_enforced_and_is_a_failure(monkeypatch):
    calendar = _calendar()
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))
    failures: list[str] = []

    calendar_export.list_events(
        datetime(2026, 9, 20), datetime(2026, 10, 20), failures=failures, max_calls=5
    )

    assert len(calls) == 5
    assert len(failures) == 1
    assert "5" in failures[0]


def test_every_subdivision_is_logged(monkeypatch, caplog):
    """calendar-sync.log is stderr, and only a warning reaches it: cli.py
    configures no logging, so an info line would be dropped."""
    import logging

    calendar = _calendar()
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))

    with caplog.at_level(logging.WARNING, logger="src.export.calendar_export"):
        calendar_export.list_events(datetime(2026, 9, 20), datetime(2026, 10, 20), failures=[])

    full_pages = sum(
        1 for r in caplog.records if r.levelno == logging.WARNING and "full page" in r.message
    )
    # Every call past one per chunk is half of a split.
    assert full_pages and len(calls) - 2 == 2 * full_pages


# ------------------------------------------------------------ what the run makes of it


def _sync(monkeypatch, tmp_path, run_outlook_cli, **flags) -> tuple[int, str | None]:
    """One cmd_calendar_sync over a fresh store; its exit code and heartbeat."""
    import sqlite3
    import types

    from src import cli
    from src.store.schema import create_database

    db_path = str(tmp_path / "brain.db")
    if not (tmp_path / "brain.db").exists():
        create_database(db_path).close()
    monkeypatch.setattr(calendar_export, "run_outlook_cli", run_outlook_cli)
    args = types.SimpleNamespace(
        db=db_path, backfill=False, since=None, until=None, skip_extraction=True
    )
    for name, value in flags.items():
        setattr(args, name, value)
    rc = cli.cmd_calendar_sync(args)
    row = (
        sqlite3.connect(db_path)
        .execute("SELECT value FROM sync_metadata WHERE key = 'calendar_last_listed'")
        .fetchone()
    )
    return rc, row[0] if row else None


def _answering(calendar: list[dict], calls: list):
    """outlook-cli for both commands the run uses: a one-page list-calendar, and
    get-event returning the whole event."""
    listing = _fake_cli(calendar, calls)
    by_id = {e["Id"]: e for e in calendar}

    def run(args):
        if args[0] == "get-event":
            return {**by_id[args[1]], "Body": {"Content": ""}}
        return listing(args)

    return run


def test_a_run_whose_window_holds_an_unlistable_hour_fails_and_leaves_no_heartbeat(
    monkeypatch, tmp_path
):
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    calendar = [_event(f"crowd-{n}", now + timedelta(days=2), 45) for n in range(11)]
    calls: list = []

    rc, heartbeat = _sync(monkeypatch, tmp_path, _answering(calendar, calls))

    assert (rc, heartbeat) == (1, None)


def test_a_run_over_a_busy_window_lists_it_all(monkeypatch, tmp_path):
    """The week ahead was never stored: each chunk's ten events were a week old."""
    import sqlite3

    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    calendar = [
        _event(f"ahead-{day}-{n}", now + timedelta(days=day, hours=n), 30)
        for day in range(1, 8)
        for n in range(6)
    ]
    calls: list = []

    rc, heartbeat = _sync(monkeypatch, tmp_path, _answering(calendar, calls))

    stored = sqlite3.connect(str(tmp_path / "brain.db")).execute(
        "SELECT COUNT(*) FROM calendar_events"
    )
    assert (rc, stored.fetchone()[0]) == (0, len(calendar))
    assert heartbeat is not None


def test_backfill_gets_the_larger_call_bound(monkeypatch, tmp_path):
    bounds: list = []

    def record(since, until, failures=None, max_calls=calendar_export.DEFAULT_LIST_CALLS):
        bounds.append(max_calls)
        return []

    monkeypatch.setattr(calendar_export, "list_events", record)
    _sync(monkeypatch, tmp_path, lambda args: [], backfill=True)
    _sync(monkeypatch, tmp_path, lambda args: [])

    assert bounds == [calendar_export.BACKFILL_LIST_CALLS, calendar_export.DEFAULT_LIST_CALLS]
