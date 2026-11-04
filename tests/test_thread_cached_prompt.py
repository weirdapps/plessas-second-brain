"""Cached system prompts for the Teams and WhatsApp routes under call shapes v2 and v2-text
(issue 211).

Their v1 system prompts are a few hundred tokens, under what the model caches, and the JSON
the model is asked for sat in the user turn after the thread, so nothing of it could be
cached. Under v2 and v2-text the instructions and the JSON keys move into a system prompt
with worked examples, the same bytes for every thread of a store, with one cache marker on
its last block; the user turn holds the fenced thread and a line restating the task. v1 is
unchanged (tests/test_thread_call_shape_v2.py).
"""

import json
from types import SimpleNamespace

import pytest

from src.extract import (
    call_shape,
    claude_extract,
    teams_pipeline,
    teams_prompt,
    thread_examples,
    whatsapp_pipeline,
    whatsapp_prompt,
)
from tests.patching import unset_env

THREAD = {
    "chat_label": "Project X",
    "thread_kind": "chat_session",
    "started_at": "2026-10-01T09:00:00Z",
    "ended_at": "2026-10-01T09:30:00Z",
    "participants": ["A. One", "B. Two"],
    "message_count": 2,
}
MESSAGES = [
    {"composed_at": "2026-10-01T09:00:00Z", "sender": "A. One", "content": "Can you send it?"},
    {"composed_at": "2026-10-01T09:10:00Z", "sender": "B. Two", "content": "Yes, by Friday."},
]
OTHER = [{"composed_at": "2026-10-02T10:00:00Z", "sender": "C", "content": "Another thread"}]

ROUTES = [
    (teams_pipeline, teams_prompt.build_prompt, "teams"),
    (whatsapp_pipeline, whatsapp_prompt.build_prompt, "whatsapp"),
]


def response(text='{"summary": "s"}', stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        model="claude-sonnet-5-5",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


@pytest.fixture
def model(monkeypatch, tmp_path):
    stub = SimpleNamespace(calls=[])

    def create(client, *, after_refusal=None, **kwargs):
        stub.calls.append(kwargs)
        return response()

    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (object(), "m"))
    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", create)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    unset_env(monkeypatch, "BRAIN_TEAMS_MODEL", "BRAIN_WHATSAPP_MODEL")
    return stub


def _sent(model, pipeline, build, messages=MESSAGES, commitments=False):
    system, user = build(THREAD, messages)
    if commitments:
        system, user = teams_prompt.with_commitments(system, user)
    pipeline._call_llm(system, user)
    return model.calls[-1]


@pytest.mark.parametrize("shape", ["v2", "v2-text"])
@pytest.mark.parametrize(("pipeline", "build", "route"), ROUTES)
def test_the_system_prompt_is_cached_at_its_last_block(
    monkeypatch, model, shape, pipeline, build, route
):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", shape)

    sent = _sent(model, pipeline, build)

    blocks = sent["system"]
    assert isinstance(blocks, list) and len(blocks) == 2
    assert [b.get("cache_control") for b in blocks] == [None, call_shape.CACHE_CONTROL]
    v1_system, _ = build(THREAD, MESSAGES)
    assert blocks[0]["text"].startswith(v1_system)
    assert "Return JSON with these keys" in blocks[0]["text"]
    assert "WORKED EXAMPLES" in blocks[1]["text"]
    assert sent["output_config"] == {"effort": call_shape.ROUTES[route].effort}


@pytest.mark.parametrize(("pipeline", "build", "_route"), ROUTES)
def test_the_user_turn_holds_the_fenced_thread_and_not_the_keys(
    monkeypatch, model, pipeline, build, _route
):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")

    sent = _sent(model, pipeline, build)

    [turn] = sent["messages"]
    assert "Yes, by Friday." in turn["content"]
    assert "Return JSON with these keys" not in turn["content"]
    assert turn["content"].endswith(teams_prompt.RESTATE)


@pytest.mark.parametrize(("pipeline", "build", "_route"), ROUTES)
def test_the_system_prompt_is_the_same_bytes_for_every_thread(
    monkeypatch, model, pipeline, build, _route
):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")

    first = _sent(model, pipeline, build)["system"]
    second = _sent(model, pipeline, build, messages=OTHER)["system"]

    assert first == second


@pytest.mark.parametrize(("pipeline", "build", "_route"), ROUTES)
def test_the_system_prompt_is_long_enough_to_cache(monkeypatch, model, pipeline, build, _route):
    """Sonnet 5.5 caches a prefix of 1,024 tokens or more; at four characters a token, the
    least English packs into one, 4,096 characters is the floor, and Greek packs fewer."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")

    blocks = _sent(model, pipeline, build)["system"]

    assert sum(len(b["text"]) for b in blocks) >= 4096


@pytest.mark.parametrize(("pipeline", "build", "_route"), ROUTES)
def test_commitments_are_asked_in_the_cached_keys_and_shown_in_the_examples(
    monkeypatch, model, pipeline, build, _route
):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")

    plain = _sent(model, pipeline, build)["system"]
    asked = _sent(model, pipeline, build, commitments=True)["system"]

    assert '"commitments"' not in plain[0]["text"] + plain[1]["text"]
    assert teams_prompt.COMMITMENT_RULE in asked[0]["text"]
    assert '"commitments"' in asked[0]["text"]
    assert '"commitments"' in asked[1]["text"]


@pytest.mark.parametrize("route", ["teams", "whatsapp"])
@pytest.mark.parametrize("commitments", [False, True])
def test_every_worked_answer_has_exactly_the_keys_asked(route, commitments):
    keys = ["summary", "decisions", "action_items", "key_facts", "sentiment", "language"]
    if commitments:
        keys.insert(3, "commitments")
    for example in thread_examples.EXAMPLES[route]:
        answer = thread_examples.answer(example, commitments=commitments)
        assert list(answer) == keys
        assert answer["sentiment"] in ("positive", "neutral", "negative", "mixed")
        assert answer["language"] in ("el", "en")
        json.dumps(answer, ensure_ascii=False)


@pytest.mark.parametrize("route", ["teams", "whatsapp"])
def test_each_worked_item_is_said_in_its_thread(route):
    """An example must not teach the model to invent: each decision, action and commitment
    names something its thread's messages say."""
    for example in thread_examples.EXAMPLES[route]:
        text = " ".join(m["content"] for m in example.chat)
        for commitment in example.commitments:
            assert commitment["by"] in example.thread["participants"]
        for item in example.answer["decisions"] + example.answer["action_items"]:
            assert item.get("decided_by", item.get("owner")) in example.thread["participants"]
        assert text


def test_the_two_routes_keep_their_own_instructions(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")

    teams = _sent(model, teams_pipeline, teams_prompt.build_prompt)["system"]
    whatsapp = _sent(model, whatsapp_pipeline, whatsapp_prompt.build_prompt)["system"]

    assert teams[0]["text"].startswith(teams_prompt.SYSTEM_PROMPT)
    assert whatsapp[0]["text"].startswith(whatsapp_prompt.SYSTEM_PROMPT)
    assert teams[1]["text"] != whatsapp[1]["text"]


@pytest.mark.parametrize(("pipeline", "build", "_route"), ROUTES)
def test_v1_sends_the_prompts_as_built(monkeypatch, model, pipeline, build, _route):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v1")
    system, user = build(THREAD, MESSAGES)

    pipeline._call_llm(system, user)

    [sent] = model.calls
    assert sent["system"] == system
    assert sent["messages"] == [{"role": "user", "content": user}]
