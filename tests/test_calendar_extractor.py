"""Tests for calendar event extraction."""

import json

from src.extract.calendar_extractor import parse_extraction_response


def test_parse_extraction_response_valid():
    """Test parsing a valid JSON response with all fields."""
    raw = json.dumps(
        {
            "body_summary": "Discussion about Q2 strategy and budget allocation",
            "decisions": [
                {
                    "decision": "Approved 15% increase in marketing budget",
                    "decided_by": "CFO",
                    "decision_date": "2026-03-15",
                }
            ],
            "action_items": [
                {
                    "task": "Prepare detailed budget breakdown",
                    "owner": "Finance team",
                    "deadline": "2026-03-20",
                }
            ],
        }
    )

    result = parse_extraction_response(raw)

    assert result["body_summary"] == "Discussion about Q2 strategy and budget allocation"
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["decision"] == "Approved 15% increase in marketing budget"
    assert len(result["action_items"]) == 1
    assert result["action_items"][0]["task"] == "Prepare detailed budget breakdown"


def test_parse_extraction_response_empty():
    """Test parsing valid JSON with empty fields."""
    raw = json.dumps({"body_summary": "", "decisions": [], "action_items": []})

    result = parse_extraction_response(raw)

    assert result["body_summary"] == ""
    assert result["decisions"] == []
    assert result["action_items"] == []


def test_parse_extraction_response_malformed():
    """Test parsing non-JSON input returns empty defaults."""
    raw = "This is not valid JSON at all"

    result = parse_extraction_response(raw)

    assert result["body_summary"] == ""
    assert result["decisions"] == []
    assert result["action_items"] == []


def test_parse_extraction_response_with_code_fence():
    """Test parsing JSON wrapped in markdown code fence."""
    raw = """```json
{
  "body_summary": "Team sync meeting",
  "decisions": [],
  "action_items": [
    {
      "task": "Update project timeline",
      "owner": "PM",
      "deadline": "Friday"
    }
  ]
}
```"""

    result = parse_extraction_response(raw)

    assert result["body_summary"] == "Team sync meeting"
    assert result["decisions"] == []
    assert len(result["action_items"]) == 1
    assert result["action_items"][0]["task"] == "Update project timeline"


# --- Regression: leading ThinkingBlock in the event-extraction response --------
#
# extract_event read ``response.content[0].text``. With extended thinking the model
# leads with a ThinkingBlock (.thinking, no .text), so the call raised
# "'ThinkingBlock' object has no attribute 'text'". 86 events failed that way
# between 2026-08-12 and 2026-08-24, on the same days as successful extractions.
# calendar_events has no llm_error column, so the only trace was calendar-sync.log.


class _ThinkingBlock:
    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, *blocks, stop_reason="end_turn") -> None:
        self.content = list(blocks)
        self.stop_reason = stop_reason


_EVENT = {
    "subject": "Q3 review",
    "organizer": "a@example.com",
    "attendees": [],
    "start_at": "2026-08-24",
}
_BODY = "Agenda: review the Q3 numbers, agree the budget, and assign the follow-ups. " * 2


def _patch_llm(monkeypatch, response):
    """calendar_extractor binds _get_client_and_model at MODULE level (line 5), so the
    interception point is the name in calendar_extractor, not the one in claude_extract.
    Patching the wrong one lets the real Vertex client be built and a real call go out."""

    class FakeMessages:
        def create(self, **kw):
            return response

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr("src.extract.calendar_extractor._get_client_and_model", lambda: (fake, "m"))


def test_extract_event_reads_past_a_leading_thinking_block(monkeypatch):
    """Mutation check: revert to content[0].text and this raises AttributeError."""
    from src.extract.calendar_extractor import extract_event

    _patch_llm(
        monkeypatch,
        _Response(
            _ThinkingBlock("skimming the agenda"),
            _TextBlock(
                '{"body_summary": "Q3 numbers and budget", "decisions": [], "action_items": []}'
            ),
        ),
    )

    result = extract_event(_EVENT, _BODY)

    assert result["body_summary"] == "Q3 numbers and budget"


def test_extract_event_still_parses_a_plain_text_response(monkeypatch):
    """No thinking block: unchanged behaviour."""
    from src.extract.calendar_extractor import extract_event

    _patch_llm(
        monkeypatch,
        _Response(_TextBlock('{"body_summary": "plain", "decisions": [], "action_items": []}')),
    )

    assert extract_event(_EVENT, _BODY)["body_summary"] == "plain"


def test_extract_event_raises_diagnosably_on_a_thinking_only_response(monkeypatch):
    """max_tokens spent on thinking: the caller must see WHY, not a bare message.

    extract_event deliberately does not swallow this. src/cli.py catches it per
    event and logs it, which is what put the 86 lines in calendar-sync.log; the
    message now names stop_reason and the block types so the residual after this
    fix can be counted from the same log.
    """
    import pytest

    from src.extract.calendar_extractor import extract_event

    _patch_llm(monkeypatch, _Response(_ThinkingBlock("..."), stop_reason="max_tokens"))

    with pytest.raises(ValueError, match="no text block"):
        extract_event(_EVENT, _BODY)
