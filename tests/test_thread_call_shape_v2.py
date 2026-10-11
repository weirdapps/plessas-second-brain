"""Teams and WhatsApp calls under each call shape.

Thinking counts toward max_tokens, and at 2,048 it cut 13% of Teams calls short; a
thread cut short fails its parse and is sent again next run. v1 keeps the 2,048 calls as
they were. v2 gives them 8,192, pins the effort, and asks once more at twice the budget
after a max_tokens stop. Their prompts are unchanged, and too short to cache, so no
schema and no cache marker are sent.
"""

from types import SimpleNamespace

import pytest

from src.extract import call_shape, claude_extract, teams_pipeline, whatsapp_pipeline

ROUTES = [(teams_pipeline, "teams"), (whatsapp_pipeline, "whatsapp")]


def response(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model="claude-sonnet-5-5",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


@pytest.fixture
def model(monkeypatch, tmp_path):
    stub = SimpleNamespace(calls=[], replies=[])

    def create(client, *, after_refusal=None, **kwargs):
        stub.calls.append(kwargs)
        return stub.replies.pop(0)

    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (object(), "m"))
    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", create)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    monkeypatch.delenv("BRAIN_TEAMS_MODEL", raising=False)
    monkeypatch.delenv("BRAIN_WHATSAPP_MODEL", raising=False)
    return stub


@pytest.mark.parametrize(("pipeline", "_route"), ROUTES)
def test_v1_sends_what_it_always_sent(monkeypatch, model, pipeline, _route):
    monkeypatch.delenv("BRAIN_EXTRACT_CALL_SHAPE", raising=False)
    model.replies = [response('{"summary": "s"}')]

    assert pipeline._call_llm("the system prompt", "the thread") == '{"summary": "s"}'

    assert model.calls == [
        {
            "model": "m",
            "max_tokens": 2048,
            "system": "the system prompt",
            "messages": [{"role": "user", "content": "the thread"}],
        }
    ]


@pytest.mark.parametrize(("pipeline", "route"), ROUTES)
def test_v2_pins_the_effort_and_raises_the_budget(monkeypatch, model, pipeline, route):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    model.replies = [response('{"summary": "s"}')]

    pipeline._call_llm("the system prompt", "the thread")

    [sent] = model.calls
    assert sent["max_tokens"] == call_shape.ROUTES[route].max_tokens >= 8192
    assert sent["output_config"] == {"effort": call_shape.ROUTES[route].effort}
    assert sent["system"] == "the system prompt"
    assert sent["messages"] == [{"role": "user", "content": "the thread"}]


@pytest.mark.parametrize(("pipeline", "_route"), ROUTES)
def test_v2_asks_once_more_at_double_after_a_max_tokens_stop(monkeypatch, model, pipeline, _route):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    model.replies = [response('{"summary": "cu', stop="max_tokens"), response('{"summary": "s"}')]

    assert pipeline._call_llm("sys", "thread") == '{"summary": "s"}'

    assert [c["max_tokens"] for c in model.calls] == [8192, 16384]


@pytest.mark.parametrize(("pipeline", "_route"), ROUTES)
def test_v2_returns_a_second_cut_short_reply_for_the_parser_to_fail(
    monkeypatch, model, pipeline, _route
):
    """As in v1: the thread's parse fails and the thread is offered again next run."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    model.replies = [response('{"a', stop="max_tokens"), response('{"ab', stop="max_tokens")]

    assert pipeline._call_llm("sys", "thread") == '{"ab'
    assert len(model.calls) == 2


def test_v2_a_teams_refusal_is_still_a_refused_thread(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    model.replies = [response("", stop="refusal")]

    with pytest.raises(teams_pipeline.ThreadRefused):
        teams_pipeline._call_llm("sys", "thread")
