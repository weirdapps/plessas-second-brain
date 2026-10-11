"""Every Claude call through claude_extract.complete() leaves one usage line.

None of these calls writes a Claude Code transcript, so before this file the
extraction spend could only be estimated from item counts (2026-10-03: ~10k
calls a week, no token figure anywhere). One JSON line per completed call, in a
monthly file under the data root, makes it measurable. Logging must never cost
an extraction, so a failure to write is swallowed.
"""

import json
from datetime import UTC, datetime

from src.extract import claude_extract
from tests.patching import unset_env


class _Usage:
    input_tokens = 1200
    output_tokens = 345
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _TextBlock:
    text = '{"summary": "s"}'


class _Response:
    def __init__(self, usage=True):
        self.content = [_TextBlock()]
        self.stop_reason = "end_turn"
        self.model = "claude-test-model"
        if usage:
            self.usage = _Usage()


def _patch_client(monkeypatch, response):
    class FakeMessages:
        def create(self, **kw):
            return response

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr("src.extract.claude_extract._get_client_and_model", lambda: (fake, "m"))


def _usage_lines(tmp_path):
    path = tmp_path / f"llm-usage-{datetime.now(UTC).strftime('%Y-%m')}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_completed_call_logs_its_tokens_and_caller(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    _patch_client(monkeypatch, _Response())

    claude_extract.complete(max_tokens=10, messages=[{"role": "user", "content": "x"}])

    [rec] = _usage_lines(tmp_path)
    assert rec["model"] == "claude-test-model"
    assert rec["input_tokens"] == 1200 and rec["output_tokens"] == 345
    assert rec["stop_reason"] == "end_turn"
    assert rec["site"].endswith("test_a_completed_call_logs_its_tokens_and_caller")


def test_a_response_without_usage_logs_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    _patch_client(monkeypatch, _Response(usage=False))

    claude_extract.complete(max_tokens=10, messages=[{"role": "user", "content": "x"}])

    assert _usage_lines(tmp_path) == []


def test_an_unwritable_log_never_costs_the_call(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", blocker)
    response = _Response()
    _patch_client(monkeypatch, response)

    out = claude_extract.complete(max_tokens=10, messages=[{"role": "user", "content": "x"}])

    assert out is response


# --- what each line says about the call (issue 163) ---------------------------------


class _Details:
    thinking_tokens = 210


class _CacheCreation:
    ephemeral_5m_input_tokens = 0
    ephemeral_1h_input_tokens = 3100


class _FullUsage(_Usage):
    cache_creation_input_tokens = 3100
    cache_read_input_tokens = 0
    output_tokens_details = _Details()
    cache_creation = _CacheCreation()


class _StopDetails:
    category = "general_harms"


class _FullResponse(_Response):
    def __init__(self, stop_reason="end_turn"):
        super().__init__()
        self.usage = _FullUsage()
        self.stop_reason = stop_reason
        self.id = "msg_test_1"
        self._request_id = "req_test_1"
        if stop_reason == "refusal":
            self.stop_details = _StopDetails()


def test_a_line_carries_thinking_cache_ttl_ids_effort_budget_and_prompt_hash(monkeypatch, tmp_path):
    from src.extract.call_shape import prompt_hash, system_blocks

    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    _patch_client(monkeypatch, _FullResponse())
    system = system_blocks("instructions", "examples")
    output_config = {"effort": "high", "format": {"type": "json_schema", "schema": {}}}

    claude_extract.complete(
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": "x"}],
        output_config=output_config,
    )

    [rec] = _usage_lines(tmp_path)
    assert rec["thinking_tokens"] == 210
    assert (rec["cache_write_5m_tokens"], rec["cache_write_1h_tokens"]) == (0, 3100)
    assert rec["cache_creation_input_tokens"] == 3100
    assert (rec["request_id"], rec["response_id"]) == ("req_test_1", "msg_test_1")
    assert rec["effort"] == "high"
    assert rec["max_tokens"] == 8192
    assert rec["prompt_hash"] == prompt_hash(system, output_config["format"])
    assert rec["refusal_category"] is None


def test_a_refusal_logs_its_category(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    unset_env(monkeypatch, "VERTEX_MODEL_FALLBACK_SDK")
    _patch_client(monkeypatch, _FullResponse(stop_reason="refusal"))

    claude_extract.complete(max_tokens=10, messages=[{"role": "user", "content": "x"}])

    [rec] = _usage_lines(tmp_path)
    assert rec["stop_reason"] == "refusal"
    assert rec["refusal_category"] == "general_harms"


def test_a_v1_call_logs_the_new_fields_empty(monkeypatch, tmp_path):
    """v1 sends no system prompt, schema or effort; a platform that reports no thinking
    or cache split leaves those empty rather than zero."""
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    _patch_client(monkeypatch, _Response())

    claude_extract.complete(max_tokens=8192, messages=[{"role": "user", "content": "x"}])

    [rec] = _usage_lines(tmp_path)
    for key in (
        "thinking_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "request_id",
        "response_id",
        "effort",
        "prompt_hash",
        "refusal_category",
    ):
        assert rec[key] is None, key
    assert rec["max_tokens"] == 8192


def test_logging_sends_the_request_unchanged(monkeypatch, tmp_path):
    """The log reads the request; it must not add to it."""
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    sent = []

    class FakeMessages:
        def create(self, **kw):
            sent.append(kw)
            return _Response()

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr("src.extract.claude_extract._get_client_and_model", lambda: (fake, "m"))

    claude_extract.complete(max_tokens=8192, messages=[{"role": "user", "content": "x"}])

    assert sent == [
        {"model": "m", "max_tokens": 8192, "messages": [{"role": "user", "content": "x"}]}
    ]
