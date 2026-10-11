"""The attachment route under each call shape.

v1 sends what it always sent. v2 sends one cached system prompt for every attachment
call, a single text, a spreadsheet digest, a part or the merge of the parts alike, with
the document fenced in the user turn and the pipeline's notes (part, digest, cut short,
merge) outside the fence; schema v2 as structured output; a pinned effort; and one more
call at twice the budget after a max_tokens stop, each billed call charged to the run's
token budget. The client is stubbed at the one call site.
"""

import json
import re
from types import SimpleNamespace

import pytest

from src.extract import attachment_pipeline as ap
from src.extract import attachment_prompt, call_shape, claude_extract, schema_v2
from src.llm_cost import TokenBudget
from tests.patching import unset_env

_TAG = re.compile(r"untrusted_[0-9a-f]+")


def reply(summary="A summary.", **over):
    answer = {
        "language": "en",
        "summary": summary,
        "topics": ["policy"],
        "decisions": [],
        "action_items": [],
        "key_facts": ["Payments above 50,000 euros need two signatories"],
    }
    answer.update(over)
    return json.dumps(answer)


def response(text, stop="end_turn", category=None, tokens=(100, 10)):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        stop_details=SimpleNamespace(category=category) if category else None,
        model="claude-sonnet-5-5",
        usage=SimpleNamespace(
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture
def model(monkeypatch, tmp_path):
    """`calls` holds each request as the SDK gets it; `answer(kwargs)` makes the reply."""
    stub = SimpleNamespace(calls=[], answer=lambda kwargs: response(reply()))

    def create(client, *, after_refusal=None, **kwargs):
        stub.calls.append(kwargs)
        return stub.answer(kwargs)

    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (object(), "m"))
    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", create)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    return stub


@pytest.fixture
def v2(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")


def _row(text="Policy text. Payments above 50,000 euros need two signatories.", method=None):
    return (1, 2, text, "policy.pdf", "application/pdf", 3, "Updated policy", "2026-05-12", method)


def _long_text():
    """Over LONG_TEXT_CHARS, so it goes in parts: four of them, every one read."""
    paragraph = "Paragraph about the plan. " + "word " * 400
    return "\n\n".join([paragraph] * 70)[:130_000]


def _turn(sent):
    [turn] = sent["messages"]
    return turn["content"]


def _fenced(turn):
    tag = _TAG.search(turn).group(0)
    before, rest = turn.split(f"<{tag}>", 1)
    inside, after = rest.split(f"</{tag}>", 1)
    return before, inside, after


# --- v1 stays as it was -----------------------------------------------------------------


def test_v1_sends_the_v1_attachment_prompt_alone(monkeypatch, model):
    unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")

    ap._extract_one_attachment(_row())

    [sent] = model.calls
    assert set(sent) == {"model", "max_tokens", "messages"}
    assert sent["max_tokens"] == 8192
    expected = attachment_prompt.build_attachment_prompt(
        extracted_text=_row()[2],
        filename="policy.pdf",
        mime_type="application/pdf",
        email_subject="Updated policy",
        email_date="2026-05-12",
    )
    assert _TAG.sub("T", _turn(sent)) == _TAG.sub("T", expected)


# --- v2 ------------------------------------------------------------------------------------


def test_v2_sends_the_cached_system_prompt_the_fenced_document_a_schema_and_an_effort(v2, model):
    ac_id, email_id, extraction, error, _ = ap._extract_one_attachment(_row())

    assert error is None and extraction["extraction_schema"] == 2
    [sent] = model.calls
    route = call_shape.ROUTES["attachment"]
    assert sent["max_tokens"] == route.max_tokens
    assert sent["output_config"] == {
        "effort": route.effort,
        "format": {"type": "json_schema", "schema": schema_v2.ATTACHMENT_SCHEMA_V2},
    }
    assert [("cache_control" in b) for b in sent["system"]] == [False, True]
    before, inside, _after = _fenced(_turn(sent))
    assert "Attachment filename: policy.pdf" in inside
    assert "Payments above 50,000 euros" in inside
    assert "Parent email subject: Updated policy" in inside
    assert "Pipeline note" not in before


def test_v2_the_digest_note_is_outside_the_fence(v2, model):
    ap._extract_one_attachment(_row(text=_long_text(), method="openpyxl"))

    [sent] = model.calls
    before, inside, _ = _fenced(_turn(sent))
    assert "digest of a spreadsheet" in before
    assert "digest of a spreadsheet" not in inside


def test_v2_parts_name_their_part_and_the_merge_combines_them(v2, model):
    ac_id, _, extraction, error, _ = ap._extract_one_attachment(_row(text=_long_text()))

    assert error is None
    *parts, merge = model.calls
    assert len(parts) == len(attachment_prompt.split_text(_long_text()))
    for sent in parts:
        before, _inside, _ = _fenced(_turn(sent))
        assert re.search(r"part \d of \d", before)
    before, inside, _ = _fenced(_turn(merge))
    assert "extractions of" in before
    assert "Extraction 1 of" in inside
    # What the parts' extractions carry for the loader stays out of the merge.
    assert "extraction_schema" not in inside and "evidence_found" not in inside
    assert extraction["extraction_schema"] == 2


def test_v2_every_attachment_call_shares_one_system_prompt(v2, model):
    ap._extract_one_attachment(_row())
    ap._extract_one_attachment(_row(text=_long_text()))

    assert len(model.calls) == 1 + len(attachment_prompt.split_text(_long_text())) + 1
    assert len({json.dumps(c["system"], sort_keys=True) for c in model.calls}) == 1


def test_v2_a_max_tokens_stop_is_asked_again_at_double_and_both_calls_are_charged(v2, model):
    replies = iter([response('{"summary": "cut', stop="max_tokens", tokens=(100, 8192))])
    model.answer = lambda kwargs: next(replies, response(reply(), tokens=(100, 50)))
    budget = TokenBudget(1_000_000)

    extraction = ap._complete_and_parse(
        ap._attachment_prompt(ap._Phase2Row(*_row()), _row()[2]), budget
    )

    assert [c["max_tokens"] for c in model.calls] == [8192, 16384]
    assert extraction["summary"] == "A summary."
    assert (budget.calls, budget.spent) == (2, 100 + 8192 + 100 + 50)


def test_v2_a_second_max_tokens_stop_keeps_what_the_reply_holds(v2, model):
    """As v1 does for a document: a summary salvaged from a reply cut short beats none,
    and a failed attachment is not asked again."""
    model.answer = lambda kwargs: response('{"summary": "salvaged", "topics": ["a"', "max_tokens")

    extraction = ap._complete_and_parse(ap._attachment_prompt(ap._Phase2Row(*_row()), _row()[2]))

    assert len(model.calls) == 2
    assert extraction["summary"] == "salvaged"


def test_v2_a_refusal_fails_the_attachment_naming_its_category(v2, model):
    model.answer = lambda kwargs: response("", stop="refusal", category="cyber")

    _ac, _email, extraction, error, verdict = ap._extract_one_attachment(_row())

    assert extraction is None
    assert "cyber" in error and verdict is False


def test_parts_saved_under_v1_are_not_reused_under_v2(model, monkeypatch):
    """A run cut short keeps its finished parts for the next; the next, in the other
    shape, asks for them again rather than merge extractions of two schemas."""
    text = _long_text()
    unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")
    stops = iter([False, False, True])  # time runs out after two parts

    assert (
        ap._extract_in_parts(text, "f.txt", "text/plain", None, None, lambda: next(stops), 9)
        is None
    )
    assert len(model.calls) == 2 and len(ap._load_parts(9)) == 2

    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    merged = ap._extract_in_parts(text, "f.txt", "text/plain", None, None, lambda: False, 9)

    assert merged is not None and merged["extraction_schema"] == 2
    assert len(model.calls) == 2 + len(attachment_prompt.split_text(text)) + 1


def test_the_estimate_counts_a_v2_call_with_its_system_prompt(monkeypatch):
    """--estimate sizes the calls the run would send: under v2 that includes the system
    prompt, an upper bound once the cache serves it."""
    row = ap._Phase2Row(*_row())
    unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")
    v1 = ap.estimate_rows([row], chars_per_token=1.0)
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2")
    v2 = ap.estimate_rows([row], chars_per_token=1.0)

    assert v1["calls"] == v2["calls"] == 1
    assert v2["input_tokens"] == ap._attachment_prompt(row, row.text).chars > v1["input_tokens"]


# --- the attachment system prompt ------------------------------------------------------


def test_the_attachment_system_prompt_is_long_enough_to_cache_and_holds_the_rule():
    system = attachment_prompt.attachment_system_v2()

    assert sum(len(b["text"]) for b in system) >= 4096
    assert attachment_prompt.PROMPT_RULE in system[0]["text"]


@pytest.mark.parametrize("example", attachment_prompt.ATTACHMENT_EXAMPLES_V2, ids=lambda e: e.title)
def test_each_attachment_example_is_a_valid_answer_quoting_its_document(example):
    text = attachment_prompt.document_text(**example.item)

    assert schema_v2.problems(example.answer, schema_v2.ATTACHMENT_SCHEMA_V2) == []
    for key in ("decisions", "action_items"):
        for item in example.answer[key]:
            assert item["evidence"] in text
