"""The plain calendar-sync looks further back once a day, for events changed after their week.

A plain run lists the week behind and the 30 days ahead, so an event edited once it had left
that week (a series edited, notes added to a meeting gone by) kept its old etag and content
for good: 15 of the 215 events of the 14 days around 2026-10-20. Once every
DEEP_REFRESH_HOURS a plain run also lists the DEEP_LOOKBACK_DAYS before the week, and fetches
at most DEEP_FETCH_CAP changed events there.
"""

import sqlite3
import types
from datetime import UTC, datetime, timedelta

import pytest

from src.export import calendar_export
from src.pipeline import calendar_sync
from src.store.schema import create_database
from tests.pipeline.test_calendar_sync import EXTRACTION, Outlook, _event
from tests.test_audit_calendar_listing import _FMT, _utc

KEY = calendar_sync.DEEP_REFRESH_KEY


def _older(event_id: str, days: int, etag: str = "e1") -> dict:
    """An event `days` ago, outside the plain run's week."""
    event = _event(event_id, etag)
    start = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(days=days)
    event["Start"]["DateTime"] = start.strftime(_FMT)
    event["End"]["DateTime"] = (start + timedelta(hours=1)).strftime(_FMT)
    return event


class Listing(Outlook):
    """Outlook that also records the start of every window listed."""

    def __init__(self, events):
        super().__init__(events)
        self.listed_from: list[datetime] = []
        self.failing_before: datetime | None = None

    def __call__(self, args):
        if args[0] != "get-event":
            start = _utc(args[args.index("--from") + 1])
            self.listed_from.append(start)
            if self.failing_before is not None and start < self.failing_before:
                raise calendar_sync_error()
        return super().__call__(args)


def calendar_sync_error():
    from src.export.outlook_cli import OutlookCliError

    return OutlookCliError(5, "upstream", retryable=True)


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    outlook = Listing([_event("ev1"), _older("old1", 20), _older("old2", 21)])
    extracted: list[str] = []

    def extract(event, body_html):
        extracted.append(event["outlook_event_id"])
        return EXTRACTION

    monkeypatch.setattr(calendar_export, "run_outlook_cli", outlook)
    monkeypatch.setattr("src.extract.calendar_extractor.extract_event", extract)
    monkeypatch.setattr(calendar_sync, "DATA_ROOT", tmp_path)

    def run(**options):
        args = {
            "db": str(db),
            "backfill": False,
            "since": None,
            "until": None,
            "skip_extraction": False,
            "dry_run": False,
        }
        return calendar_sync.run(types.SimpleNamespace(**{**args, **options}))

    def stamp(value=None):
        conn = sqlite3.connect(str(db))
        try:
            if value is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)", (KEY, value)
                )
                conn.commit()
            row = conn.execute("SELECT value FROM sync_metadata WHERE key = ?", (KEY,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def stored_etag(event_id):
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute(
                "SELECT change_key FROM calendar_events WHERE outlook_event_id = ?", (event_id,)
            ).fetchone()[0]
        finally:
            conn.close()

    # Every event stored once, by a run over a month, which is not a plain run.
    since = (datetime.now() - timedelta(days=25)).date().isoformat()
    assert run(since=since) == 0
    outlook.fetched.clear()
    extracted.clear()
    outlook.listed_from.clear()
    return outlook, extracted, run, stamp, stored_etag


def _hours_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def test_the_first_plain_run_lists_the_week_only_and_starts_the_clock(calendar):
    outlook, _, run, stamp, _ = calendar
    assert stamp() is None

    assert run() == 0

    week_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
    assert all(start > week_ago for start in outlook.listed_from)
    assert stamp() is not None


def test_a_due_refresh_fetches_an_older_event_that_changed(calendar):
    outlook, extracted, run, stamp, stored_etag = calendar
    stamp(_hours_ago(25))
    outlook.events[1] = _older("old1", 20, etag="e2")

    assert run() == 0

    assert outlook.fetched == ["old1"]
    # The same invitation: the extraction on record stands, the event's facts are new.
    assert extracted == []
    assert stored_etag("old1") == "e2"
    assert datetime.fromisoformat(stamp()) > datetime.now(UTC) - timedelta(minutes=5)


def test_a_refresh_done_today_is_not_repeated(calendar):
    outlook, extracted, run, stamp, stored_etag = calendar
    stamp(_hours_ago(1))
    outlook.events[1] = _older("old1", 20, etag="e2")

    assert run() == 0

    assert outlook.fetched == [] and extracted == []


def test_the_cap_leaves_the_rest_to_the_next_run(calendar, monkeypatch):
    outlook, extracted, run, stamp, stored_etag = calendar
    monkeypatch.setattr(calendar_sync, "DEEP_FETCH_CAP", 1)
    before = _hours_ago(25)
    stamp(before)
    outlook.events[1] = _older("old1", 20, etag="e2")
    outlook.events[2] = _older("old2", 21, etag="e2")

    assert run() == 0
    assert len(outlook.fetched) == 1
    assert stamp() == before  # not done: due again on the next run

    assert run() == 0
    assert sorted(outlook.fetched) == ["old1", "old2"]
    assert stamp() != before


def test_a_deep_listing_that_fails_does_not_fail_the_run(calendar):
    outlook, _, run, stamp, _ = calendar
    before = _hours_ago(25)
    stamp(before)
    outlook.failing_before = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)

    assert run() == 0

    assert stamp() == before
