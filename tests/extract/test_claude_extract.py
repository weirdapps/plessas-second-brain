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


def _thinking_first_response(text='{"summary": "ok"}'):
    """Response whose FIRST block is a thinking block, as extended thinking emits.

    A ThinkingBlock carries .thinking, not .text — indexing content[0].text
    raised AttributeError and failed the whole extraction. Seen on 17 of the
    first 177 news-synthesis extractions (2026-08-06 backfill).
    """
    thinking = types.SimpleNamespace(type="thinking", thinking="reasoning...")
    return types.SimpleNamespace(
        content=[thinking, types.SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=types.SimpleNamespace(output_tokens=1),
    )


def test_extract_one_reads_past_a_leading_thinking_block(monkeypatch):
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (MagicMock(), "m"))
    monkeypatch.setattr("src.extract.prompt.build_extraction_prompt", lambda email: "p")
    monkeypatch.setattr(
        "src.extract.vertex_fallback.create_with_refusal_fallback",
        lambda *a, **k: _thinking_first_response(),
    )
    seen = {}
    monkeypatch.setattr(
        "src.extract.parser.parse_extraction",
        lambda text, **k: seen.setdefault("text", text) and {} or {},
    )

    result = claude_extract.extract_one({"message_id": "m1"})

    assert result["message_id"] == "m1"
    assert seen["text"] == '{"summary": "ok"}', "must parse the text block, not the thinking block"


def test_extract_conversation_reads_past_a_leading_thinking_block(monkeypatch):
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (MagicMock(), "m"))
    monkeypatch.setattr("src.extract.prompt.build_conversation_extraction_prompt", lambda c: "p")
    monkeypatch.setattr(
        "src.extract.vertex_fallback.create_with_refusal_fallback",
        lambda *a, **k: _thinking_first_response(),
    )
    seen = {}
    monkeypatch.setattr(
        "src.extract.parser.parse_extraction",
        lambda text, **k: seen.setdefault("text", text) and {} or {},
    )

    result = claude_extract.extract_conversation({"session_id": "s1"})

    assert result["session_id"] == "s1"
    assert seen["text"] == '{"summary": "ok"}'


def test_extraction_asks_for_enough_output_tokens(monkeypatch):
    """4096 truncated dense inputs — a single news digest synthesis summarises
    ~50 articles, so the JSON legitimately runs long. attachment_pipeline
    already uses 8192 for the same reason."""
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (MagicMock(), "m"))
    monkeypatch.setattr("src.extract.prompt.build_extraction_prompt", lambda email: "p")
    captured = {}

    def _capture(client, **kwargs):
        captured.update(kwargs)
        return _fake_response()

    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", _capture)
    monkeypatch.setattr("src.extract.parser.parse_extraction", lambda text, **k: {})

    claude_extract.extract_one({"message_id": "m1"})

    assert captured["max_tokens"] >= 8192
