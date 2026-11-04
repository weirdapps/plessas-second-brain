"""BRAIN_EXTRACT_V2_ROUTES (issue 211): the calendar route and the cached Teams and WhatsApp
system prompts are opt-in, per route, so a deploy leaves those requests as they were until
the owner names the route after an eval. Empty (the default) keeps them as before under any
call shape; v1 keeps them as before whatever it names; a name that is not one of them stops
the calls."""

import pytest

from src.extract import call_shape
from src.extract.claude_extract import ClaudeNotConfigured
from tests.patching import unset_env


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    unset_env(monkeypatch, "BRAIN_EXTRACT_V2_ROUTES")
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")


@pytest.mark.parametrize("route", call_shape.OPT_IN_ROUTES)
def test_unset_keeps_every_opt_in_route_off(route):
    assert call_shape.OPT_IN_ROUTES == ("calendar", "teams", "whatsapp")
    assert not call_shape.v2_route(route)


def test_a_named_route_is_on_and_the_others_stay_off(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_V2_ROUTES", " Calendar , teams ")

    assert call_shape.v2_route("calendar") and call_shape.v2_route("teams")
    assert not call_shape.v2_route("whatsapp")


def test_v1_keeps_them_off_whatever_is_named(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v1")
    monkeypatch.setenv("BRAIN_EXTRACT_V2_ROUTES", "calendar,teams,whatsapp")

    assert not any(call_shape.v2_route(r) for r in call_shape.OPT_IN_ROUTES)


def test_a_name_that_is_not_a_route_stops_the_call(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_V2_ROUTES", "calender")

    with pytest.raises(ClaudeNotConfigured, match="BRAIN_EXTRACT_V2_ROUTES"):
        call_shape.v2_route("calendar")
