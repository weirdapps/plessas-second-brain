"""calendar_export.list_events never takes a possibly truncated page as the whole span.

outlook-cli's list-calendar returns one page of Outlook's calendarview, the ten
earliest events of the window, however many it holds (mcp-1, teams-calendar-1,
vps-runtime-1). The fake below behaves the same way over a synthetic calendar.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest

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


def _all_day(event_id: str, date: datetime) -> dict:
    """An all-day event as list-calendar returns it: a floating midnight labelled
    UTC, whatever the zone it was created in."""
    return {
        "Id": event_id,
        "Subject": event_id,
        "IsAllDay": True,
        "Start": {"DateTime": date.strftime(_FMT), "TimeZone": "UTC"},
        "End": {"DateTime": (date + timedelta(days=1)).strftime(_FMT), "TimeZone": "UTC"},
    }


def _utc(value: str) -> datetime:
    """A --from or --to as outlook-cli reads it: naive means this host's local
    time (Date.parse), and Outlook compares it in UTC. Naive UTC out, to match
    the events' DateTime."""
    return datetime.fromisoformat(value).astimezone(UTC).replace(tzinfo=None)


def _span(event: dict) -> tuple[datetime, datetime]:
    """Where Exchange places an event, in naive UTC. An all-day event sits at
    the host's local midnight, not at the midnight its DateTime shows (verified
    live: an all-day event overlapped a 21:30Z-23:00Z window the day before)."""
    start = datetime.fromisoformat(event["Start"]["DateTime"])
    end = datetime.fromisoformat(event["End"]["DateTime"])
    if event.get("IsAllDay"):
        return _utc(start.isoformat()), _utc(end.isoformat())
    return start, end


def _fake_cli(calendar: list[dict], calls: list[tuple[datetime, datetime]]):
    """list-calendar as outlook-cli answers it today: the events overlapping the
    window, earliest first, cut at one page."""

    def run(args):
        assert args[0] == "list-calendar"
        start = _utc(args[args.index("--from") + 1])
        end = _utc(args[args.index("--to") + 1])
        calls.append((start, end))
        overlapping = [e for e in calendar if _span(e)[0] < end and _span(e)[1] > start]
        overlapping.sort(key=lambda e: _span(e)[0])
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
    # Every call past one per chunk lists part of a full page again, and each
    # full page is listed again in one part or two.
    assert full_pages and full_pages <= len(calls) - 2 <= 2 * full_pages


@pytest.fixture(params=["UTC", "Europe/Athens", "America/New_York"])
def host_zone(request, monkeypatch):
    """The host's zone, in which outlook-cli reads a naive --from and --to. East
    and west of UTC both, because reading a UTC start as local time errs late
    on one side, and late loses events."""
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


def test_a_full_page_is_complete_up_to_its_last_start(monkeypatch, host_zone):
    """list-calendar orders by start, so a full page holds every event that
    starts before the last one on it, and only the rest is listed again. The
    whole first half used to be, and split again and again around a busy
    morning it already held."""
    morning = datetime(2026, 10, 2, 6)
    calendar = [_event(f"early-{n}", morning + timedelta(minutes=30 * n), 30) for n in range(11)]
    calendar += [_event(f"late-{n}", datetime(2026, 10, 20, 9 + n), 60) for n in range(4)]
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))
    failures: list[str] = []

    events = calendar_export.list_events(
        datetime(2026, 10, 1), datetime(2026, 10, 31), failures=failures
    )

    ids = [e["Id"] for e in events]
    assert failures == []
    assert sorted(ids) == sorted(e["Id"] for e in calendar)
    assert len(ids) == len(set(ids))
    # The month, the rest of its first half from the tenth start on, its second half.
    assert len(calls) == 3


def test_a_half_the_page_already_holds_is_not_listed_again(monkeypatch, host_zone):
    day = datetime(2026, 10, 8, 6)
    calendar = [_event(f"day-{n}", day + timedelta(minutes=30 * n), 30) for n in range(12)]
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))
    failures: list[str] = []

    events = calendar_export.list_events(
        datetime(2026, 10, 1), datetime(2026, 10, 11), failures=failures
    )

    assert (failures, sorted(e["Id"] for e in events)) == ([], sorted(e["Id"] for e in calendar))
    tenth = day + timedelta(minutes=30 * 9)
    assert all(end >= tenth for _, end in calls), "a window the first page holds was listed"


def test_an_event_of_no_length_at_the_cut_is_still_listed(monkeypatch, host_zone):
    """The rest is listed from a second before the tenth start: an event of no
    length that starts with the tenth and was cut off with the page need not
    overlap a window that opens exactly at its start."""
    day = datetime(2026, 10, 8, 6)
    calendar = [_event(f"day-{n}", day + timedelta(minutes=30 * n), 30) for n in range(10)]
    calendar.append(_event("reminder", day + timedelta(minutes=30 * 9), 0))
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))

    events = calendar_export.list_events(datetime(2026, 10, 1), datetime(2026, 10, 11), failures=[])

    assert "reminder" in {e["Id"] for e in events}


def test_an_all_day_event_does_not_stretch_the_page(monkeypatch, host_zone):
    """East of UTC an all-day event's floating midnight is later than where it
    sits in the page. Taken as the page's last start, it moved the resume point
    past a timed event cut from the page just after local midnight, which was
    then never listed, and the run still counted as complete."""
    day = datetime(2026, 6, 10)
    calendar = [
        _event(f"t{n}", day - timedelta(days=1) + timedelta(hours=6, minutes=30 * n), 30)
        for n in range(9)
    ]
    calendar.append(_all_day("all-day", day))
    # 00:30 local time on the all-day event's date.
    calendar.append(_event("late-call", _utc((day + timedelta(minutes=30)).isoformat()), 30))
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))
    failures: list[str] = []

    events = calendar_export.list_events(
        datetime(2026, 6, 9), datetime(2026, 6, 20), failures=failures
    )

    assert failures == []
    assert sorted(e["Id"] for e in events) == sorted(e["Id"] for e in calendar)


def _busy_month() -> list[dict]:
    """Twelve meetings every working day, as in the busiest real weeks: 325
    events over the thirty-seven days an hourly run lists."""
    first = datetime(2026, 6, 8)
    events = []
    for day in range(37):
        date = first + timedelta(days=day)
        if date.weekday() >= 5:
            continue
        events += [
            _event(f"d{day}-{n}", date + timedelta(hours=6, minutes=40 * n), 60 if n % 3 else 30)
            for n in range(12)
        ]
    events.append(_event("offsite", first + timedelta(days=9, hours=6), 3 * 24 * 60))
    return events


def test_a_busy_month_is_listed_inside_the_hourly_call_bound(monkeypatch, host_zone):
    """Splitting every full page into two whole halves took 146 to 168 calls
    here, over the bound of 120, so every busy week's hourly run exited 1,
    held its heartbeat and never marked a cancelled event."""
    calendar = _busy_month()
    calls: list = []
    monkeypatch.setattr(calendar_export, "run_outlook_cli", _fake_cli(calendar, calls))
    failures: list[str] = []

    events = calendar_export.list_events(
        datetime(2026, 6, 8), datetime(2026, 7, 15), failures=failures
    )

    assert failures == []
    assert sorted(e["Id"] for e in events) == sorted(e["Id"] for e in calendar)
    assert len(calls) <= calendar_export.DEFAULT_LIST_CALLS


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
