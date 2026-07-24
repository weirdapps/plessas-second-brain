"""Extraction-robustness regressions (PR #4, Tier 2e).

Covers the email-body truncation cap and apostrophe-safe JSON parsing.
"""

import json

from src.extract.parser import parse_extraction
from src.extract.prompt import MAX_CONTENT_CHARS, build_extraction_prompt


class TestEmailContentCap:
    def test_long_body_is_truncated(self):
        email = {"content": "x" * (MAX_CONTENT_CHARS + 5000), "subject": "s"}
        prompt = build_extraction_prompt(email)
        assert "email body truncated" in prompt
        # The oversized body must not reach the LLM in full.
        assert prompt.count("x") <= MAX_CONTENT_CHARS + 50

    def test_short_body_untouched(self):
        email = {"content": "short body here", "subject": "s"}
        prompt = build_extraction_prompt(email)
        assert "short body here" in prompt
        assert "truncated" not in prompt

    def test_missing_content_does_not_crash(self):
        assert isinstance(build_extraction_prompt({"subject": "s"}), str)


class TestApostropheSafeParsing:
    def _full(self, summary: str) -> str:
        return json.dumps(
            {
                "summary": summary,
                "topics": [],
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

    def test_apostrophes_in_values_survive(self):
        result = parse_extraction(self._full("we don't know yet; it's Maria's call"))
        assert result["summary"] == "we don't know yet; it's Maria's call"

    def test_greek_text_with_apostrophe_survives(self):
        result = parse_extraction(self._full("ο κ. Παπαδόπουλος δεν είναι σίγουρος"))
        assert result["summary"] == "ο κ. Παπαδόπουλος δεν είναι σίγουρος"

    def test_truncated_json_with_apostrophes_salvaged_intact(self):
        # Truncated mid-array; the apostrophes in summary must not be mangled.
        raw = '{"summary": "it\'s Maria\'s decision", "topics": ["alpha", "bet'
        result = parse_extraction(raw)
        assert result["summary"] == "it's Maria's decision"

    def test_completely_invalid_json_still_raises(self):
        import pytest

        with pytest.raises(ValueError, match="Failed to parse JSON"):
            parse_extraction("this is not JSON at all")
