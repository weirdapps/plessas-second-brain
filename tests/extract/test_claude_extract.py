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
    monkeypatch.delenv("VERTEX_SDK_PROJECT", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)

    client1, model1 = claude_extract._get_client_and_model()
    client2, model2 = claude_extract._get_client_and_model()

    assert client1 is client2
    assert model1 == model2


class _FakeSDKClient:
    """Stands in for an SDK client class, recording how it was built."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_sdk(monkeypatch):
    vertex = type("Vertex", (_FakeSDKClient,), {})
    direct = type("Direct", (_FakeSDKClient,), {})
    monkeypatch.setattr("anthropic.AnthropicVertex", vertex)
    monkeypatch.setattr("anthropic.Anthropic", direct)
    return vertex, direct


@pytest.mark.parametrize("name", ["VERTEX_SDK_PROJECT", "ANTHROPIC_VERTEX_PROJECT_ID"])
def test_a_vertex_project_wins_over_an_api_key(monkeypatch, name):
    """A key left in a shell profile sent work mail to the direct API, not Vertex."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("VERTEX_SDK_PROJECT", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.setenv(name, "test-project")
    monkeypatch.setenv("VERTEX_SDK_REGION", "eu")
    vertex, _ = _fake_sdk(monkeypatch)

    client, _ = claude_extract._build_client_and_model()

    assert isinstance(client, vertex)
    assert client.kwargs["region"] == "eu"


def test_the_api_key_is_used_only_without_a_vertex_project(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.delenv("VERTEX_SDK_PROJECT", raising=False)
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    _, direct = _fake_sdk(monkeypatch)

    client, _ = claude_extract._build_client_and_model()

    assert isinstance(client, direct)


@pytest.mark.parametrize("vertex", [True, False])
def test_the_backend_in_use_is_logged(monkeypatch, capsys, vertex):
    """The sync logs could not tell which backend had run. Names no credential."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    if vertex:
        monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
        monkeypatch.setenv("VERTEX_SDK_REGION", "eu")
    else:
        monkeypatch.delenv("VERTEX_SDK_PROJECT", raising=False)
        monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    _fake_sdk(monkeypatch)
    monkeypatch.setattr(claude_extract, "CLAUDE_MODEL_BASE", "claude-test-model")

    claude_extract._build_client_and_model()

    err = capsys.readouterr().err
    assert "claude-test-model" in err
    if vertex:
        assert "Vertex AI, region eu" in err
    else:
        assert "direct Anthropic API" in err
    assert "sk-ant" not in err
    assert "test-project" not in err


def test_complete_retries_a_refusal_on_the_fallback_tier(monkeypatch):
    """Attachments, images and calendar called the SDK themselves, so a refusal
    failed the item where extract_one retried it on the fallback tier."""
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    refusal = types.SimpleNamespace(content=[], stop_reason="refusal")
    primary = MagicMock()
    primary.messages.create.return_value = refusal
    fallback = MagicMock()
    fallback.messages.create.return_value = _fake_response("recovered")
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (primary, "m"))
    monkeypatch.setattr("anthropic.AnthropicVertex", lambda **kwargs: fallback)

    response = claude_extract.complete(max_tokens=10, messages=[{"role": "user", "content": "x"}])

    assert claude_extract._response_text(response) == "recovered"


def test_complete_does_not_replay_a_refusal_the_fallback_tier_saw(monkeypatch):
    """The policy retries a refusal twice, and each retry replayed the primary and
    the fallback again: six calls and 90 s of sleep for an answer that could not
    change. The fallback tier is the one retry a refusal gets."""
    monkeypatch.setenv("VERTEX_SDK_PROJECT", "test-project")
    refusal = types.SimpleNamespace(content=[], stop_reason="refusal")
    primary = MagicMock()
    primary.messages.create.return_value = refusal
    fallback = MagicMock()
    fallback.messages.create.return_value = refusal
    slept = []
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (primary, "m"))
    monkeypatch.setattr("anthropic.AnthropicVertex", lambda **kwargs: fallback)
    monkeypatch.setattr(claude_extract.time, "sleep", slept.append)

    response = claude_extract.complete(max_tokens=10, messages=[])

    assert response.stop_reason == "refusal"
    assert primary.messages.create.call_count + fallback.messages.create.call_count == 2
    assert slept == []


def test_complete_sends_an_explicit_model_and_the_system_prompt():
    """Teams names its own model and a system prompt; everything else takes the
    configured model."""
    client = MagicMock()
    client.messages.create.return_value = _fake_response()

    with patch.object(claude_extract, "_get_client_and_model", lambda: (client, "configured")):
        claude_extract.complete(max_tokens=10, messages=[])
        claude_extract.complete(model="teams-model", max_tokens=10, messages=[], system="s")

    first, second = (call.kwargs for call in client.messages.create.call_args_list)
    assert (first["model"], second["model"]) == ("configured", "teams-model")
    assert second["system"] == "s"
    assert "system" not in first


def _sdk_and_policy_references(tree):
    """Names each place in `tree` that reaches the SDK's messages API or the policy.

    From the syntax tree, not the text: any use of `.messages` counts (create,
    stream, a raw response, an alias, a getattr), and a docstring or comment
    that mentions them does not.
    """
    import ast

    guarded = {"call_with_policy", "create_with_refusal_fallback"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "messages":
            yield "messages"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "messages"
        ):
            yield "messages"
        elif isinstance(node, ast.Name) and node.id in guarded:
            yield node.id
        elif isinstance(node, ast.Attribute) and node.attr in guarded:
            yield node.attr
        elif isinstance(node, ast.ImportFrom):
            yield from (alias.name for alias in node.names if alias.name in guarded)


def test_the_reference_finder_sees_aliases_and_ignores_docstrings():
    import ast

    source = """
def f(client):
    \"\"\"It used to call client.messages.create() itself.\"\"\"
    send = client.messages.create
    other = getattr(client, "messages").create
    raw = client.messages.with_raw_response.create
    stream = client.messages.stream
    from src.extract.claude_extract import call_with_policy
    return send, other, raw, stream
"""
    found = sorted(_sdk_and_policy_references(ast.parse(source)))

    assert found == ["call_with_policy"] + ["messages"] * 4


def test_every_sdk_request_goes_through_complete():
    """The call sites each built the same request, and one bug was fixed four times.
    Only claude_extract.py, where complete() lives, may run the policy or the
    refusal fallback, and only the fallback module may touch the messages API."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    found: dict[str, set[str]] = {}
    for tree in ("src", "scripts"):
        for path in (root / tree).rglob("*.py"):
            parsed = ast.parse(path.read_text(encoding="utf-8"))
            for name in _sdk_and_policy_references(parsed):
                found.setdefault(name, set()).add(str(path.relative_to(root)))

    assert found["messages"] == {"src/extract/vertex_fallback.py"}
    assert found["create_with_refusal_fallback"] == {"src/extract/claude_extract.py"}
    assert found["call_with_policy"] == {"src/extract/claude_extract.py"}


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
            return type(
                "R", (), {"stop_reason": "end_turn", "content": [type("C", (), {"text": "{}"})()]}
            )()

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


def test_reauth_receives_the_same_is_linux_as_decide(monkeypatch):
    """reauth(is_linux=...) and decide(is_linux=...) must use the same value.

    call_with_policy computes is_linux = running_on_linux() and passes it to
    decide().  If reauth() is called without is_linux=is_linux, the two can
    disagree silently: on a VPS decide() sees True (WAIT_FOR_PUSH) while
    reauth() auto-detects and runs the macOS gcloud script instead of polling.

    running_on_linux is pinned False (macOS path) so the budget admits one
    reauth, and the captured kwargs must carry is_linux=False.

    Mutation check: reverting reauth(is_linux=is_linux) to bare reauth() leaves
    reauth_calls[0] empty, making reauth_calls[0]["is_linux"] raise KeyError.
    """
    calls = []
    reauth_calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise gauth.RefreshError("invalid_grant: Bad Request")
            return type(
                "R",
                (),
                {
                    "stop_reason": "end_turn",
                    "content": [type("C", (), {"text": "{}"})()],
                },
            )()

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: False)
    monkeypatch.setattr("src.extract.parser.parse_extraction", lambda text, **k: {})

    def _capturing_reauth(**kwargs):
        reauth_calls.append(kwargs)
        return ReauthResult.SUCCEEDED

    with patch.object(claude_extract, "reauth", side_effect=_capturing_reauth):
        claude_extract.extract_one({"id": "1", "subject": "s", "body": "b", "message_id": "1"})

    assert len(reauth_calls) == 1
    assert reauth_calls[0]["is_linux"] is False


# --- _response_text is the repo's single reader of a Claude response -----------
#
# Six call sites now route through it: extract_one, extract_conversation,
# attachment_pipeline, calendar_extractor, teams_pipeline and
# scripts/curate_documents_daily. image_vision keeps its own copy because its
# message is vision-specific. The raise is the diagnostic surface: its string is
# what lands in attachment_content.llm_error and in the sync logs.


class _ThinkingBlock:
    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


def test_response_text_returns_the_first_text_block(monkeypatch):
    resp = types.SimpleNamespace(
        content=[_ThinkingBlock("..."), types.SimpleNamespace(text="answer")],
        stop_reason="end_turn",
    )
    assert claude_extract._response_text(resp) == "answer"


def test_response_text_names_the_shape_when_there_is_no_text_block():
    """A bare "contained no text block" said nothing about WHY.

    The two shapes that actually occur are a thinking-only response truncated at
    max_tokens, and an empty content list. Both have to be distinguishable from
    the persisted error string alone, with no access to the response object.
    """
    resp = types.SimpleNamespace(content=[_ThinkingBlock("...")], stop_reason="max_tokens")

    with pytest.raises(ValueError) as excinfo:
        claude_extract._response_text(resp)

    message = str(excinfo.value)
    assert "no text block" in message
    assert "max_tokens" in message, "the stop_reason must survive into the log line"
    assert "_ThinkingBlock" in message, "the block types must survive into the log line"


def test_response_text_handles_an_empty_content_list():
    """Zero blocks must raise the same diagnosable ValueError, never IndexError."""
    resp = types.SimpleNamespace(content=[], stop_reason="max_tokens")

    with pytest.raises(ValueError, match="no text block"):
        claude_extract._response_text(resp)
