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


# --- The fallback tier is allowed to equal the primary, and often does --------
# A deployment can point VERTEX_MODEL_EXTRACT and VERTEX_MODEL_FALLBACK_SDK at
# the same model in the same region, and this one does, because the fallback
# stopped being a model-class escape hatch and became a way to absorb transient
# errors. A policy refusal is a property of the model and the prompt, so
# replaying the identical pair cannot produce a different stop_reason: it just
# spends a second call, and up to another max_call_seconds of a unit's budget,
# to reach the same answer. 7,124 of these were logged in a single day.


def test_does_not_retry_when_fallback_tier_equals_the_primary(monkeypatch):
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-proj")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK_SDK", "claude-opus-5")
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "eu")

    primary = MagicMock()
    primary.region = "eu"
    primary.messages.create.return_value = _resp("refused", stop_reason="refusal")

    with patch("anthropic.AnthropicVertex") as mk_vertex:
        out = create_with_refusal_fallback(
            primary,
            model="claude-opus-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "x"}],
        )

    assert out.stop_reason == "refusal"
    primary.messages.create.assert_called_once()
    mk_vertex.assert_not_called()


def test_still_retries_when_only_the_region_differs(monkeypatch):
    """Same model in a different region is a real second attempt: the pairing
    rule (>=4.7 to eu, <=4.6 to europe-west1) means a mismatched pair 429s
    rather than refusing, so the region alone can change the outcome."""
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-proj")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK_SDK", "claude-opus-5")
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "europe-west1")

    primary = MagicMock()
    primary.region = "eu"
    primary.messages.create.return_value = _resp("refused", stop_reason="refusal")
    fb_client = MagicMock()
    fb_client.messages.create.return_value = _resp("RECOVERED")

    with patch("anthropic.AnthropicVertex", return_value=fb_client):
        out = create_with_refusal_fallback(
            primary,
            model="claude-opus-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "x"}],
        )

    assert out.content[0].text == "RECOVERED"


def test_retries_when_the_primary_region_is_unknowable(monkeypatch):
    """A client that does not expose `.region` (the direct-API path, or a future
    SDK rename) must keep the old behaviour. Skipping on an unproven match would
    turn an unknown into a silently dropped retry."""
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-proj")
    monkeypatch.setenv("VERTEX_MODEL_FALLBACK_SDK", "claude-opus-5")
    monkeypatch.setenv("VERTEX_REGION_FALLBACK", "eu")

    primary = MagicMock(spec=["messages"])
    primary.messages.create.return_value = _resp("refused", stop_reason="refusal")
    fb_client = MagicMock()
    fb_client.messages.create.return_value = _resp("RECOVERED")

    with patch("anthropic.AnthropicVertex", return_value=fb_client):
        out = create_with_refusal_fallback(
            primary,
            model="claude-opus-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "x"}],
        )

    assert out.content[0].text == "RECOVERED"
