"""An invitation is asked what a meeting is for; only a past meeting's notes give decisions.

The prompt asked every invite for decisions and action items, and got the agenda
back as both: one daily call's template, paraphrased 296 times, was 460 of the
519 calendar decisions (code-extract-11, data-coverage-05, data-quality-11). A
future meeting, or any occurrence of a series, is now read as an invitation:
purpose, agenda, who is expected. Only a single meeting already over, whose text
can hold notes, is asked for decisions, and only from notes.
"""

import json
import types
from datetime import UTC, datetime, timedelta

import pytest

from src.extract import calendar_extractor
from src.extract.calendar_extractor import event_prompt_hash, extract_event

_AGENDA = "Agenda: review the third quarter figures, agree the card budget, assign the follow-ups."


def _when(days: float) -> str:
    moment = datetime.now(UTC).replace(microsecond=0) + timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _meeting(days: float, **fields) -> dict:
    """An event as parse_event hands it to the extractor."""
    return {
        "outlook_event_id": "ev-1",
        "subject": "Quarterly review",
        "organizer_name": "Chair Person",
        "organizer_email": "chair@example.com",
        "start_at": _when(days),
        "end_at": _when(days + 1 / 24),
        "is_recurring": False,
        "recurrence_master_id": None,
        "attendees": [{"name": "Ana Example", "email": "ana@example.com"}],
        **fields,
    }


class _Model:
    """claude_extract's client: records each prompt, answers with `answer`."""

    def __init__(self, monkeypatch, answer: dict):
        self.prompts: list[str] = []
        model = self

        class Messages:
            def create(self, **kw):
                model.prompts.append(kw["messages"][0]["content"])
                text = types.SimpleNamespace(text=json.dumps(answer))
                return types.SimpleNamespace(content=[text], stop_reason="end_turn")

        client = types.SimpleNamespace(messages=Messages())
        monkeypatch.setattr(
            "src.extract.claude_extract._get_client_and_model", lambda: (client, "m")
        )


_INVITATION_ANSWER = {
    "purpose": "Review the quarter",
    "agenda": ["third quarter figures", "card budget"],
    "expected": "the division heads",
    # What the old prompt produced from an agenda, and what must not be stored.
    "decisions": [{"decision": "Hold the quarterly review"}],
    "action_items": [{"task": "Attend the review"}],
}


@pytest.mark.parametrize(
    "event",
    [
        _meeting(3),
        _meeting(-3, is_recurring=True, recurrence_master_id="series-1"),
    ],
    ids=["a meeting still to come", "an occurrence of a series"],
)
def test_an_invitation_gives_purpose_agenda_and_who_is_expected(monkeypatch, event):
    model = _Model(monkeypatch, _INVITATION_ANSWER)

    result = extract_event(event, _AGENDA)

    assert result["decisions"] == []
    assert result["action_items"] == []
    for piece in ("Review the quarter", "third quarter figures", "card budget", "division heads"):
        assert piece in result["body_summary"]
    prompt = model.prompts[0]
    assert '"purpose"' in prompt and '"agenda"' in prompt and '"expected"' in prompt
    assert '"decisions"' not in prompt


def test_a_past_meeting_is_asked_for_decisions_from_its_notes_only(monkeypatch):
    answer = {
        "body_summary": "The quarter was reviewed.",
        "decisions": [{"decision": "Raise the card budget", "decided_by": "Chair"}],
        "action_items": [{"task": "Send the figures", "owner": "Ana"}],
    }
    model = _Model(monkeypatch, answer)

    result = extract_event(_meeting(-3), "Minutes: the committee agreed to raise the budget.")

    assert result["decisions"] == answer["decisions"]
    assert result["action_items"] == answer["action_items"]
    prompt = model.prompts[0]
    assert '"decisions"' in prompt
    assert "notes or minutes" in prompt


def test_the_prompt_names_the_organiser():
    """It read event['organizer'], which parse_event never sets."""
    prompt = calendar_extractor._invite_text(_meeting(3), _AGENDA)

    assert "Organizer: Chair Person" in prompt


def test_the_prompt_names_thirty_attendees_and_counts_the_rest():
    crowd = [{"name": f"Member {k}", "email": f"m{k}@example.com"} for k in range(100)]

    prompt = calendar_extractor._invite_text(_meeting(3, attendees=crowd), _AGENDA)

    assert "Member 29" in prompt
    assert "Member 30" not in prompt
    assert "and 70 more" in prompt


def test_an_html_invitation_reaches_the_model_as_text(monkeypatch):
    model = _Model(monkeypatch, _INVITATION_ANSWER)
    html = f"<html><head><style>p {{margin:0}}</style></head><body><p>{_AGENDA}</p></body></html>"

    extract_event(_meeting(3), html)

    assert _AGENDA in model.prompts[0]
    assert "<p>" not in model.prompts[0]
    assert "margin" not in model.prompts[0]


_TEAMS_BLOCK = """\
________________________________________________________________________________
Microsoft Teams meeting
Join on your computer, mobile app or room device
Click here to join the meeting <https://teams.microsoft.com/l/meetup-join/abc>
Meeting ID: 123 456 789 012
Passcode: AbCdEf
Download Teams <https://www.microsoft.com/microsoft-teams/download-app> | Join on the web <https://www.microsoft.com/microsoft-teams/join-a-meeting>
Learn More <https://aka.ms/JoinTeamsMeeting> | Meeting options <https://teams.microsoft.com/meetingOptions/>
________________________________________________________________________________
"""


def test_an_invitation_with_only_joining_details_costs_no_call(monkeypatch):
    model = _Model(monkeypatch, _INVITATION_ANSWER)

    result = extract_event(_meeting(3), _TEAMS_BLOCK)

    assert model.prompts == []
    assert result == {"body_summary": "", "decisions": [], "action_items": []}
    assert event_prompt_hash(_meeting(3), _TEAMS_BLOCK) is None


def test_the_joining_details_are_left_out_and_the_agenda_kept(monkeypatch):
    model = _Model(monkeypatch, _INVITATION_ANSWER)

    extract_event(_meeting(3), _AGENDA + "\n\n" + _TEAMS_BLOCK)

    prompt = model.prompts[0]
    assert _AGENDA in prompt
    assert "Passcode" not in prompt
    assert "meetup-join" not in prompt


def test_occurrences_of_one_series_share_one_prompt():
    """So a series is read once per change of its invitation, not once a day."""
    monday = _meeting(-7, is_recurring=True, recurrence_master_id="series-1")
    tuesday = {**monday, "outlook_event_id": "ev-2", "start_at": _when(-6), "end_at": _when(-6)}

    assert event_prompt_hash(monday, _AGENDA) == event_prompt_hash(tuesday, _AGENDA)


def test_a_meeting_that_has_happened_is_read_again_as_a_meeting():
    """The prompt changes when the meeting passes, so its notes get asked about."""
    ahead = _meeting(3)
    over = {**ahead, "start_at": _when(-3), "end_at": _when(-3 + 1 / 24)}

    assert event_prompt_hash(ahead, _AGENDA) != event_prompt_hash(over, _AGENDA)


# ------------------------------------------------------------ through calendar-sync


def test_a_series_invitation_is_read_once_for_all_its_occurrences(monkeypatch, tmp_path):
    import sqlite3

    from src import cli
    from src.export import calendar_export
    from src.store.schema import create_database

    path = str(tmp_path / "brain.db")
    create_database(path).close()
    soon = datetime.now(UTC).replace(minute=0, second=0, microsecond=0, tzinfo=None)

    def occurrence(n: int) -> dict:
        start = soon + timedelta(days=n + 1)
        return {
            "Id": f"occ-{n}",
            "Subject": "Daily call",
            "Type": "Occurrence",
            "SeriesMasterId": "series-1",
            "Start": {"DateTime": start.strftime("%Y-%m-%dT%H:%M:%S.0000000"), "TimeZone": "UTC"},
            "End": {
                "DateTime": (start + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                "TimeZone": "UTC",
            },
            "Organizer": {"EmailAddress": {"Address": "chair@example.com", "Name": "Chair"}},
            "Attendees": [],
            "Body": {"Content": _AGENDA},
            "@odata.etag": "e1",
        }

    series = {f"occ-{n}": occurrence(n) for n in range(3)}

    def run(args):
        if args[0] == "get-event":
            return series[args[1]]
        return [{k: v for k, v in e.items() if k != "Body"} for e in series.values()]

    calls: list = []

    def extract(event, body):
        calls.append(event["outlook_event_id"])
        return {"body_summary": "Purpose: the daily call.", "decisions": [], "action_items": []}

    monkeypatch.setattr(calendar_export, "run_outlook_cli", run)
    monkeypatch.setattr(calendar_extractor, "extract_event", extract)
    args = types.SimpleNamespace(
        db=path, backfill=False, since=None, until=None, skip_extraction=False
    )

    assert cli.cmd_calendar_sync(args) == 0

    assert calls == ["occ-0"]
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT body_summary, llm_status, extraction_hash FROM calendar_events ORDER BY start_at"
    ).fetchall()
    conn.close()
    assert {row[:2] for row in rows} == {("Purpose: the daily call.", "extracted")}
    assert len({row[2] for row in rows}) == 1
