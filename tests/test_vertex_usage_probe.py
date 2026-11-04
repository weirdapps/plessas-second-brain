"""scripts/vertex_usage_probe.py (issue 211): a few tiny calls in the shape v2 and v2-text
send, to see which usage fields the platform returns. The usage log records the thinking
tokens, the cache writes by time to live and the request id as null when they are not
reported; the probe says, field by field, whether they were.

The model is a fake installed as claude_extract.complete.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.extract import claude_extract

SCRIPT = Path(__file__).parents[1] / "scripts" / "vertex_usage_probe.py"


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("vertex_usage_probe", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(*, thinking=None, ttl=None, request_id=None, read=0):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="{}")],
        stop_reason="end_turn",
        model="claude-sonnet-5-5",
        id="msg_1",
        _request_id=request_id,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=20,
            cache_read_input_tokens=read,
            cache_creation_input_tokens=0 if read else 3000,
            cache_creation=ttl,
            output_tokens_details=SimpleNamespace(thinking_tokens=thinking)
            if thinking is not None
            else None,
        ),
    )


@pytest.fixture
def fake(monkeypatch):
    stub = SimpleNamespace(calls=[], replies=[])

    def complete(**kwargs):
        stub.calls.append(kwargs)
        return stub.replies.pop(0)

    monkeypatch.setattr(claude_extract, "complete", complete)
    return stub


def test_without_yes_nothing_is_called(probe, fake, capsys):
    assert probe.main([]) == 2

    assert fake.calls == []
    assert "Add --yes" in capsys.readouterr().out


def test_each_call_is_the_cached_v2_text_shape_at_a_small_budget(probe, fake):
    fake.replies = [_response(), _response(read=3000)]

    probe.main(["--yes", "--calls", "2"])

    first, second = fake.calls
    assert first["system"] == second["system"]
    assert first["system"][-1]["cache_control"]["ttl"] == "1h"
    assert "format" not in first["output_config"]
    assert first["max_tokens"] <= 1024


def test_it_names_what_was_reported_and_what_was_not(probe, fake, capsys):
    ttl = SimpleNamespace(ephemeral_1h_input_tokens=3000, ephemeral_5m_input_tokens=0)
    fake.replies = [
        _response(thinking=12, ttl=ttl, request_id="req_1"),
        _response(read=3000, request_id="req_2"),
    ]

    assert probe.main(["--yes", "--calls", "2"]) == 0

    out = capsys.readouterr().out
    assert "thinking_tokens: reported (12)" in out
    assert "cache_write_1h_tokens: reported (3000)" in out
    assert "request_id: reported (req_1, req_2)" in out
    assert "cache_read_input_tokens: reported (0, 3000)" in out


def test_a_field_never_reported_is_said_so(probe, fake, capsys):
    fake.replies = [_response()]

    probe.main(["--yes", "--calls", "1"])

    out = capsys.readouterr().out
    assert "thinking_tokens: not reported" in out
    assert "request_id: not reported" in out


def test_the_calls_are_capped(probe, fake):
    with pytest.raises(SystemExit):
        probe.main(["--yes", "--calls", "9"])
