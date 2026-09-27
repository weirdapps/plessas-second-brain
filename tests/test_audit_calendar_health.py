"""check_calendar warns when the week ahead holds no meeting.

The listing kept only the ten earliest events of each month, so the week ahead
was never stored while the row stayed green on a fresh heartbeat (mcp-1). The
owner has meetings every working week, so an empty week means the lookahead is
broken, whatever the heartbeat says.
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

HEALTH_CHECK_PATH = Path(__file__).resolve().parent.parent / "scripts" / "health_check.py"


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(*events: tuple[str, int]) -> sqlite3.Connection:
    """Freshly ingested events, each (start offset for SQLite's 'now', is_cancelled)."""
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE calendar_events (id INTEGER PRIMARY KEY, start_at TEXT, end_at TEXT, "
        "is_cancelled INTEGER NOT NULL DEFAULT 0, ingested_at TEXT)"
    )
    for offset, cancelled in events:
        db.execute(
            "INSERT INTO calendar_events (start_at, end_at, is_cancelled, ingested_at) VALUES "
            "(strftime('%Y-%m-%dT%H:%M:%S.0000000Z', 'now', ?), "
            "strftime('%Y-%m-%dT%H:%M:%S.0000000Z', 'now', ?, '+1 hour'), ?, "
            "strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
            (offset, offset, cancelled),
        )
    db.commit()
    return db


def test_a_meeting_in_the_week_ahead_is_ok(hc):
    db = _db(("-2 days", 0), ("+3 days", 0))

    result = hc.check_calendar(db)

    assert result["status"] == "OK"
    assert result["upcoming_7d"] == 1


def test_an_empty_week_ahead_warns_though_the_sync_is_fresh(hc):
    """A cancelled meeting is not one, and nor is one past the week."""
    db = _db(("-2 days", 0), ("+2 days", 1), ("+9 days", 0))

    result = hc.check_calendar(db)

    assert result["status"] == "WARN"
    assert result["upcoming_7d"] == 0
    assert "7 days" in result["note"]


def test_a_stale_calendar_is_still_stale(hc):
    db = _db(("+3 days", 0))
    db.execute(
        "UPDATE calendar_events SET ingested_at = strftime('%Y-%m-%dT%H:%M:%SZ','now','-9 days')"
    )

    assert hc.check_calendar(db)["status"] == "STALE"
