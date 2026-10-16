"""The pre-meeting brief pilot: which meetings get a brief, the stop rule, owner-only mail.

Push nobody asked for is not read, so this is a two-week pilot that ends by itself: a
brief 30 to 45 minutes before a meeting with someone from outside or someone not met in
90 days, mailed to the owner and to no one else (src/pipeline/briefs.py).
"""

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.pipeline import briefs
from src.store.schema import create_database
from tests import factories as f

OWNER = ("Owner Example", "owner@example.com")
COLLEAGUE = ("Colleague Example", "colleague@example.com")
OUTSIDER = ("Outside Partner", "partner@partner.example.org")
PATTERN = "owner@example.com"
NOW = datetime(2026, 10, 20, 8, 30, 4, tzinfo=UTC)
ENV = {
    "BRAIN_BRIEF_PILOT_UNTIL": "2026-10-30",
    "HEALTH_EMAIL_TO": "owner@example.com",
}


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _at(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _meeting(conn, attendees, *, minutes=35, **columns):
    """A meeting `minutes` after NOW's quarter hour, the owner attending."""
    start = datetime(2026, 10, 20, 8, 30, tzinfo=UTC) + timedelta(minutes=minutes)
    event_id = f.calendar_event(
        conn, organizer=COLLEAGUE, attendees=attendees, start_at=_at(start), **columns
    )
    conn.execute(
        "INSERT INTO event_attendees (event_id, email, name, response_status, is_self)"
        " VALUES (?, ?, ?, 'Accepted', 1)",
        (event_id, OWNER[1], OWNER[0]),
    )
    conn.commit()
    return event_id


def _met(conn, contact, days_ago=10):
    """An earlier meeting the owner had with `contact`."""
    _meeting(conn, [contact], minutes=-days_ago * 24 * 60)


def _reply(monkeypatch, calls, text="WHO: someone\nOPEN ITEMS: none\nWATCH: nothing"):
    from src.extract import claude_extract

    def fake(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=8_000, output_tokens=400),
        )

    monkeypatch.setattr(claude_extract, "complete", fake)


def _run(store, tmp_path, *, env=ENV, now=NOW, dry_run=False):
    sent: list = []

    def send(to, subject, html_body):
        sent.append((to, subject, html_body))
        return True

    stats = briefs.run_briefs(
        store,
        now=now,
        environ=env,
        pattern=PATTERN,
        dry_run=dry_run,
        send=send,
        log=tmp_path / "state" / "briefs.jsonl",
        say=lambda _: None,
    )
    return stats, sent


# --------------------------------------------------------------- stop rule


@pytest.mark.parametrize(
    "env, today, running",
    [
        ({"BRAIN_BRIEF_PILOT_UNTIL": "2026-10-30"}, date(2026, 10, 30), True),
        ({"BRAIN_BRIEF_PILOT_UNTIL": "2026-10-30"}, date(2026, 10, 31), False),
        ({}, date(2026, 10, 20), False),
        ({"BRAIN_BRIEF_PILOT_UNTIL": "next month"}, date(2026, 10, 20), False),
        (
            {"BRAIN_BRIEF_PILOT_UNTIL": "2026-10-30", "BRAIN_BRIEFS_OFF": "1"},
            date(2026, 10, 20),
            False,
        ),
        (
            {"BRAIN_BRIEF_PILOT_UNTIL": "2026-10-30", "BRAIN_BRIEFS_OFF": "0"},
            date(2026, 10, 20),
            True,
        ),
    ],
)
def test_the_pilot_runs_only_until_its_date_and_never_with_the_switch_on(env, today, running):
    assert briefs.pilot_state(env, today)[0] is running


def test_after_its_date_nothing_is_called_or_sent(store, tmp_path, monkeypatch):
    calls: list = []
    _reply(monkeypatch, calls)
    _meeting(store, [OUTSIDER])

    stats, sent = _run(store, tmp_path, env={**ENV, "BRAIN_BRIEF_PILOT_UNTIL": "2026-10-19"})

    assert (calls, sent) == ([], [])
    assert stats["state"].startswith("pilot ended on 2026-10-19")


# ------------------------------------------------------------- owner only


@pytest.mark.parametrize(
    "to",
    ["", "colleague@example.com", "owner@example.com, colleague@example.com", "owner@evil.example"],
)
def test_it_mails_no_one_but_the_owner(store, tmp_path, monkeypatch, to):
    calls: list = []
    _reply(monkeypatch, calls)
    _meeting(store, [OUTSIDER])

    stats, sent = _run(store, tmp_path, env={**ENV, "HEALTH_EMAIL_TO": to})

    assert (calls, sent) == ([], [])
    assert stats["sent"] == 0


def test_a_brief_goes_to_the_owner_with_the_meeting_and_the_reply(store, tmp_path, monkeypatch):
    calls: list = []
    _reply(monkeypatch, calls, text="WHO: Outside Partner, last wrote on 2026-10-01")
    _meeting(store, [OUTSIDER], subject="Partnership review")

    stats, sent = _run(store, tmp_path)

    ((to, subject, body),) = sent
    assert to == "owner@example.com"
    assert subject == "brief: Partnership review"
    assert "last wrote on 2026-10-01" in body and "ends by itself, 2026-10-30" in body
    assert stats["sent"] == 1
    # One plain call through complete(): no structured output.
    (call,) = calls
    assert "format" not in call.get("output_config", {}) and call["system"] == briefs.SYSTEM


# ------------------------------------------------------------- which meetings


def test_a_meeting_with_someone_from_outside_qualifies(store):
    event_id = _meeting(store, [COLLEAGUE, OUTSIDER])
    _met(store, COLLEAGUE)
    _met(store, OUTSIDER)
    event = {"id": event_id, "is_recurring": 0}

    ok, reason, people = briefs.qualifies(store, event, PATTERN, NOW)

    assert ok and "external" in reason and people[0] == OUTSIDER[1]


def test_a_colleague_not_met_in_90_days_qualifies(store):
    event_id = _meeting(store, [COLLEAGUE])
    _met(store, COLLEAGUE, days_ago=120)

    ok, reason, _ = briefs.qualifies(store, {"id": event_id, "is_recurring": 0}, PATTERN, NOW)

    assert ok and "not met in 90 days" in reason


def test_a_colleague_met_recently_does_not(store):
    event_id = _meeting(store, [COLLEAGUE])
    _met(store, COLLEAGUE, days_ago=10)

    ok, reason, _ = briefs.qualifies(store, {"id": event_id, "is_recurring": 0}, PATTERN, NOW)

    assert not ok and reason == "only internal attendees met recently"


def test_a_recurring_internal_series_does_not(store):
    event_id = _meeting(store, [COLLEAGUE], is_recurring=1)

    ok, reason, _ = briefs.qualifies(store, {"id": event_id, "is_recurring": 1}, PATTERN, NOW)

    assert (ok, reason) == (False, "a recurring internal series")


def test_a_town_hall_does_not(store):
    crowd = [(f"Person {i}", f"p{i}@partner.example.org") for i in range(25)]
    event_id = _meeting(store, crowd)

    ok, reason, _ = briefs.qualifies(store, {"id": event_id, "is_recurring": 0}, PATTERN, NOW)

    assert not ok and "not a meeting to prepare" in reason


@pytest.mark.parametrize("minutes, picked", [(29, False), (30, True), (44, True), (45, False)])
def test_only_meetings_30_to_45_minutes_ahead_of_the_quarter_hour(store, minutes, picked):
    _meeting(store, [OUTSIDER], minutes=minutes)

    assert bool(briefs.upcoming(store, NOW)) is picked


def test_a_cancelled_or_declined_meeting_is_not_briefed(store):
    _meeting(store, [OUTSIDER], is_cancelled=1)
    _meeting(store, [OUTSIDER], response_status="Declined")

    assert briefs.upcoming(store, NOW) == []


# ---------------------------------------------------------------- the log


def test_each_brief_is_logged_with_its_meeting_and_cost_and_sent_once(store, tmp_path, monkeypatch):
    calls: list = []
    _reply(monkeypatch, calls)
    event_id = _meeting(store, [OUTSIDER])

    _run(store, tmp_path)
    stats, sent = _run(store, tmp_path)

    (line,) = (tmp_path / "state" / "briefs.jsonl").read_text().splitlines()
    record = json.loads(line)
    assert record["event_id"] == event_id and record["sent"] is True
    assert record["cost_usd"] > 0 and record["input_tokens"] == 8_000
    assert "subject" not in record
    assert (len(calls), sent, stats["sent"]) == (1, [], 0)
    assert (tmp_path / "state" / "briefs.jsonl").stat().st_mode & 0o077 == 0


def test_no_more_than_the_daily_cap(store, tmp_path, monkeypatch):
    calls: list = []
    _reply(monkeypatch, calls)
    monkeypatch.setattr(briefs, "MAX_PER_DAY", 1)
    _meeting(store, [OUTSIDER])
    _meeting(store, [("Other Partner", "other@partner.example.org")], minutes=40)

    stats, sent = _run(store, tmp_path)

    assert (stats["sent"], len(sent)) == (1, 1)


def test_a_dry_run_calls_nothing_and_sends_nothing(store, tmp_path, monkeypatch):
    calls: list = []
    _reply(monkeypatch, calls)
    _meeting(store, [OUTSIDER])

    stats, sent = _run(store, tmp_path, dry_run=True)

    assert (calls, sent, stats["qualified"]) == ([], [], 1)
    assert not (tmp_path / "state" / "briefs.jsonl").exists()
