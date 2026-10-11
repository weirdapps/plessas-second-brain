"""`brain calendar-sync` stage by stage: list, fetch, extract, load, and what each failure
leaves behind.

outlook-cli and the model are fakes; the store is a temporary one. These read the
orchestrator alone (src/pipeline/calendar_sync.py).
"""

import sqlite3
import types
from datetime import datetime, timedelta

import pytest

from src.export import calendar_export
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError
from src.pipeline import EXIT_REAUTH, calendar_sync
from src.store.schema import create_database
from tests.test_audit_calendar_listing import _FMT, _span, _utc

BODY = "Agenda: review the okapi plan, agree the budget, and assign the follow-ups. " * 2
EXTRACTION = {"body_summary": "Reviewed the okapi plan", "decisions": [], "action_items": []}


def _event(event_id: str, etag: str = "e1") -> dict:
    start = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    return {
        "Id": event_id,
        "Subject": f"Okapi {event_id}",
        "Start": {"DateTime": start.strftime(_FMT), "TimeZone": "UTC"},
        "End": {"DateTime": (start + timedelta(hours=1)).strftime(_FMT), "TimeZone": "UTC"},
        "Organizer": {"EmailAddress": {"Name": "Olga Organiser", "Address": "olga@example.com"}},
        "Attendees": [{"EmailAddress": {"Name": "Alice Smith", "Address": "alice@example.com"}}],
        "ResponseStatus": {"Response": "accepted"},
        "@odata.etag": etag,
        "Body": {"Content": BODY},
    }


class Outlook:
    """outlook-cli as calendar_export calls it: list-calendar over a window, get-event by id."""

    def __init__(self, events):
        self.events = events
        self.fetched: list[str] = []
        self.unfetchable: set[str] = set()
        self.listing_error: Exception | None = None

    def __call__(self, args):
        if args[0] == "get-event":
            self.fetched.append(args[1])
            if args[1] in self.unfetchable:
                raise OutlookCliError(5, "upstream", retryable=True)
            return next(e for e in self.events if e["Id"] == args[1])
        if self.listing_error is not None:
            raise self.listing_error
        start, end = _utc(args[args.index("--from") + 1]), _utc(args[args.index("--to") + 1])
        return [
            {k: v for k, v in e.items() if k != "Body"}
            for e in self.events
            if _span(e)[0] < end and _span(e)[1] > start
        ]


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    """(Outlook, the events extracted, run, the stored rows, sync_metadata)."""
    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    outlook = Outlook([_event("ev1")])
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

    def query(sql):
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def rows():
        return {
            r[0]: (r[1], r[2])
            for r in query("SELECT outlook_event_id, llm_status, is_cancelled FROM calendar_events")
        }

    def metadata():
        return dict(query("SELECT key, value FROM sync_metadata"))

    return outlook, extracted, run, rows, metadata


def test_a_listed_event_is_fetched_extracted_and_loaded(calendar):
    outlook, extracted, run, rows, metadata = calendar

    assert run() == 0

    assert outlook.fetched == ["ev1"]
    assert extracted == ["ev1"]
    assert rows() == {"ev1": ("extracted", 0)}
    assert "calendar_last_listed" in metadata()


def test_an_event_listed_unchanged_is_neither_fetched_nor_extracted_again(calendar):
    outlook, extracted, run, _, _ = calendar
    run()

    assert run() == 0

    assert outlook.fetched == ["ev1"]
    assert extracted == ["ev1"]


def test_a_body_that_cannot_be_fetched_leaves_the_store_alone_and_fails_the_run(calendar):
    outlook, extracted, run, rows, metadata = calendar
    outlook.unfetchable = {"ev1"}

    assert run() == 1

    assert extracted == []
    assert rows() == {}
    assert "calendar_last_listed" not in metadata()


def test_a_listing_that_needs_a_login_writes_nothing_and_asks_for_one(calendar):
    outlook, _, run, rows, metadata = calendar
    outlook.listing_error = OutlookCliAuthRequired("session expired")

    assert run() == EXIT_REAUTH

    assert outlook.fetched == []
    assert rows() == {}
    assert "calendar_last_listed" not in metadata()


def test_a_dry_run_lists_and_counts_and_writes_nothing(calendar):
    outlook, extracted, run, rows, metadata = calendar

    assert run(dry_run=True) == 0

    assert (outlook.fetched, extracted, rows()) == ([], [], {})
    assert "calendar_last_listed" not in metadata()


def test_an_event_outlook_no_longer_lists_is_marked_cancelled(calendar):
    """One of three: the loader keeps a window that loses more than half at once."""
    outlook, _, run, rows, _ = calendar
    outlook.events = [_event("ev1"), _event("ev2"), _event("ev3")]
    run()
    outlook.events = outlook.events[:2]

    assert run() == 0

    assert rows() == {
        "ev1": ("extracted", 0),
        "ev2": ("extracted", 0),
        "ev3": ("extracted", 1),
    }
