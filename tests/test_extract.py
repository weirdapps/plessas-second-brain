"""
Tests for extraction pipeline (prompt, parser, batch).
"""

import json

import pytest

from src.extract.parser import (
    CONVERSATION_SENTIMENT_VALUES,
    parse_extraction,
    validate_extraction,
)
from src.extract.prompt import build_extraction_prompt


class TestPromptBuilder:
    """Test extraction prompt building."""

    def test_basic_email_prompt(self):
        """Test prompt generation for basic email."""
        email = {
            "message_id": "test123",
            "date_received": "2026-03-15T10:30:00Z",
            "sender": "john@example.com",
            "to_recipients": ["jane@example.com"],
            "cc_recipients": [],
            "subject": "Project update",
            "content": "The project is on track. We'll deliver next week.",
            "mailbox_name": "Inbox",
        }

        prompt = build_extraction_prompt(email)

        # Check metadata is included
        assert "john@example.com" in prompt
        assert "jane@example.com" in prompt
        assert "Project update" in prompt
        assert "2026-03-15T10:30:00Z" in prompt

        # Check content is included
        assert "The project is on track" in prompt

        # Check JSON schema is described
        assert "summary" in prompt
        assert "topics" in prompt
        assert "decisions" in prompt
        assert "action_items" in prompt
        assert "sentiment" in prompt
        assert "urgency" in prompt

    def test_greek_email_prompt(self):
        """Test prompt handles Greek content."""
        email = {
            "message_id": "gr123",
            "date_received": "2026-03-15T10:30:00Z",
            "sender": "nikos@example.com",
            "to_recipients": ["team@example.com"],
            "cc_recipients": [],
            "subject": "Ενημέρωση",
            "content": "Καλημέρα, η ανάλυση είναι έτοιμη.",
            "mailbox_name": "Archive",
        }

        prompt = build_extraction_prompt(email)

        # Check Greek content is preserved
        assert "Ενημέρωση" in prompt
        assert "Καλημέρα" in prompt
        assert "ανάλυση" in prompt

    def test_multiple_recipients(self):
        """Test prompt with multiple To and CC recipients."""
        email = {
            "message_id": "multi123",
            "date_received": "2026-03-15T10:30:00Z",
            "sender": "boss@example.com",
            "to_recipients": ["alice@example.com", "bob@example.com", "charlie@example.com"],
            "cc_recipients": ["dave@example.com", "eve@example.com"],
            "subject": "Team announcement",
            "content": "Important update for everyone.",
            "mailbox_name": "Inbox",
        }

        prompt = build_extraction_prompt(email)

        # Check all recipients are listed
        assert "alice@example.com" in prompt
        assert "bob@example.com" in prompt
        assert "charlie@example.com" in prompt
        assert "dave@example.com" in prompt
        assert "eve@example.com" in prompt

    def test_missing_optional_fields(self):
        """Test prompt with minimal email data."""
        email = {
            "message_id": "minimal123",
            "content": "Just the content.",
        }

        prompt = build_extraction_prompt(email)

        # Should not crash, should use N/A for missing fields
        assert "N/A" in prompt
        assert "Just the content" in prompt

    def test_dict_format_sender_and_recipients(self):
        """Test prompt handles dict-format sender/recipients from export pipeline."""
        email = {
            "message_id": "dict123",
            "date_received": "2026-03-15T10:30:00Z",
            "sender": {"name": "John Doe", "address": "john@example.com"},
            "to_recipients": [
                {"name": "Jane Smith", "address": "jane@example.com"},
                {"name": "Bob", "address": "bob@example.com"},
            ],
            "cc_recipients": [
                {"name": "Carol", "address": "carol@example.com"},
            ],
            "subject": "Test",
            "content": "Hello world.",
        }

        prompt = build_extraction_prompt(email)

        assert "John Doe" in prompt
        assert "john@example.com" in prompt
        assert "jane@example.com" in prompt
        assert "bob@example.com" in prompt
        assert "carol@example.com" in prompt


class TestParser:
    """Test JSON parsing and validation."""

    def test_parse_clean_json(self):
        """Test parsing clean JSON response."""
        response = json.dumps(
            {
                "summary": "Project status update",
                "topics": ["project-alpha", "delivery"],
                "decisions": [],
                "action_items": [],
                "commitments": [],
                "people_roles": {},
                "sentiment": "informational",
                "urgency": "low",
                "language": "english",
                "key_facts": [],
                "references": [],
            }
        )

        result = parse_extraction(response)

        assert result["summary"] == "Project status update"
        assert result["topics"] == ["project-alpha", "delivery"]
        assert result["sentiment"] == "informational"
        assert result["urgency"] == "low"
        assert result["language"] == "english"

    def test_parse_markdown_wrapped_json(self):
        """Test parsing JSON wrapped in markdown code blocks."""
        response = """```json
{
  "summary": "Test",
  "topics": [],
  "decisions": [],
  "action_items": [],
  "commitments": [],
  "people_roles": {},
  "sentiment": "informational",
  "urgency": "low",
  "language": "english",
  "key_facts": [],
  "references": []
}
```"""

        result = parse_extraction(response)
        assert result["summary"] == "Test"

    def test_parse_with_trailing_commas(self):
        """Test parsing JSON with trailing commas."""
        response = """{
  "summary": "Test",
  "topics": ["a", "b",],
  "decisions": [],
  "action_items": [],
  "commitments": [],
  "people_roles": {},
  "sentiment": "informational",
  "urgency": "low",
  "language": "english",
  "key_facts": [],
  "references": [],
}"""

        result = parse_extraction(response)
        assert result["summary"] == "Test"
        assert result["topics"] == ["a", "b"]

    def test_parse_applies_defaults(self):
        """Test that missing fields get default values."""
        response = json.dumps({"summary": "Minimal response"})

        result = parse_extraction(response)

        # Check defaults are applied
        assert result["summary"] == "Minimal response"
        assert result["topics"] == []
        assert result["decisions"] == []
        assert result["action_items"] == []
        assert result["commitments"] == []
        assert result["people_roles"] == {}
        assert result["sentiment"] == "informational"
        assert result["urgency"] == "low"
        assert result["language"] == "english"

    def test_parse_normalizes_enums(self):
        """Test that enum values are normalized to lowercase."""
        response = json.dumps(
            {
                "summary": "Test",
                "topics": [],
                "decisions": [],
                "action_items": [],
                "commitments": [],
                "people_roles": {},
                "sentiment": "DIRECTIVE",
                "urgency": "HIGH",
                "language": "GREEK",
                "key_facts": [],
                "references": [],
            }
        )

        result = parse_extraction(response)

        assert result["sentiment"] == "directive"
        assert result["urgency"] == "high"
        assert result["language"] == "greek"

    def test_parse_invalid_json_raises(self):
        """Test that completely invalid JSON raises ValueError."""
        response = "This is not JSON at all"

        with pytest.raises(ValueError, match="Failed to parse JSON"):
            parse_extraction(response)

    def test_validate_complete_extraction(self):
        """Test validation of complete, valid extraction."""
        data = {
            "summary": "Test email",
            "topics": ["test"],
            "decisions": [{"decision": "We decided to test", "decided_by": "Team"}],
            "action_items": [{"task": "Write tests", "owner": "Dev", "deadline": "2026-03-20"}],
            "commitments": [
                {
                    "commitment": "Will deliver by Friday",
                    "by": "Alice",
                    "to": "Bob",
                }
            ],
            "people_roles": {"Alice": "developer", "Bob": "reviewer"},
            "sentiment": "collaborative",
            "urgency": "medium",
            "language": "english",
            "key_facts": ["Tests are important"],
            "references": [],
        }

        is_valid, issues = validate_extraction(data)

        assert is_valid
        assert len(issues) == 0

    def test_validate_missing_fields(self):
        """Test validation catches missing required fields."""
        data = {
            "summary": "Incomplete",
            # Missing most fields
        }

        is_valid, issues = validate_extraction(data)

        assert not is_valid
        assert len(issues) > 0
        assert any("Missing required field" in issue for issue in issues)

    def _base_extraction(self, sentiment: str) -> str:
        """Minimal valid extraction JSON with the given sentiment."""
        return json.dumps(
            {
                "summary": "Session",
                "topics": [],
                "decisions": [],
                "action_items": [],
                "commitments": [],
                "people_roles": {},
                "sentiment": sentiment,
                "urgency": "low",
                "language": "english",
                "key_facts": [],
                "references": [],
            }
        )

    def test_parse_conversation_sentiment_accepted(self):
        """Conversation sentiments (productive/planning/…) are accepted, not coerced."""
        for sentiment in CONVERSATION_SENTIMENT_VALUES:
            result = parse_extraction(
                self._base_extraction(sentiment),
                sentiment_values=CONVERSATION_SENTIMENT_VALUES,
                sentiment_default="exploratory",
            )
            assert result["sentiment"] == sentiment

    def test_parse_email_sentiment_unaffected_by_conversation_values(self):
        """Email path (default vocab) still coerces a conversation-only sentiment."""
        result = parse_extraction(self._base_extraction("productive"))
        assert result["sentiment"] == "informational"

    def test_validate_conversation_sentiment(self):
        """validate_extraction accepts conversation sentiment when given that vocab."""
        data = json.loads(self._base_extraction("planning"))
        is_valid, issues = validate_extraction(data, sentiment_values=CONVERSATION_SENTIMENT_VALUES)
        assert is_valid
        assert not any("sentiment" in issue.lower() for issue in issues)

    def test_validate_wrong_types(self):
        """Test validation catches type mismatches."""
        data = {
            "summary": "Test",
            "topics": "should be list",  # Wrong type
            "decisions": [],
            "action_items": [],
            "commitments": [],
            "people_roles": {},
            "sentiment": "informational",
            "urgency": "low",
            "language": "english",
            "key_facts": [],
            "references": [],
        }

        is_valid, issues = validate_extraction(data)

        assert not is_valid
        assert any("wrong type" in issue for issue in issues)

    def test_validate_invalid_enum_values(self):
        """Test validation catches invalid enum values."""
        data = {
            "summary": "Test",
            "topics": [],
            "decisions": [],
            "action_items": [],
            "commitments": [],
            "people_roles": {},
            "sentiment": "invalid_sentiment",
            "urgency": "invalid_urgency",
            "language": "invalid_language",
            "key_facts": [],
            "references": [],
        }

        is_valid, issues = validate_extraction(data)

        assert not is_valid
        assert any("sentiment" in issue for issue in issues)
        assert any("urgency" in issue for issue in issues)
        assert any("language" in issue for issue in issues)

    def test_validate_nested_structures(self):
        """Test validation of nested decision/action/commitment structures."""
        data = {
            "summary": "Test",
            "topics": [],
            "decisions": [
                {"decision": "Good decision", "decided_by": "Boss"},
                {"decision": "Missing decided_by"},  # Invalid
            ],
            "action_items": [
                {
                    "task": "Do thing",
                    "owner": "Alice",
                    "deadline": None,
                },  # Valid (null deadline ok)
                {"task": "Missing owner and deadline"},  # Invalid
            ],
            "commitments": [
                {"commitment": "Promise", "by": "Alice"},  # Missing 'to'
            ],
            "people_roles": {},
            "sentiment": "informational",
            "urgency": "low",
            "language": "english",
            "key_facts": [],
            "references": [],
        }

        is_valid, issues = validate_extraction(data)

        assert not is_valid
        assert any("decisions[1]" in issue for issue in issues)
        assert any("action_items[1]" in issue for issue in issues)
        assert any("commitments[0]" in issue for issue in issues)
