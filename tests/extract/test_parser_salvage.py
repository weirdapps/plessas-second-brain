"""Tests for truncated-JSON salvage in parser.parse_extraction.

Covers the attachment failure mode where the LLM hits max_tokens and the JSON
stops mid-structure (stop_reason=max_tokens → "Expecting ',' delimiter").
"""

from src.extract.parser import _salvage_truncated_json, parse_extraction


def test_salvage_recovers_truncated_mid_value_string():
    # Real pattern: cut off mid-string inside the key_facts array.
    raw = (
        '{"summary": "A dense status report.", "topics": ["retail", "digital"], '
        '"key_facts": ["fact one", "fact two was cut off right here'
    )
    out = parse_extraction(raw)
    assert out["summary"] == "A dense status report."
    assert out["topics"] == ["retail", "digital"]
    assert "fact one" in out["key_facts"]


def test_salvage_recovers_trailing_comma_truncation():
    raw = '{"summary": "S", "topics": ["a", "b"],'
    out = parse_extraction(raw)
    assert out["summary"] == "S"
    assert out["topics"] == ["a", "b"]


def test_salvage_recovers_dangling_key_colon():
    raw = '{"summary": "S", "topics": ["a"], "urgency":'
    out = parse_extraction(raw)
    assert out["summary"] == "S"
    assert out["topics"] == ["a"]


def test_salvage_recovers_nested_array_of_objects():
    raw = (
        '{"summary": "S", "action_items": ['
        '{"task": "do X", "owner": "ME", "deadline": "2026-01-01"}, '
        '{"task": "do Y was truncated'
    )
    out = parse_extraction(raw)
    assert out["summary"] == "S"
    assert out["action_items"][0]["task"] == "do X"


def test_salvage_returns_none_for_balanced_non_truncation():
    # Bracket-balanced but invalid for a different reason (missing comma between
    # values) — not a truncation, so salvage must decline (caller keeps the error).
    assert _salvage_truncated_json('{"a": 1 "b": 2}') is None


def test_valid_json_is_unaffected():
    raw = '{"summary": "ok", "topics": [], "urgency": "high", "language": "english"}'
    out = parse_extraction(raw)
    assert out["summary"] == "ok"
    assert out["urgency"] == "high"
