"""parse_extraction hands the loader lists of strings, and leaves valid JSON alone.

Two defects in the same function:

- Fields outside the schema (the conversation prompt's preferences_expressed and
  technical_decisions) were copied through unchecked. 35 conversation extractions
  answered technical_decisions as [{"decision": ...}], and the loader, which
  formats each item into "[TECHNICAL] {item}", stored the dict's Python repr as
  a key fact: 57 such rows reached the store. A null would have reached the
  loader as None to iterate, which stops every conversation load.
- The trailing-comma repair ran over every response before any strict parse. In
  valid JSON a comma before a closing bracket can only sit inside a string, so
  on valid input every match deleted a character of content.
"""

import json

import pytest

from src.extract.parser import parse_extraction


def _parse(**fields) -> dict:
    return parse_extraction(json.dumps(fields))


class TestStringListFields:
    def test_a_dict_decision_is_stored_as_its_text(self):
        out = _parse(technical_decisions=[{"decision": "chose SQLite"}, "kept FTS5"])

        assert out["technical_decisions"] == ["chose SQLite", "kept FTS5"]

    def test_a_dict_without_decision_falls_back_to_text_then_any_string(self):
        out = _parse(
            preferences_expressed=[
                {"text": "terse replies"},
                {"why": 3, "what": "active voice"},
                {"n": 1},
            ]
        )

        assert out["preferences_expressed"] == ["terse replies", "active voice"]

    @pytest.mark.parametrize("field", ["preferences_expressed", "technical_decisions"])
    def test_a_null_conversation_field_becomes_an_empty_list(self, field):
        out = _parse(summary="s", **{field: None})

        assert out[field] == []

    @pytest.mark.parametrize(
        "field",
        ["topics", "key_facts", "references", "preferences_expressed", "technical_decisions"],
    )
    def test_a_bare_string_becomes_one_item(self, field):
        out = _parse(**{field: "only one"})

        assert out[field] == ["only one"]

    def test_other_non_strings_are_dropped(self):
        out = _parse(key_facts=["a", 7, None, ["nested"], True, "b"])

        assert out["key_facts"] == ["a", "b"]

    def test_an_email_extraction_gains_no_conversation_fields(self):
        out = _parse(summary="s", topics=["t"])

        assert "preferences_expressed" not in out
        assert "technical_decisions" not in out

    def test_the_loader_stores_text_not_a_repr(self, tmp_path):
        """End to end: what the parser returns is what the loader writes."""
        from src.extract.parser import CONVERSATION_SENTIMENT_VALUES
        from src.store.loader import load_single_conversation
        from src.store.schema import create_database

        raw = json.dumps(
            {
                "summary": "s",
                "technical_decisions": [{"decision": "chose SQLite"}],
                "preferences_expressed": None,
            }
        )
        extraction = parse_extraction(raw, sentiment_values=CONVERSATION_SENTIMENT_VALUES)
        conn = create_database(str(tmp_path / "brain.db"))
        metadata = {
            "session_id": "session-1",
            "started_at": "2026-09-01T10:00:00",
            "ended_at": "2026-09-01T11:00:00",
            "turns": [{"speaker": "user", "content": "hello", "timestamp": ""}],
        }

        load_single_conversation(conn, metadata, extraction)

        facts = [r[0] for r in conn.execute("SELECT fact FROM key_facts")]
        assert facts == ["[TECHNICAL] chose SQLite"]


class TestValidJsonIsNotRepaired:
    def test_a_comma_before_a_bracket_inside_a_string_survives(self):
        raw = '{"summary": "tuple (a, ] and {x, } done", "topics": []}'

        assert parse_extraction(raw)["summary"] == "tuple (a, ] and {x, } done"

    def test_a_fenced_valid_response_is_parsed_as_is(self):
        raw = '```json\n{"summary": "arr = [1, 2, ]", "key_facts": ["x, }"]}\n```'

        out = parse_extraction(raw)

        assert out["summary"] == "arr = [1, 2, ]"
        assert out["key_facts"] == ["x, }"]

    def test_a_trailing_comma_is_still_repaired_when_strict_parsing_fails(self):
        raw = '{"summary": "s", "topics": ["a", "b",],}'

        out = parse_extraction(raw)

        assert out["summary"] == "s"
        assert out["topics"] == ["a", "b"]

    def test_a_missing_comma_between_lines_is_still_repaired(self):
        raw = '{"summary": "s"\n"topics": ["a"]}'

        out = parse_extraction(raw)

        assert out["summary"] == "s"
        assert out["topics"] == ["a"]
