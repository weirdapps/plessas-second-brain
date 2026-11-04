"""The effort a route is pinned at, and the per-route override an effort sweep sets
(issue 211): BRAIN_EXTRACT_EFFORT_<ROUTE> replaces the route's pinned effort in every
call of that route, and nothing else; a value that is not an effort stops the calls."""

import pytest

from src.extract import call_shape
from src.extract.claude_extract import ClaudeNotConfigured
from tests.patching import unset_env

ROUTE_NAMES = sorted(call_shape.ROUTES)


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch):
    unset_env(monkeypatch, *(call_shape.effort_var(name) for name in ROUTE_NAMES))


@pytest.mark.parametrize("name", ROUTE_NAMES)
def test_unset_keeps_the_pinned_effort(name):
    route = call_shape.ROUTES[name]
    assert call_shape.route_effort(route) == route.effort
    assert route.output_config(structured=False) == {"effort": route.effort}


def test_the_override_names_its_route():
    assert call_shape.effort_var("email") == "BRAIN_EXTRACT_EFFORT_EMAIL"
    assert call_shape.effort_var("calendar") == "BRAIN_EXTRACT_EFFORT_CALENDAR"


def test_an_override_changes_its_route_and_no_other(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_EFFORT_EMAIL", " Medium ")

    assert call_shape.ROUTES["email"].output_config(structured=False) == {"effort": "medium"}
    assert call_shape.ROUTES["attachment"].output_config(structured=False) == {"effort": "high"}


def test_an_override_reaches_the_thread_calls(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    monkeypatch.setenv("BRAIN_EXTRACT_EFFORT_TEAMS", "low")

    _, pinned = call_shape.thread_call("teams", 2048)

    assert pinned == {"output_config": {"effort": "low"}}


def test_v1_sends_no_effort_whatever_the_override(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v1")
    monkeypatch.setenv("BRAIN_EXTRACT_EFFORT_TEAMS", "low")

    assert call_shape.thread_call("teams", 2048)[1] == {}


def test_a_value_that_is_not_an_effort_stops_the_call(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_EFFORT_EMAIL", "hihg")

    with pytest.raises(ClaudeNotConfigured, match="BRAIN_EXTRACT_EFFORT_EMAIL"):
        call_shape.ROUTES["email"].output_config()
