"""Calendar answers list at most a dozen attendees, the owner and the organiser always.

Events average 48 attendees and reach 500, and one daily call's distribution
list was 41% of every attendee row (data-coverage-05). query_calendar_events
listed the first ten, so the organiser and the owner could be cut while
strangers were shown; meeting_prep listed no meeting at all.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src import mcp_server
from src.store.calendar_query import ATTENDEES_SHOWN, capped_attendees
from src.store.schema import create_database, get_connection


def _when(days: float) -> str:
    moment = datetime.now(UTC).replace(microsecond=0) + timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _event(conn, event_id: int, subject: str, days: float, attendees: list[tuple], **row) -> None:
    """attendees: (name, email, is_self, is_organizer), in the order Outlook lists them."""
    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "organizer_name, organizer_email, body_summary, is_recurring, is_cancelled, "
        "ingested_at, llm_status, response_status) VALUES "
        "(?, ?, ?, ?, ?, 'Chair', 'chair@example.com', ?, ?, ?, '2026-09-01', 'extracted', "
        "'Accepted')",
        (
            event_id,
            f"ev{event_id}",
            subject,
            _when(days),
            _when(days + 1 / 24),
            row.get("summary", "Purpose: review the plan."),
            row.get("recurring", 0),
            row.get("cancelled", 0),
        ),
    )
    conn.executemany(
        "INSERT INTO event_attendees (event_id, email, name, response_status, is_self, "
        "is_organizer) VALUES (?, ?, ?, 'Accepted', ?, ?)",
        [(event_id, email, name, me, org) for name, email, me, org in attendees],
    )


def _crowd(n: int, start: int = 0) -> list[tuple]:
    return [(f"Member {k}", f"m{k}@example.com", 0, 0) for k in range(start, start + n)]


_OWNER = ("Owner Person", "owner@example.com", 1, 0)
_CHAIR = ("Chair", "chair@example.com", 0, 1)
_ZETA = ("Zeta Late", "zeta@example.com", 0, 0)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    # Forty listed before the owner and the organiser, and Zeta last of all.
    _event(conn, 1, "Town hall", -2, [*_crowd(40), _OWNER, _CHAIR, *_crowd(20, 40), _ZETA])
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))
    return path


def _names(event: dict) -> list[str]:
    return [a["name"] for a in event["attendees"]]


def test_an_event_lists_a_dozen_with_the_owner_and_the_organiser(db):
    event = mcp_server.query_calendar_events(keyword="town")["events"][0]

    assert ATTENDEES_SHOWN == 12
    assert event["attendees_total"] == 63
    assert len(event["attendees"]) == 12
    assert {"Owner Person", "Chair"} <= set(_names(event))


def test_the_person_asked_about_is_listed_first_and_the_owner_still_kept(db):
    event = mcp_server.query_calendar_events(person="Zeta")["events"][0]

    names = _names(event)
    assert names[0] == "Zeta Late"
    assert {"Owner Person", "Chair"} <= set(names)
    assert len(names) == 12


def test_the_owner_and_organiser_are_kept_when_more_than_the_cap_match(db):
    """`person` matching every member must not push the two out."""
    event = mcp_server.query_calendar_events(person="example.com")["events"][0]

    assert {"Owner Person", "Chair"} <= set(_names(event))
    assert len(event["attendees"]) == 12


@pytest.mark.parametrize(("cap", "listed"), [(0, 0), (2, 2), (100, 63)])
def test_the_cap_is_the_callers_to_set(db, cap, listed):
    event = mcp_server.query_calendar_events(keyword="town", max_attendees=cap)["events"][0]

    assert len(event["attendees"]) == listed
    assert event["attendees_total"] == 63
    if cap == 2:
        assert set(_names(event)) == {"Owner Person", "Chair"}


def test_capped_attendees_keeps_the_addresses_it_is_given(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    _event(conn, 1, "Review", 1, [*_crowd(30), _OWNER, _ZETA])

    listed, total = capped_attendees(conn, 1, limit=3, emails=["ZETA@example.com"])

    assert total == 32
    assert [a["name"] for a in listed] == ["Zeta Late", "Owner Person", "Member 0"]


# ------------------------------------------------------------ meeting_prep


@pytest.fixture
def prep(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.executescript(
        """
        INSERT INTO people (id, name, email) VALUES (1, 'Ana Example', 'ana@example.com');
        INSERT INTO people (id, name, email) VALUES (2, 'Ben Example', 'ben@example.com');
        """
    )
    ana = ("Ana Example", "ana@example.com", 0, 0)
    ben = ("Ben Example", "ben@example.com", 0, 0)
    _event(conn, 1, "Daily call", 1, [*_crowd(40), ana, _OWNER, _CHAIR], recurring=1)
    _event(conn, 2, "Budget review", 6, [_CHAIR, ana, ben, _OWNER, *_crowd(30)])
    _event(conn, 3, "Kick-off", -3, [_CHAIR, ana, _OWNER])
    _event(conn, 4, "Old review", -40, [_CHAIR, ana, ben, _OWNER])
    _event(conn, 5, "Moved away", 2, [_CHAIR, ana, ben, _OWNER], cancelled=1)
    _event(conn, 6, "Unrelated", 1, [_CHAIR, _OWNER])
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))
    return path


def test_meeting_prep_names_the_meetings_shared_with_them(prep):
    out = mcp_server.meeting_prep("ana@example.com, ben@example.com")

    meetings = out["meetings"]
    # The one with both of them first, then the soonest; a cancelled one never.
    assert [m["subject"] for m in meetings["next"]] == ["Budget review", "Daily call"]
    assert [m["subject"] for m in meetings["last"]] == ["Old review", "Kick-off"]


def test_meeting_prep_caps_each_meetings_attendees(prep):
    out = mcp_server.meeting_prep("ana@example.com")

    daily = next(m for m in out["meetings"]["next"] if m["subject"] == "Daily call")
    assert daily["attendees_total"] == 43
    assert len(daily["attendees"]) == ATTENDEES_SHOWN
    names = [a["name"] for a in daily["attendees"]]
    assert names[0] == "Ana Example"
    assert {"Owner Person", "Chair"} <= set(names)
    assert daily["is_recurring"] is True
    assert daily["start_local"].endswith(("+02:00", "+03:00"))
    assert daily["body_summary"] == "Purpose: review the plan."


def test_meeting_prep_with_nobody_resolved_lists_no_meetings(prep):
    out = mcp_server.meeting_prep("Nobody At All")

    assert out["meetings"] == {"next": [], "last": []}
