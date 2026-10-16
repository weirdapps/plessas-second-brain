"""Call shape v2-text: v2's prompt rules and schema v2, with no structured outputs.

On 2026-10-11 every v2 eval call failed with HTTP 400: the Vertex project's organization
policy (constraints/vertexai.allowedPartnerModelFeatures) refuses `structured_outputs`.
A probe on 2026-10-16 sent one feature per call: `output_config.effort` and
`cache_control` (five-minute and one-hour) were served, `output_config.format` was the
one refused. v2-text is v2 without it: the schema is stated in the system prompt, as a
text block before the cached examples, and the reply is parsed tolerantly by
parse_extraction_v2. It is the default; `v1` stays the override back.
"""

import json
from types import SimpleNamespace

import pytest

from src.extract import attachment_prompt, call_shape, claude_extract, prompt, schema_v2
from tests.patching import unset_env

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


def answer(**over):
    out = {
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
        "action_items": [],
        "commitments": [],
        "key_facts": [],
        "people": [],
    }
    out.update(over)
    return out


def response(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        stop_details=None,
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
    return stub


@pytest.fixture
def text_shape(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")


# --- choosing it ------------------------------------------------------------------------


def test_v2_text_is_the_default(monkeypatch):
    unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")

    assert call_shape.call_shape() == "v2-text"


@pytest.mark.parametrize("value", ["v2-text", "V2-TEXT", " v2-text "])
def test_v2_text_is_chosen_by_the_variable(monkeypatch, value):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", value)

    assert call_shape.call_shape() == "v2-text"


@pytest.mark.parametrize(
    ("shape", "schema_v2_shape", "structured"),
    [("v1", False, False), ("v2", True, True), ("v2-text", True, False)],
)
def test_which_shapes_ask_for_schema_v2_and_which_send_structured_outputs(
    shape, schema_v2_shape, structured
):
    assert call_shape.uses_schema_v2(shape) is schema_v2_shape
    assert call_shape.structured_outputs(shape) is structured


def test_the_shape_functions_read_the_variable_when_given_none(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v1")
    assert call_shape.uses_schema_v2() is False

    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    assert call_shape.structured_outputs() is True


# --- what it sends ----------------------------------------------------------------------


def test_a_route_without_structured_outputs_sends_effort_alone():
    route = call_shape.ROUTES["email"]

    assert route.output_config(structured=False) == {"effort": route.effort}
    assert "format" in route.output_config()


def test_v2_text_email_call_sends_no_output_format(text_shape, model):
    model.replies = [response(json.dumps(answer()))]

    claude_extract.extract_one(EMAIL)

    [sent] = model.calls
    assert sent["output_config"] == {"effort": call_shape.ROUTES["email"].effort}
    assert "format" not in json.dumps(sent["output_config"])


def test_v2_text_states_the_schema_in_a_cached_system_block(text_shape, model):
    model.replies = [response(json.dumps(answer()))]

    claude_extract.extract_one(EMAIL)

    [sent] = model.calls
    system = sent["system"]
    base = prompt.email_system_v2()
    assert len(system) == len(base) + 1
    assert system[0] == base[0]
    assert system[-1] == base[-1]
    assert [("cache_control" in b) for b in system] == [False, False, True]
    schema_text = system[1]["text"]
    assert json.dumps(schema_v2.EMAIL_SCHEMA_V2, ensure_ascii=False, separators=(",", ":")) in (
        schema_text
    )
    assert "JSON" in schema_text


def test_v2_text_system_prompt_is_the_same_bytes_for_every_email(text_shape, model):
    model.replies = [response(json.dumps(answer())), response(json.dumps(answer()))]

    claude_extract.extract_one(EMAIL)
    claude_extract.extract_one(dict(EMAIL, message_id="m-2", content="Another body."))

    first, second = model.calls
    assert first["system"] == second["system"]


def test_v2_and_v2_text_have_different_prompt_hashes(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    v2 = prompt.build_email_request_v2(EMAIL).kwargs(8192)
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    text = prompt.build_email_request_v2(EMAIL).kwargs(8192)

    hashes = {
        call_shape.prompt_hash(kw["system"], kw["output_config"].get("format")) for kw in (v2, text)
    }
    assert len(hashes) == 2


def test_the_request_counts_the_schema_block_in_its_characters(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    structured = prompt.build_email_request_v2(EMAIL)
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    text = prompt.build_email_request_v2(EMAIL)

    assert text.chars > structured.chars
    sent = text.kwargs(8192)
    assert text.chars == sum(len(b["text"]) for b in sent["system"]) + len(text.user)


def test_a_request_keeps_the_shape_it_was_built_under(monkeypatch):
    """A batch line is built under one shape and sent later: the request decides."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    request = prompt.build_email_request_v2(EMAIL)
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")

    assert "format" not in request.kwargs(8192)["output_config"]


# --- what it reads back -----------------------------------------------------------------


@pytest.mark.parametrize(
    "wrap",
    [
        lambda text: text,
        lambda text: f"```json\n{text}\n```",
        lambda text: f"Here is the extraction:\n{text}\nDone.",
    ],
    ids=["bare", "fenced", "with-prose"],
)
def test_v2_text_reads_the_reply_tolerantly(text_shape, model, wrap):
    model.replies = [response(wrap(json.dumps(answer(), ensure_ascii=False)))]

    out = claude_extract.extract_one(EMAIL)

    assert out["extraction_schema"] == 2
    assert out["decisions"][0]["status"] == "made"
    assert out["decisions"][0]["evidence_found"] is True
    assert out["message_id"] == "m-1"


def test_v2_text_max_tokens_stop_is_asked_once_more_at_twice_the_budget(text_shape, model):
    model.replies = [
        response('{"summary": "cut', stop="max_tokens"),
        response(json.dumps(answer())),
    ]

    claude_extract.extract_one(EMAIL)

    assert [c["max_tokens"] for c in model.calls] == [8192, 16384]
    assert all("format" not in c["output_config"] for c in model.calls)


# --- attachments --------------------------------------------------------------------------


def test_attachments_take_schema_v2_without_structured_outputs(text_shape):
    single = attachment_prompt.attachment_input(
        extracted_text="Payments above 50,000 euros need two signatories.",
        filename="policy.pdf",
        mime_type="application/pdf",
    )
    merge = attachment_prompt.merge_input(
        parts=[{"summary": "a"}, {"summary": "b"}],
        filename="policy.pdf",
        mime_type="application/pdf",
        source="the text",
    )

    for request in (single, merge):
        assert isinstance(request, call_shape.Request)
        sent = request.kwargs(8192)
        assert "format" not in sent["output_config"]
        schema = json.dumps(
            schema_v2.ATTACHMENT_SCHEMA_V2, ensure_ascii=False, separators=(",", ":")
        )
        assert any(schema in block["text"] for block in sent["system"])
        assert "cache_control" in sent["system"][-1]


def test_v1_still_sends_the_v1_prompt(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v1")
    model.replies = [response('{"summary": "s"}')]

    claude_extract.extract_one(EMAIL)

    [sent] = model.calls
    assert set(sent) == {"model", "max_tokens", "messages"}
