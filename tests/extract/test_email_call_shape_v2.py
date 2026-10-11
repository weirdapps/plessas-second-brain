"""The email route under each call shape.

v1, the default, must send exactly what it sent before issue 163. v2 sends the
instructions and two worked examples as a cached system prompt, the email fenced in the
user turn, schema v2 as structured output and a pinned effort, and asks once more at
twice the budget after a max_tokens stop. The client is stubbed at the one call site,
vertex_fallback.create_with_refusal_fallback, so the request is seen as the SDK gets it.
"""

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from src.extract import call_shape, claude_extract, prompt, schema_v2

EMAIL = {
    "message_id": "m-1",
    "date_received": "2026-10-05T09:00:00Z",
    "sender": {"name": "Sam Carter", "address": "sam.carter@example.com"},
    "to_recipients": [{"name": "Owner Person", "address": "owner@example.com"}],
    "cc_recipients": [],
    "subject": "Q4 vendor review",
    "content": "Hi,\nthe audit team approved the revised vendor list yesterday.\n"
    "Could you send me your comments on the risk section by Friday 16 October?\nSam",
    "mailbox_name": "Inbox",
}
OTHER = dict(EMAIL, message_id="m-2", subject="Another subject", content="Another body.")

_TAG = re.compile(r"untrusted_[0-9a-f]+")


def reply(**over):
    answer = {
        "item_kind": "work_thread",
        "language": "en",
        "summary": "The audit team approved the vendor list; the owner is asked for comments.",
        "topics": ["vendor review"],
        "sentiment": "collaborative",
        "urgency": "medium",
        "decisions": [
            {
                "evidence": "the audit team approved the revised vendor list yesterday",
                "decision": "Approve the revised vendor list",
                "decided_by": "Audit team",
                "owner_role": "owner_fyi",
                "status": "made",
            }
        ],
        "action_items": [
            {
                "evidence": "Could you send me your comments on the risk section by Friday 16 October?",
                "task": "Send comments on the risk section",
                "owner": "Owner Person",
                "direction": "owner_owes",
                "deadline": "2026-10-16",
            }
        ],
        "commitments": [],
        "key_facts": [],
        "people": [
            {
                "name_as_written": "Sam Carter",
                "name_latin": "Sam Carter",
                "email": "sam.carter@example.com",
                "role": "requester",
            }
        ],
    }
    answer.update(over)
    return json.dumps(answer)


def response(text="{}", stop="end_turn", category=None):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop,
        stop_details=SimpleNamespace(category=category) if category else None,
        model="claude-sonnet-5-5",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )


@pytest.fixture
def model(monkeypatch, tmp_path):
    """The stubbed SDK call: `calls` holds each request, `replies` what comes back."""
    stub = SimpleNamespace(calls=[], replies=[])

    def create(client, *, after_refusal=None, **kwargs):
        # after_refusal is the fallback helper's own bookkeeping; the rest is the request.
        stub.calls.append(kwargs)
        return stub.replies.pop(0)

    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (object(), "m"))
    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", create)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    return stub


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")


# --- v1 stays as it was --------------------------------------------------------------


def test_v1_is_the_default_and_sends_the_v1_prompt_alone(monkeypatch, model):
    monkeypatch.delenv("BRAIN_EXTRACT_CALL_SHAPE", raising=False)
    model.replies = [response('{"summary": "s"}')]

    claude_extract.extract_one(EMAIL)

    [sent] = model.calls
    assert set(sent) == {"model", "max_tokens", "messages"}
    assert sent["max_tokens"] == 8192
    [turn] = sent["messages"]
    assert turn["role"] == "user"
    expected = prompt.build_extraction_prompt(EMAIL)
    assert _TAG.sub("T", turn["content"]) == _TAG.sub("T", expected)


def test_the_v1_prompt_is_byte_for_byte_what_it_was(monkeypatch):
    """Fingerprints of the v1 prompt taken before call shape v2 existed, with the
    identity and the payment rule held still so other changes cannot move them."""
    monkeypatch.setattr(prompt, "USER_NAME", "Probe Owner")
    monkeypatch.setattr(prompt, "USER_ROLE", "Head of Probes")
    monkeypatch.setattr(prompt, "PROMPT_RULE", "PROMPT RULE")
    probe = {
        "message_id": "probe-1",
        "date_received": "2026-10-01T08:30:00Z",
        "sender": {"name": "Sender Person", "address": "sender@example.com"},
        "to_recipients": [
            {"name": "Probe Owner", "address": "owner@example.com"},
            "plain@example.com",
        ],
        "cc_recipients": [],
        "subject": "Probe subject",
        "content": "<html><body><p>Hello</p><p>Line two &amp; more</p></body></html>",
        "mailbox_name": "Inbox",
    }
    long = dict(
        probe,
        message_id="probe-2",
        content="x" * 50_010,
        cc_recipients=[{"name": "", "address": "cc@example.com"}],
    )

    def fingerprint(email):
        text = _TAG.sub("untrusted_TAG", prompt.build_extraction_prompt(email))
        return hashlib.sha256(text.encode()).hexdigest()

    assert fingerprint(probe) == "4aa181a46c3884fb5dcff56ab6b56d3363d011fd8641619c9737f3db4600e669"
    assert fingerprint(long) == "41e8e716dd355690c9d16c9a7af0c88032cb6d6a7dd757efe75a08d0e3f3651b"


# --- the v2 request ------------------------------------------------------------------


def test_v2_sends_a_cached_system_prompt_the_fenced_email_a_schema_and_an_effort(v2, model):
    model.replies = [response(reply())]

    claude_extract.extract_one(EMAIL)

    [sent] = model.calls
    route = call_shape.ROUTES["email"]
    assert sent["max_tokens"] == route.max_tokens
    assert sent["output_config"] == {
        "effort": route.effort,
        "format": {"type": "json_schema", "schema": schema_v2.EMAIL_SCHEMA_V2},
    }
    system = sent["system"]
    assert [b["type"] for b in system] == ["text", "text"]
    assert [("cache_control" in b) for b in system] == [False, True]
    assert system[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    [turn] = sent["messages"]
    tag = _TAG.search(turn["content"]).group(0)
    inside = turn["content"].split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
    assert "Subject: Q4 vendor review" in inside
    assert "Could you send me your comments" in inside
    # The instructions are in the system prompt, not in the user turn.
    assert "owner_role" in system[0]["text"]
    assert "owner_role" not in turn["content"]


def test_v2_system_prompt_is_the_same_bytes_for_every_email(v2, model):
    model.replies = [response(reply()), response(reply())]

    claude_extract.extract_one(EMAIL)
    claude_extract.extract_one(OTHER)

    first, second = model.calls
    assert first["system"] == second["system"]
    assert first["output_config"] == second["output_config"]
    assert call_shape.prompt_hash(first["system"], first["output_config"]["format"]) == (
        call_shape.prompt_hash(second["system"], second["output_config"]["format"])
    )
    assert first["messages"] != second["messages"]


def test_v2_system_prompt_is_long_enough_to_cache():
    """Sonnet 5.5 caches nothing under 512 tokens, silently. Greek and English text
    run about four characters a token at most, so 4,096 characters is a safe margin."""
    system = prompt.build_email_request_v2(EMAIL).system

    assert sum(len(block["text"]) for block in system) >= 4096


def test_v2_system_prompt_holds_the_identity_and_the_payment_rule(monkeypatch):
    monkeypatch.setattr(prompt, "USER_NAME", "Probe Owner")
    monkeypatch.setattr(prompt, "USER_ROLE", "Head of Probes")

    instructions = prompt.build_email_request_v2(EMAIL).system[0]["text"]

    assert "Probe Owner" in instructions and "Head of Probes" in instructions
    assert prompt.PROMPT_RULE in instructions


def test_v2_source_is_the_email_as_the_model_reads_it():
    request = prompt.build_email_request_v2(EMAIL)

    assert "Subject: Q4 vendor review" in request.source
    assert request.source in request.user.replace("&lt;", "<")
    assert "<untrusted_" not in request.source


# --- the v2 reply ------------------------------------------------------------------------


def test_v2_parses_the_reply_and_checks_its_evidence(v2, model):
    model.replies = [response(reply())]

    out = claude_extract.extract_one(EMAIL)

    assert out["message_id"] == "m-1"
    assert out["extraction_schema"] == 2
    assert out["decisions"][0]["evidence_found"] is True
    assert out["action_items"][0]["evidence_found"] is True
    assert out["action_items"][0]["deadline"] == "2026-10-16"


def test_v2_refusal_raises_a_value_error_naming_its_category(v2, model):
    """A refused email is an unusable reply, counted against its attempts as v1 counts
    one; the category says why."""
    model.replies = [response("", stop="refusal", category="general_harms")]

    with pytest.raises(ValueError, match="general_harms"):
        claude_extract.extract_one(EMAIL)
    assert len(model.calls) == 1


def test_v2_max_tokens_stop_is_asked_once_more_at_twice_the_budget(v2, model):
    model.replies = [response('{"summary": "cut', stop="max_tokens"), response(reply())]

    out = claude_extract.extract_one(EMAIL)

    assert [c["max_tokens"] for c in model.calls] == [8192, 16384]
    assert model.calls[0]["system"] == model.calls[1]["system"]
    assert out["summary"].startswith("The audit team")


def test_v2_a_second_max_tokens_stop_fails_the_email(v2, model):
    model.replies = [
        response('{"summary": "cut', stop="max_tokens"),
        response('{"summary": "cut again', stop="max_tokens"),
    ]

    with pytest.raises(ValueError, match="max_tokens"):
        claude_extract.extract_one(EMAIL)
    assert len(model.calls) == 2


def test_each_billed_call_of_a_retry_is_logged(v2, model, tmp_path):
    model.replies = [response('{"summary": "cut', stop="max_tokens"), response(reply())]

    claude_extract.extract_one(EMAIL)

    lines = [json.loads(line) for p in tmp_path.glob("llm-usage-*.jsonl") for line in p.open()]
    assert [(r["stop_reason"], r["max_tokens"]) for r in lines] == [
        ("max_tokens", 8192),
        ("end_turn", 16384),
    ]
    assert {r["effort"] for r in lines} == {"high"}
    assert len({r["prompt_hash"] for r in lines}) == 1


# --- the worked examples -------------------------------------------------------------


@pytest.mark.parametrize("example", prompt.EMAIL_EXAMPLES_V2, ids=lambda e: e.title)
def test_each_worked_example_is_a_valid_answer_quoting_its_email(example):
    answer = example.answer
    text = prompt._email_text(example.email)

    assert schema_v2.problems(answer, schema_v2.EMAIL_SCHEMA_V2) == []
    for key in ("decisions", "action_items", "commitments"):
        for item in answer[key]:
            assert item["evidence"] in text


def test_the_examples_are_in_the_cached_block():
    examples = prompt.build_email_request_v2(EMAIL).system[-1]["text"]

    for example in prompt.EMAIL_EXAMPLES_V2:
        assert json.dumps(example.answer, ensure_ascii=False, indent=2) in examples
