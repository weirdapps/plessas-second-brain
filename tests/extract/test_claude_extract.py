"""Tests for the shared Claude/Vertex client lifecycle in claude_extract.

Regression context: the extractor used to build (and then close) a fresh
``AnthropicVertex`` client per item. On Vertex with an ``authorized_user`` ADC,
each client's first request makes ``google.auth`` fork ``gcloud config get
project`` (~2s). A backlog of N emails therefore forked gcloud N times and blew
past the sb-noon-catchup systemd start timeout (2026-07-07 incident).

The client must be built once and reused across the batch, and must NOT be
closed per item (it is a shared, long-lived instance closed at process exit).
"""

import types
from unittest.mock import MagicMock

import pytest

from src.extract import claude_extract


@pytest.fixture(autouse=True)
def _reset_client_cache():
    # Forward-compatible: no-op before the cache exists (RED), resets after (GREEN),
    # so cached clients never leak between tests.
    getattr(claude_extract, "reset_client_cache", lambda: None)()
    yield
    getattr(claude_extract, "reset_client_cache", lambda: None)()


def _fake_response(text="{}"):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(text=text)],
        stop_reason="end_turn",
        usage=types.SimpleNamespace(output_tokens=1),
    )


def test_get_client_and_model_reuses_same_client(monkeypatch):
    """Repeated calls return the SAME client instance (built once, reused)."""
    # Direct-API branch avoids Vertex/gcloud/network entirely.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

    client1, model1 = claude_extract._get_client_and_model()
    client2, model2 = claude_extract._get_client_and_model()

    assert client1 is client2
    assert model1 == model2


def test_extract_one_does_not_close_shared_client(monkeypatch):
    """extract_one must not close the shared client — later items still need it."""
    fake_client = MagicMock()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake_client, "m"))
    monkeypatch.setattr("src.extract.prompt.build_extraction_prompt", lambda email: "p")
    monkeypatch.setattr(
        "src.extract.vertex_fallback.create_with_refusal_fallback",
        lambda *a, **k: _fake_response(),
    )
    monkeypatch.setattr("src.extract.parser.parse_extraction", lambda text, **k: {})

    result = claude_extract.extract_one({"message_id": "m1"})

    assert result["message_id"] == "m1"
    fake_client.close.assert_not_called()


def test_extract_conversation_does_not_close_shared_client(monkeypatch):
    """extract_conversation must not close the shared client either."""
    fake_client = MagicMock()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake_client, "m"))
    monkeypatch.setattr("src.extract.prompt.build_conversation_extraction_prompt", lambda conv: "p")
    monkeypatch.setattr(
        "src.extract.vertex_fallback.create_with_refusal_fallback",
        lambda *a, **k: _fake_response(),
    )
    monkeypatch.setattr("src.extract.parser.parse_extraction", lambda text, **k: {})

    result = claude_extract.extract_conversation({"session_id": "s1"})

    assert result["session_id"] == "s1"
    fake_client.close.assert_not_called()
