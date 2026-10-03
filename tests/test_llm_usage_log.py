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
