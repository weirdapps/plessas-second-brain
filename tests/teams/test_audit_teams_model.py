"""Teams extraction follows the configured model unless BRAIN_TEAMS_MODEL says otherwise.

_call_llm fell back from BRAIN_TEAMS_MODEL to VERTEX_MODEL_EXTRACT and then to a
hardcoded claude-sonnet-4-6, skipping CLAUDE_EXTRACT_MODEL, the variable README
documents. An operator who set CLAUDE_EXTRACT_MODEL to a 4.7+ model with the eu
region sent every Teams call to a 4.6 model on that region, which returns 429
(audit docs-2).
"""

from types import SimpleNamespace

import pytest

from src.extract import teams_pipeline


@pytest.fixture
def sent_model(monkeypatch):
    captured = {}

    def fake_create(client, **kwargs):
        captured["model"] = kwargs.get("model")
        return SimpleNamespace(content=[SimpleNamespace(text="RAW")])

    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model",
        lambda: ("shared-client", "configured-extract-model"),
    )
    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", fake_create)
    monkeypatch.delenv("BRAIN_TEAMS_MODEL", raising=False)
    monkeypatch.delenv("VERTEX_MODEL_EXTRACT", raising=False)
    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", "configured-extract-model")
    return captured


def test_without_an_override_teams_uses_the_configured_model(sent_model):
    teams_pipeline._call_llm("system", "user")

    assert sent_model["model"] == "configured-extract-model"


def test_the_teams_override_still_wins(sent_model, monkeypatch):
    monkeypatch.setenv("BRAIN_TEAMS_MODEL", "claude-teams-override")

    teams_pipeline._call_llm("system", "user")

    assert sent_model["model"] == "claude-teams-override"
