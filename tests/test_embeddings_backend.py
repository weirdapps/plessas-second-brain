"""Which service embeds: Vertex (the default) or the Gemini API with a key.

The same model, gemini-embedding-001, is served by both. When the Vertex
project stopped serving it, the store needed a way to reach it through
GEMINI_API_KEY even on a host where a Vertex project is configured for the
Claude calls, which is every host this runs on.
"""

import pytest


class _Client:
    """Stands in for google.genai.Client and keeps what it was built with."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr("google.genai.Client", _Client)
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "proj")
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("VERTEX_REGION_EMBED", raising=False)
    monkeypatch.delenv("BRAIN_EMBED_BACKEND", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return monkeypatch


def test_gemini_backend_uses_the_key_although_a_vertex_project_is_set(env):
    from src.store.embeddings import _get_client

    env.setenv("BRAIN_EMBED_BACKEND", "gemini")
    env.setenv("GEMINI_API_KEY", "k-123")

    assert _get_client().kwargs == {"api_key": "k-123"}


def test_gemini_backend_without_a_key_fails_before_any_call(env):
    from src.store.embeddings import _get_client

    env.setenv("BRAIN_EMBED_BACKEND", "gemini")

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        _get_client()


def test_the_default_stays_on_vertex(env):
    from src.store.embeddings import _get_client

    env.setenv("GEMINI_API_KEY", "k-123")

    assert _get_client().kwargs == {"vertexai": True, "project": "proj", "location": "europe-west1"}


def test_an_unknown_backend_is_an_error_not_a_silent_vertex_call(env):
    from src.store.embeddings import _get_client

    env.setenv("BRAIN_EMBED_BACKEND", "gemni")

    with pytest.raises(ValueError, match="BRAIN_EMBED_BACKEND"):
        _get_client()
