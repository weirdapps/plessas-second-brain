"""The call-shape switch: BRAIN_EXTRACT_CALL_SHAPE picks how extraction calls are built.

v1, the default, is the calls as they were; v2 is the shape of issue 163 (a cached system
prompt, a schema, a pinned effort). The switch is read at each call, so the eval can run
the same cases under either shape in one process.
"""

import pytest

from src.extract import call_shape
from src.extract.claude_extract import ClaudeNotConfigured
from tests.patching import unset_env


@pytest.mark.parametrize("value", [None, "", "  ", "v1", "V1"])
def test_v1_is_the_default(monkeypatch, value):
    if value is None:
        unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")
    else:
        monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", value)

    assert call_shape.call_shape() == "v1"


@pytest.mark.parametrize("value", ["v2", "V2", " v2 "])
def test_v2_is_chosen_by_the_variable(monkeypatch, value):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", value)

    assert call_shape.call_shape() == "v2"


@pytest.mark.parametrize("value", ["2", "true", "v3"])
def test_an_unknown_shape_stops_the_call_and_names_the_variable(monkeypatch, value):
    """A typo must not run v1 while the owner believes v2 is on. ClaudeNotConfigured
    leaves the work pending, as an unset model does."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", value)

    with pytest.raises(ClaudeNotConfigured, match="BRAIN_EXTRACT_CALL_SHAPE"):
        call_shape.call_shape()


@pytest.mark.parametrize("name", ["email", "attachment", "teams", "whatsapp"])
def test_every_route_pins_an_effort_and_a_max_tokens(name):
    route = call_shape.ROUTES[name]

    assert route.effort in ("low", "medium", "high")
    assert route.max_tokens >= 8192
    assert route.output_config()["effort"] == route.effort


def test_a_max_tokens_stop_gets_one_retry_at_double():
    route = call_shape.ROUTES["email"]

    assert call_shape.max_tokens_attempts(route) == (route.max_tokens, 2 * route.max_tokens)


def test_teams_and_whatsapp_get_more_room_than_their_v1_cap():
    """Thinking counts toward max_tokens: 13% of Teams threads stopped at 2,048."""
    assert call_shape.ROUTES["teams"].max_tokens >= 4 * 2048
    assert call_shape.ROUTES["whatsapp"].max_tokens >= 4 * 2048


def test_a_route_with_a_schema_asks_for_structured_output():
    route = call_shape.ROUTES["email"]

    assert route.output_config()["format"] == {"type": "json_schema", "schema": route.schema}


def test_a_route_without_a_schema_sends_effort_alone():
    assert call_shape.ROUTES["teams"].output_config() == {"effort": "high"}


def test_the_system_prompt_is_cached_at_its_last_block():
    blocks = call_shape.system_blocks("instructions", "examples")

    assert [b["text"] for b in blocks] == ["instructions", "examples"]
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_the_prompt_hash_is_stable_and_follows_the_instructions_and_the_schema():
    system = call_shape.system_blocks("instructions", "examples")
    schema = {"type": "json_schema", "schema": {"type": "object"}}

    same = call_shape.prompt_hash(system, schema)

    assert same == call_shape.prompt_hash(
        call_shape.system_blocks("instructions", "examples"), schema
    )
    assert same != call_shape.prompt_hash(
        call_shape.system_blocks("instructions!", "examples"), schema
    )
    assert same != call_shape.prompt_hash(system, {"type": "json_schema", "schema": {}})
    assert len(same) == 12


def test_a_call_with_no_system_prompt_and_no_schema_has_no_prompt_hash():
    assert call_shape.prompt_hash(None, None) is None
