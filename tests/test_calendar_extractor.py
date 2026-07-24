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
