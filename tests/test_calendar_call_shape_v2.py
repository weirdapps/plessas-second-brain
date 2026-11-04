"""The calendar route under call shapes v2 and v2-text (issue 211).

It keeps the three things issue 203 relies on: two answer shapes (an invitation is asked
what a meeting is for, a past meeting for decisions from its notes), each with its own
schema chosen by is_invitation; the instruction text inside event_prompt_hash, so an
edit of the prompt re-extracts; and one hash per series, so series_extraction still
reuses one reading. v1 sends the calendar call as it always did.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest

from src.extract import calendar_extractor, call_shape, claude_extract
from src.extract.calendar_extractor import event_prompt_hash, extract_event
from src.extract.schema_v2 import CALENDAR_INVITATION_SCHEMA_V2, CALENDAR_MEETING_SCHEMA_V2
from tests.test_calendar_invites import _AGENDA, _meeting, _when

NOTES = (
    "Minutes of the review. Agreed: the card budget rises 10% from 1 July."
    " Maria will send the revised plan by 2026-07-03."
)
MEETING_ANSWER = {
    "body_summary": "The review agreed a budget rise.",
    "decisions": [
        {
            "evidence": "Agreed: the card budget rises 10% from 1 July.",
            "decision": "The card budget rises 10% from 1 July",
            "decided_by": "Chair Person",
            "decision_date": None,
        }
    ],
    "action_items": [
        {
            "evidence": "a sentence the notes never say",
            "task": "Send the revised plan",
            "owner": "Maria",
            "deadline": "2026-07-03",
        }
    ],
}
INVITATION_ANSWER = {"purpose": "Review the quarter", "agenda": ["figures"], "expected": "heads"}


def response(answer, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(answer))],
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
    return stub


PAST = _meeting(-3)
AHEAD = _meeting(3)


def test_v1_sends_the_calendar_call_as_it_always_did(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v1")
    model.replies = [response(INVITATION_ANSWER)]

    extract_event(AHEAD, _AGENDA)

    [sent] = model.calls
    assert set(sent) == {"model", "max_tokens", "messages"}
    assert sent["max_tokens"] == calendar_extractor.MAX_TOKENS
    assert sent["messages"][0]["content"].startswith("You are reading a calendar invitation")


def test_v1_keeps_its_prompt_hash(monkeypatch):
    """The v1 hash is the one every stored extraction carries: changing it would read
    every event again the next time Outlook moves its etag."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v1")
    _, template, invite = calendar_extractor._prompt_parts(AHEAD, _AGENDA)
    expected = hashlib.sha256(f"{template}\x00{invite}".encode()).hexdigest()

    assert event_prompt_hash(AHEAD, _AGENDA) == expected


@pytest.mark.parametrize("shape", ["v2", "v2-text"])
@pytest.mark.parametrize(
    ("event", "body", "schema", "answer"),
    [
        (AHEAD, _AGENDA, CALENDAR_INVITATION_SCHEMA_V2, INVITATION_ANSWER),
        (PAST, NOTES, CALENDAR_MEETING_SCHEMA_V2, MEETING_ANSWER),
    ],
    ids=["invitation", "past meeting"],
)
def test_v2_caches_the_instructions_and_fences_the_event(
    monkeypatch, model, shape, event, body, schema, answer
):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", shape)
    model.replies = [response(answer)]

    extract_event(event, body)

    [sent] = model.calls
    blocks = sent["system"]
    assert [b.get("cache_control") for b in blocks][-1] == call_shape.CACHE_CONTROL
    assert sum("cache_control" in b for b in blocks) == 1
    [turn] = sent["messages"]
    assert body.split(".")[0] in turn["content"]
    assert turn["content"].endswith(calendar_extractor.RESTATE)
    assert "Extract the following as JSON" not in turn["content"]
    assert sent["max_tokens"] == call_shape.ROUTES["calendar"].max_tokens
    if shape == "v2":
        assert sent["output_config"] == {
            "effort": "high",
            "format": {"type": "json_schema", "schema": schema},
        }
    else:
        assert sent["output_config"] == {"effort": "high"}
        assert json.dumps(schema, ensure_ascii=False, separators=(",", ":")) in "".join(
            b["text"] for b in blocks
        )


def test_v2_keeps_the_two_answer_shapes(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    model.replies = [response(INVITATION_ANSWER), response(MEETING_ANSWER)]

    invitation = extract_event(AHEAD, _AGENDA)
    meeting = extract_event(PAST, NOTES)

    assert invitation == {
        "body_summary": "Purpose: Review the quarter. Agenda: figures. Expected: heads.",
        "decisions": [],
        "action_items": [],
    }
    assert meeting["body_summary"] == "The review agreed a budget rise."
    assert [d["decision"] for d in meeting["decisions"]] == [
        "The card budget rises 10% from 1 July"
    ]
    first, second = model.calls
    assert first["system"] != second["system"]


def test_v2_marks_whether_each_evidence_quote_is_in_the_notes(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    model.replies = [response(MEETING_ANSWER)]

    meeting = extract_event(PAST, NOTES)

    assert meeting["decisions"][0]["evidence_found"] is True
    assert meeting["action_items"][0]["evidence_found"] is False


def test_v2_asks_once_more_at_double_after_a_max_tokens_stop(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    model.replies = [response({"purpose": "cu"}, stop="max_tokens"), response(INVITATION_ANSWER)]

    extract_event(AHEAD, _AGENDA)

    budget = call_shape.ROUTES["calendar"].max_tokens
    assert [c["max_tokens"] for c in model.calls] == [budget, 2 * budget]


def test_v2_a_second_cut_short_reply_fails(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    model.replies = [response({}, stop="max_tokens"), response({}, stop="max_tokens")]

    with pytest.raises(ValueError, match="max_tokens"):
        extract_event(AHEAD, _AGENDA)


def test_v2_a_refusal_raises(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    model.replies = [response({}, stop="refusal")]

    with pytest.raises(claude_extract.ExtractionRefused):
        extract_event(PAST, NOTES)


def test_v2_the_effort_override_reaches_the_calendar(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    monkeypatch.setenv("BRAIN_EXTRACT_EFFORT_CALENDAR", "medium")
    model.replies = [response(INVITATION_ANSWER)]

    extract_event(AHEAD, _AGENDA)

    assert model.calls[0]["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("shape", ["v2", "v2-text"])
def test_v2_occurrences_of_one_series_share_one_hash(monkeypatch, shape):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", shape)
    monday = _meeting(-7, is_recurring=True, recurrence_master_id="series-1")
    tuesday = {**monday, "outlook_event_id": "ev-2", "start_at": _when(-6), "end_at": _when(-6)}

    assert event_prompt_hash(monday, _AGENDA) == event_prompt_hash(tuesday, _AGENDA)


def test_the_hash_moves_with_the_call_shape(monkeypatch):
    """What the model reads differs by shape, so an extraction made under one is not on
    record for another."""
    hashes = set()
    for shape in ("v1", "v2", "v2-text"):
        monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", shape)
        hashes.add(event_prompt_hash(AHEAD, _AGENDA))

    assert len(hashes) == 3


def test_v2_the_hash_holds_the_instruction_text(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    before = event_prompt_hash(PAST, NOTES)

    monkeypatch.setattr(
        calendar_extractor, "_PROMPT_TEMPLATE", calendar_extractor._PROMPT_TEMPLATE + " Edited."
    )

    assert event_prompt_hash(PAST, NOTES) != before


def test_v2_the_hash_holds_the_worked_examples(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    before = event_prompt_hash(AHEAD, _AGENDA)

    edited = calendar_extractor.EXAMPLES[True] + "\nEdited."
    monkeypatch.setitem(calendar_extractor.EXAMPLES, True, edited)

    assert event_prompt_hash(AHEAD, _AGENDA) != before


def test_v2_the_hash_leaves_out_the_fence_tag(monkeypatch):
    """The tag is drawn per call; the hash must not move with it."""
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")

    assert event_prompt_hash(PAST, NOTES) == event_prompt_hash(PAST, NOTES)


def test_v2_no_text_costs_no_call(monkeypatch, model):
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")

    assert extract_event(AHEAD, "short") == {
        "body_summary": "",
        "decisions": [],
        "action_items": [],
    }
    assert event_prompt_hash(AHEAD, "short") is None
    assert model.calls == []


@pytest.mark.parametrize("invitation", [True, False])
def test_each_worked_answer_is_valid_against_its_schema(invitation):
    from src.extract.schema_v2 import problems

    schema = CALENDAR_INVITATION_SCHEMA_V2 if invitation else CALENDAR_MEETING_SCHEMA_V2
    for example_answer in calendar_extractor.EXAMPLE_ANSWERS[invitation]:
        assert problems(example_answer, schema) == []


@pytest.fixture
def db_conn(tmp_path):
    from src.store.schema import create_database

    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def test_the_loader_keeps_a_found_quote_and_drops_a_paraphrase(db_conn):
    """The v35 evidence columns hold only passages the event's text states, as for mail."""
    from src.store.calendar_loader import load_event
    from tests.test_calendar_loader import SAMPLE_EVENT

    extraction = {
        "body_summary": "s",
        "decisions": [{**MEETING_ANSWER["decisions"][0], "evidence_found": True}],
        "action_items": [{**MEETING_ANSWER["action_items"][0], "evidence_found": False}],
    }

    event_id = load_event(db_conn, SAMPLE_EVENT, extraction, llm_status="extracted")

    [(decision,)] = db_conn.execute(
        "SELECT evidence FROM decisions WHERE event_id = ?", (event_id,)
    ).fetchall()
    [(action,)] = db_conn.execute(
        "SELECT evidence FROM action_items WHERE event_id = ?", (event_id,)
    ).fetchall()
    assert decision == "Agreed: the card budget rises 10% from 1 July."
    assert action is None
