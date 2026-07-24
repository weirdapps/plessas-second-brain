"""Tests for create_with_refusal_fallback (SDK policy-refusal auto-downgrade)."""

from unittest.mock import MagicMock, patch

from src.extract.vertex_fallback import create_with_refusal_fallback


def _resp(text, stop_reason="end_turn"):
    r = MagicMock()
    r.stop_reason = stop_reason
    r.content = [MagicMock(text=text)]
    return r


def test_returns_primary_when_no_refusal():
    client = MagicMock()
    client.messages.create.return_value = _resp("OK")
    out = create_with_refusal_fallback(client, model="claude-opus-4-8", max_tokens=10, messages=[])
    assert out.content[0].text == "OK"
    client.messages.create.assert_called_once()


def test_returns_primary_on_max_tokens_without_downgrade():
    client = MagicMock()
    client.messages.create.return_value = _resp("partial", stop_reason="max_tokens")
    out = create_with_refusal_fallback(client, model="claude-opus-4-8", max_tokens=10, messages=[])
    assert out.stop_reason == "max_tokens"
    client.messages.create.assert_called_once()


def test_downgrades_to_fallback_on_refusal(monkeypatch):
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-proj")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK_SDK", "claude-opus-4-6")
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "europe-west1")

    primary = MagicMock()
    primary.messages.create.return_value = _resp("refused", stop_reason="refusal")
    fb_client = MagicMock()
    fb_client.messages.create.return_value = _resp("RECOVERED")

    with patch("anthropic.AnthropicVertex", return_value=fb_client) as mk_vertex:
        out = create_with_refusal_fallback(
            primary,
            model="claude-opus-4-8",
            max_tokens=10,
            messages=[{"role": "user", "content": "x"}],
        )

    assert out.content[0].text == "RECOVERED"
    # fallback client pinned to europe-west1
    assert mk_vertex.call_args.kwargs["region"] == "europe-west1"
    # fallback create used the SDK fallback model id (no [1m] suffix)
    assert fb_client.messages.create.call_args.kwargs["model"] == "claude-opus-4-6"
    fb_client.close.assert_called_once()
