"""A calendar extraction has room for its reply, and a reply cut short says so.

The calendar call asked for at most 1024 tokens, which the model's thinking shares, while
every other extraction route allows 8192. A long invitation's reply was cut inside its JSON
('```json\\n{\\n  "body_summary": "This meeting is to ...'), failed as "not JSON", and was
recorded 'failed', which calendar-sync never offers again until the event is edited: one
meeting of 2026-02 stayed so from 2026-09-27 (audit 2026-10-20).
"""

import json
import types

import pytest

from src.extract import calendar_extractor
from src.extract.calendar_extractor import extract_event

_BODY = "Notes: the committee approved the card budget and asked finance for a forecast. " * 20


def _meeting() -> dict:
    return {
        "outlook_event_id": "ev-1",
        "subject": "Budget committee",
        "organizer_name": "Chair Person",
        "organizer_email": "chair@example.com",
        "start_at": "2026-02-04T09:00:00.0000000Z",
        "end_at": "2026-02-04T10:00:00.0000000Z",
        "is_recurring": False,
        "recurrence_master_id": None,
        "attendees": [{"name": "Ana Example", "email": "ana@example.com"}],
    }


class _Model:
    """claude_extract's client: records each call's max_tokens, answers with `text`."""

    def __init__(self, monkeypatch, text: str, stop_reason: str = "end_turn"):
        self.max_tokens: list[int] = []
        model = self

        class Messages:
            def create(self, **kw):
                model.max_tokens.append(kw["max_tokens"])
                block = types.SimpleNamespace(text=text)
                return types.SimpleNamespace(content=[block], stop_reason=stop_reason)

        client = types.SimpleNamespace(messages=Messages())
        monkeypatch.setattr(
            "src.extract.claude_extract._get_client_and_model", lambda: (client, "m")
        )


def test_the_calendar_call_allows_the_reply_room_the_other_routes_do(monkeypatch):
    answer = {"body_summary": "The committee approved the budget.", "decisions": []}
    model = _Model(monkeypatch, "```json\n" + json.dumps(answer) + "\n```")

    result = extract_event(_meeting(), _BODY)

    assert result["body_summary"] == "The committee approved the budget."
    assert (
        model.max_tokens == [calendar_extractor.MAX_TOKENS]
        and calendar_extractor.MAX_TOKENS >= 8192
    )


def test_a_reply_cut_at_max_tokens_says_so(monkeypatch):
    _Model(monkeypatch, '```json\n{\n  "body_summary": "This meeting is to', "max_tokens")

    with pytest.raises(ValueError, match="cut short at max_tokens"):
        extract_event(_meeting(), _BODY)
