"""Schema v2 of the extraction JSON, and its fit with structured outputs.

Structured outputs reject a schema that breaks their rules with a 400, which would fail
every v2 call: so the rules are checked here, on the schemas as they are sent.
"""

import pytest

from src.extract import schema_v2
from src.extract.parser import SENTIMENT_VALUES, URGENCY_VALUES

SCHEMAS = [schema_v2.EMAIL_SCHEMA_V2, schema_v2.ATTACHMENT_SCHEMA_V2]
# Keywords structured outputs refuse: length and number limits, and open objects.
UNSUPPORTED = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "multipleOf",
    "maxItems",
    "patternProperties",
}


def _nodes(schema):
    yield schema
    for value in schema.values():
        if isinstance(value, dict):
            yield from _nodes(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from _nodes(item)


@pytest.mark.parametrize("schema", SCHEMAS)
def test_every_object_is_closed_and_requires_every_property(schema):
    objects = [n for n in _nodes(schema) if n.get("type") == "object"]

    assert objects
    for node in objects:
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])


@pytest.mark.parametrize("schema", SCHEMAS)
def test_no_keyword_structured_outputs_refuse(schema):
    for node in _nodes(schema):
        assert not UNSUPPORTED & set(node)


@pytest.mark.parametrize("schema", SCHEMAS)
def test_the_unions_stay_far_under_the_limit_of_sixteen(schema):
    assert sum("anyOf" in node for node in _nodes(schema)) <= 1


def test_people_is_an_array_with_the_names_in_both_scripts():
    person = schema_v2.EMAIL_SCHEMA_V2["properties"]["people"]["items"]

    assert list(person["properties"]) == ["name_as_written", "name_latin", "email", "role"]


@pytest.mark.parametrize("item", ["decisions", "action_items", "commitments"])
def test_every_item_states_its_evidence_first(item):
    """Properties come out in the schema's order: the quote, then what follows from it."""
    props = schema_v2.EMAIL_SCHEMA_V2["properties"][item]["items"]["properties"]

    assert next(iter(props)) == "evidence"


def test_the_email_vocabularies_are_the_ones_the_store_already_holds():
    assert set(schema_v2.SENTIMENTS) == SENTIMENT_VALUES
    assert set(schema_v2.URGENCIES) == URGENCY_VALUES


def test_the_eval_grades_against_the_same_vocabularies():
    from src.evals import extraction as ev

    assert ev.ITEM_KINDS == schema_v2.ITEM_KINDS
    assert ev.LANGUAGES == schema_v2.LANGUAGES
    assert ev.OWNER_ROLES == schema_v2.OWNER_ROLES
    assert ev.DECISION_STATUSES == schema_v2.DECISION_STATUSES
    assert ev.DIRECTIONS == schema_v2.DIRECTIONS


def _reply(**over):
    reply = {
        "item_kind": "work_thread",
        "language": "en",
        "summary": "s",
        "topics": ["t"],
        "sentiment": "informational",
        "urgency": "low",
        "decisions": [
            {
                "evidence": "We approved it.",
                "decision": "Approved",
                "decided_by": "The board",
                "owner_role": "owner_fyi",
                "status": "made",
            }
        ],
        "action_items": [
            {
                "evidence": "Send it by Friday.",
                "task": "Send it",
                "owner": "Owner",
                "direction": "owner_owes",
                "deadline": "2026-10-16",
            }
        ],
        "commitments": [],
        "key_facts": [],
        "people": [],
    }
    reply.update(over)
    return reply


def test_a_reply_in_the_schema_has_no_problems():
    assert schema_v2.problems(_reply(), schema_v2.EMAIL_SCHEMA_V2) == []


def test_a_null_deadline_is_allowed():
    reply = _reply()
    reply["action_items"][0]["deadline"] = None

    assert schema_v2.problems(reply, schema_v2.EMAIL_SCHEMA_V2) == []


@pytest.mark.parametrize(
    "change",
    [
        {"language": "greek"},
        {"item_kind": "Work_Thread"},
        {"people": {"A": "requester"}},
        {"topics": "t"},
        {"references": []},
    ],
)
def test_a_reply_off_the_schema_has_problems(change):
    assert schema_v2.problems(_reply(**change), schema_v2.EMAIL_SCHEMA_V2)


def test_a_missing_field_and_a_bad_date_are_named_by_path_without_the_value():
    reply = _reply()
    del reply["summary"]
    reply["action_items"][0]["deadline"] = "next Friday"

    found = schema_v2.problems(reply, schema_v2.EMAIL_SCHEMA_V2)

    assert "$.summary is missing" in found
    assert any(p.startswith("$.action_items[0].deadline") for p in found)
    assert not any("next Friday" in p for p in found)


@pytest.mark.parametrize("value", ["2026-02-29", "2026-13-01", "16/10/2026", 20261016])
def test_is_date_wants_a_real_day_written_as_iso(value):
    assert not schema_v2.is_date(value)


def test_is_date_takes_an_iso_day():
    assert schema_v2.is_date("2026-10-16")
