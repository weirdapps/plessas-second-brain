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
from unittest.mock import MagicMock, patch

import google.auth.exceptions as gauth
import pytest

from src.extract import claude_extract
from src.llm_policy import ReauthResult


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


def test_an_auth_error_triggers_one_reauth_then_succeeds(monkeypatch):
    """macOS path: one reauth fires, the retry succeeds, two SDK calls total.

    running_on_linux is pinned False so this test asserts the macOS contract
    regardless of the CI host platform.
    """
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise gauth.RefreshError("invalid_grant: Bad Request")
            return type("R", (), {"stop_reason": "end_turn", "content": [type("C", (), {"text": "{}"})()]})()

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: False)
    monkeypatch.setattr("src.extract.parser.parse_extraction", lambda text, **k: {})
    with patch.object(claude_extract, "reauth", return_value=ReauthResult.SUCCEEDED) as mock_reauth:
        claude_extract.extract_one({"id": "1", "subject": "s", "body": "b", "message_id": "1"})
    assert len(calls) == 2
    assert mock_reauth.call_count == 1


def test_repeated_auth_errors_stop_at_the_cap(monkeypatch):
    """macOS path: auth fails twice, policy gives up after exactly two SDK calls.

    running_on_linux is pinned False so this test asserts the macOS contract
    regardless of the CI host platform.

    Call sequence when every create() raises RefreshError and reauth returns SUCCEEDED:
      1. Call 1 → RefreshError → AUTH_REAUTH_REQUIRED → REAUTH_RETRY.
         reauth() returns SUCCEEDED (not SKIPPED), so with_reauth_used() sets the
         one-shot latch.
      2. Call 2 → RefreshError → AUTH_REAUTH_REQUIRED → decide() sees reauth_used=True
         → UNRECOVERABLE_AUTH immediately (before the global total cap).
         Loop gives up and re-raises the RefreshError.

    Total SDK calls: 2.  A mutation that cuts retries to one call (while keeping the
    raise) leaves len(calls)==1, which kills this assertion.
    """
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            raise gauth.RefreshError("invalid_grant: Bad Request")

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: False)
    with patch.object(claude_extract, "reauth", return_value=ReauthResult.SUCCEEDED):
        with pytest.raises(gauth.RefreshError):
            claude_extract.extract_one({"id": "1", "subject": "s", "body": "b"})
    assert len(calls) == 2


def test_linux_auth_error_gives_up_immediately_without_reauth(monkeypatch):
    """Linux path: the budget cannot fund a token-push wait, so UNRECOVERABLE_AUTH
    fires on the first auth failure — one SDK call, reauth never invoked.

    Budget arithmetic (PTS_LLM_DEADLINE unset on CI):
      deadline  = now + DEFAULT_BUDGET_SECONDS          = now + 900
      wait      = PUSH_INTERVAL_SECONDS + PUSH_TOLERANCE_SECONDS = 900 + 120 = 1020
      check     = now + 1020 + max_call_seconds(120) > now + 900
                = now + 1140 > now + 900  →  True  →  UNRECOVERABLE_AUTH

    running_on_linux is pinned True so this test fails if the Linux branch is
    accidentally disabled, regardless of the actual test host.
    """
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            raise gauth.RefreshError("invalid_grant: Bad Request")

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: True)
    with patch.object(claude_extract, "reauth", return_value=ReauthResult.SUCCEEDED) as mock_reauth:
        with pytest.raises(gauth.RefreshError):
            claude_extract.extract_one({"id": "1", "subject": "s", "body": "b"})
    assert len(calls) == 1
    assert mock_reauth.call_count == 0
