"""Calendar times say they are UTC, and the MCP answers in Athens days (teams-calendar-6).

Outlook returns every event with TimeZone 'UTC', since outlook-cli sends no
Prefer: outlook.timezone. parse_event kept the DateTime and dropped the zone, so
'2026-10-01T13:00:00.0000000' read as 13:00 for a 16:00 Athens meeting, and a
meeting at 00:30 Athens time was filed under the day before.
"""

import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src.config import CURRENT_SCHEMA_VERSION
from src.export.calendar_export import parse_event
from src.store.schema import create_database, get_schema_version, run_migrations

# ------------------------------------------------------------------ parse_event


def test_a_utc_time_is_stored_with_its_z():
    parsed = parse_event(
        {
            "Id": "e1",
            "Start": {"DateTime": "2026-10-01T13:00:00.0000000", "TimeZone": "UTC"},
            "End": {"DateTime": "2026-10-01T14:00:00.0000000", "TimeZone": "UTC"},
        }
    )

    assert parsed["start_at"] == "2026-10-01T13:00:00.0000000Z"
    assert parsed["end_at"] == "2026-10-01T14:00:00.0000000Z"


def test_a_time_already_marked_or_in_another_zone_is_left_as_it_came():
    parsed = parse_event(
        {
            "Id": "e1",
            "Start": {"DateTime": "2026-10-01T13:00:00Z", "TimeZone": "UTC"},
            "End": {"DateTime": "2026-10-01T16:00:00.0000000", "TimeZone": "GTB Standard Time"},
        }
    )

    assert parsed["start_at"] == "2026-10-01T13:00:00Z"
    assert parsed["end_at"] == "2026-10-01T16:00:00.0000000"


# ------------------------------------------------------------------ migration v25


def _insert(conn, event_id, start, end):
    conn.execute(
        "INSERT INTO calendar_events (outlook_event_id, start_at, end_at, ingested_at) "
        "VALUES (?, ?, ?, '2026-09-01T00:00:00')",
        (event_id, start, end),
    )


def _times(conn):
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT outlook_event_id, start_at, end_at FROM calendar_events")
    }


def test_v25_marks_the_stored_graph_times_as_utc(tmp_path):
    """An unchanged event is never fetched again, so without the migration every
    row stored before the fix would keep the bare form for good."""
    conn = create_database(str(tmp_path / "brain.db"))
    _insert(conn, "bare", "2026-10-01T13:00:00.0000000", "2026-10-01T14:00:00.0000000")
    _insert(conn, "marked", "2026-10-02T13:00:00.0000000Z", "2026-10-02T14:00:00.0000000Z")
    _insert(conn, "offset", "2026-10-03T16:00:00+03:00", "2026-10-03T17:00:00+03:00")
    conn.execute("UPDATE schema_version SET version = 24")
    conn.commit()

    run_migrations(conn)
    after_one = _times(conn)
    conn.execute("UPDATE schema_version SET version = 24")
    conn.commit()
    run_migrations(conn)

    assert after_one == {
        "bare": ("2026-10-01T13:00:00.0000000Z", "2026-10-01T14:00:00.0000000Z"),
        "marked": ("2026-10-02T13:00:00.0000000Z", "2026-10-02T14:00:00.0000000Z"),
        "offset": ("2026-10-03T16:00:00+03:00", "2026-10-03T17:00:00+03:00"),
    }
    assert _times(conn) == after_one, "a second run changes nothing"
    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION


def test_a_fresh_store_loads_the_same_shape_the_migration_leaves(tmp_path):
    from src.store.calendar_loader import load_event

    conn = create_database(str(tmp_path / "brain.db"))
    event = parse_event(
        {
            "Id": "e1",
            "Start": {"DateTime": "2026-10-01T13:00:00.0000000", "TimeZone": "UTC"},
            "End": {"DateTime": "2026-10-01T14:00:00.0000000", "TimeZone": "UTC"},
        }
    )
    load_event(conn, event, {}, llm_status="skipped")

    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert _times(conn)["e1"] == ("2026-10-01T13:00:00.0000000Z", "2026-10-01T14:00:00.0000000Z")


def test_the_z_form_still_answers_every_time_query_the_store_makes():
    """Readers compare start_at as text with 19-character bounds, cut it with
    substr, or hand it to SQLite's date functions. A 'Z' after the fraction
    changes none of them."""
    conn = sqlite3.connect(":memory:")
    value = "2026-09-27T06:15:00.0000000Z"

    assert conn.execute(
        "SELECT ? > '2026-09-27T06:14:59', ? < '2026-09-27T06:15:01', "
        "? >= '2026-09-27T06:15:00', substr(?, 1, 10), datetime(?), "
        "? GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'",
        (value,) * 6,
    ).fetchone() == (1, 1, 1, "2026-09-27", "2026-09-27 06:15:00", 1)


# ------------------------------------------------------------ query_calendar_events


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    from src import mcp_server
    from src.store.schema import get_connection

    path = str(tmp_path / "brain.db")
    conn = create_database(path)
    # A connection per call: the handler closes the one it is given.
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(path))
    return conn, mcp_server


def test_every_event_carries_its_athens_time_with_the_offset(mcp):
    conn, mcp_server = mcp
    _insert(conn, "summer", "2026-10-01T13:00:00.0000000Z", "2026-10-01T14:00:00.0000000Z")
    _insert(conn, "winter", "2026-12-01T13:00:00.0000000Z", "2026-12-01T14:00:00.0000000Z")
    conn.commit()

    events = {e["start_at"]: e for e in mcp_server.query_calendar_events()["events"]}

    summer = events["2026-10-01T13:00:00.0000000Z"]
    winter = events["2026-12-01T13:00:00.0000000Z"]
    assert (summer["start_local"], summer["end_local"]) == (
        "2026-10-01T16:00:00+03:00",
        "2026-10-01T17:00:00+03:00",
    )
    assert winter["start_local"] == "2026-12-01T15:00:00+02:00"


def test_a_bare_date_is_an_athens_day(mcp):
    """00:30 on 1 October in Athens is 21:30 UTC on 30 September."""
    conn, mcp_server = mcp
    _insert(conn, "late", "2026-09-30T21:30:00.0000000Z", "2026-09-30T22:00:00.0000000Z")
    _insert(conn, "evening", "2026-09-30T20:30:00.0000000Z", "2026-09-30T21:00:00.0000000Z")
    conn.commit()

    oct_1 = mcp_server.query_calendar_events(since="2026-10-01", until="2026-10-01")
    sep_30 = mcp_server.query_calendar_events(since="2026-09-30", until="2026-09-30")

    assert [e["start_local"] for e in oct_1["events"]] == ["2026-10-01T00:30:00+03:00"]
    assert [e["start_local"] for e in sep_30["events"]] == ["2026-09-30T23:30:00+03:00"]


def test_a_date_time_with_an_offset_is_taken_at_its_word(mcp):
    conn, mcp_server = mcp
    _insert(conn, "noon", "2026-10-01T12:00:00.0000000Z", "2026-10-01T13:00:00.0000000Z")
    conn.commit()

    hit = mcp_server.query_calendar_events(
        since="2026-10-01T15:00:00+03:00", until="2026-10-01T15:00:00+03:00"
    )
    miss = mcp_server.query_calendar_events(since="2026-10-01T12:00:01Z")

    assert (hit["count"], miss["count"]) == (1, 0)


@pytest.mark.parametrize("field", ["since", "until"])
@pytest.mark.parametrize("value", ["yesterday", "2026-13-01", "01/10/2026"])
def test_a_malformed_date_is_an_error_not_a_string_compare(mcp, field, value):
    _conn, mcp_server = mcp

    with pytest.raises(ToolError, match=field):
        mcp_server.query_calendar_events(**{field: value})


def test_the_limit_is_clamped(mcp):
    conn, mcp_server = mcp
    for n in range(205):
        _insert(conn, f"e{n}", f"2026-10-01T{n // 60:02d}:{n % 60:02d}:00Z", "2026-10-02T00:00:00Z")
    conn.commit()

    assert mcp_server.query_calendar_events(limit=0)["count"] == 1
    assert mcp_server.query_calendar_events(limit=-3)["count"] == 1
    # 200 events are found; the character budget may show fewer, and says so.
    out = mcp_server.query_calendar_events(limit=10_000)
    assert out.get("truncated", {}).get("events", {}).get("total", out["count"]) == 200


def test_the_docstring_says_the_times_are_utc():
    from src import mcp_server

    doc = mcp_server.query_calendar_events.__doc__ or ""

    assert "UTC" in doc and "start_local" in doc and "Athens" in doc
