"""A new email's extraction with its thread's state: quotes left out, deltas reported
(issue 164).

The client is stubbed at the one call site, as in test_email_call_shape_v2. Every value
is invented.
"""

import json
from types import SimpleNamespace

import pytest

from src.extract import claude_extract, parser, prompt, quotes, schema_v2

QUOTED = (
    "From: Alice Smith <alice@example.com>\n"
    "Sent: Monday, October 5, 2026 9:12 AM\n"
    "To: Bob Jones <bob@example.com>\n"
    "Subject: Okapi launch date\n"
    "\n"
    "Can we launch the okapi plan in March instead of April? HISTORY-MARKER\n"
)
NEWEST = "Agreed, we launch in March. I sent the budget this morning.\n\nBob"
EMAIL = {
    "message_id": "m-3",
    "conversation_id": "conv-okapi",
    "date_received": "2026-10-06T09:00:00Z",
    "sender": {"name": "Bob Jones", "address": "bob@example.com"},
    "to_recipients": [{"name": "Alice Smith", "address": "alice@example.com"}],
    "cc_recipients": [],
    "subject": "RE: Okapi launch date",
    "content": NEWEST + "\n\n" + QUOTED + ("Older history. " * 300),
    "mailbox_name": "Inbox",
}
STATE = {
    "conversation_id": "conv-okapi",
    "earlier_messages": 2,
    "items": [
        {
            "handle": "D1",
            "table": "decisions",
            "id": 11,
            "line": "D1 (2026-10-05, from Alice Smith) decision proposed: Launch in April",
        },
        {
            "handle": "A1",
            "table": "action_items",
            "id": 21,
            "line": "A1 (2026-10-05, from Alice Smith) action, open, owner Bob: Send the budget",
        },
    ],
}


def _with_state(email=EMAIL, state=STATE):
    return dict(email, thread_state=state)


def reply(**over):
    answer = {
        "item_kind": "work_thread",
        "language": "en",
        "summary": "Bob agrees to launch in March and has sent the budget.",
        "topics": ["okapi"],
        "sentiment": "collaborative",
        "urgency": "low",
        "decisions": [
            {
                "evidence": "Agreed, we launch in March.",
                "decision": "Launch the okapi plan in March",
                "decided_by": "Bob Jones",
                "owner_role": "third_party",
                "status": "made",
            }
        ],
        "action_items": [],
        "commitments": [],
        "key_facts": [],
        "people": [],
        "thread_updates": [
            {
                "evidence": "Agreed, we launch in March.",
                "item": "D1",
                "change": "superseded",
                "replaced_by": "1",
            },
            {
                "evidence": "I sent the budget this morning.",
                "item": "A1",
                "change": "done",
                "replaced_by": "",
            },
        ],
    }
    answer.update(over)
    return json.dumps(answer)


def response(text):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
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
    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    monkeypatch.setenv(quotes.QUOTES_VAR, "auto")
    return stub


# --- the schema ---------------------------------------------------------------------------


def test_the_email_schema_asks_for_thread_updates_and_the_attachment_schema_does_not():
    spec = schema_v2.EMAIL_SCHEMA_V2["properties"]["thread_updates"]["items"]

    assert spec["required"] == ["evidence", "item", "change", "replaced_by"]
    assert spec["properties"]["change"]["enum"] == ["superseded", "done"]
    assert "thread_updates" not in schema_v2.ATTACHMENT_SCHEMA_V2["properties"]


def test_the_parser_keeps_known_changes_and_checks_their_quotes_in_the_newest_message():
    raw = reply(
        thread_updates=[
            {"evidence": "I sent the budget this morning.", "item": "A1", "change": "done",
             "replaced_by": ""},
            {"evidence": "HISTORY-MARKER", "item": "A2", "change": "done", "replaced_by": ""},
            {"evidence": "x", "item": "D1", "change": "deleted", "replaced_by": ""},
            {"evidence": "x", "item": "", "change": "done", "replaced_by": ""},
        ]
    )  # fmt: skip

    result = parser.parse_extraction_v2(raw, source=EMAIL["content"], newest=NEWEST)

    assert [(u["item"], u["evidence_found"]) for u in result["thread_updates"]] == [
        ("A1", True),
        ("A2", False),  # quoted history, not the newest message
    ]


def test_a_reply_without_thread_updates_parses_to_none():
    answer = json.loads(reply())
    del answer["thread_updates"]

    assert parser.parse_extraction_v2(json.dumps(answer), source="")["thread_updates"] == []


# --- the request --------------------------------------------------------------------------


def test_with_a_thread_state_the_quoted_history_is_left_out_and_the_state_is_shown():
    request = prompt.build_email_request_v2(_with_state())

    assert "HISTORY-MARKER" in request.user  # the start of the message it answers, as context
    assert "Older history. " * 120 not in request.user  # past QUOTED_CONTEXT_CHARS
    assert request.user.index("THREAD STATE") < request.user.index("Agreed, we launch in March")
    assert "D1 (2026-10-05, from Alice Smith) decision proposed: Launch in April" in request.user
    assert request.newest == NEWEST
    assert request.quotes["stripped"] is True
    assert request.quotes["left_out_chars"] > 3000


def test_without_a_thread_state_the_body_goes_whole_and_no_state_is_shown():
    request = prompt.build_email_request_v2(EMAIL)

    assert "Older history. " * 50 in request.user
    assert "THREAD STATE" not in request.user
    assert request.quotes["stripped"] is False


def test_a_state_with_earlier_messages_but_no_open_items_still_strips():
    request = prompt.build_email_request_v2(_with_state(state=dict(STATE, items=[])))

    assert request.quotes["stripped"] is True
    assert "THREAD STATE" not in request.user


def test_keep_never_strips_and_strip_always_does(monkeypatch):
    monkeypatch.setenv(quotes.QUOTES_VAR, "keep")
    assert prompt.build_email_request_v2(_with_state()).quotes["stripped"] is False

    monkeypatch.setenv(quotes.QUOTES_VAR, "strip")
    assert prompt.build_email_request_v2(EMAIL).quotes["stripped"] is True


def test_an_unknown_quotes_mode_stops_the_call(monkeypatch):
    monkeypatch.setenv(quotes.QUOTES_VAR, "sometimes")

    with pytest.raises(ValueError, match="BRAIN_EXTRACT_QUOTES"):
        prompt.build_email_request_v2(EMAIL)


def test_the_system_prompt_is_the_same_bytes_with_or_without_a_state():
    with_state = prompt.build_email_request_v2(_with_state())
    without = prompt.build_email_request_v2(EMAIL)

    assert with_state.system == without.system
    assert "thread_updates" in with_state.system[0]["text"]


def test_v1_is_left_as_it_was():
    """v1 is the way back: the body whole, no state, whatever the email carries."""
    sent = prompt.build_extraction_prompt(_with_state())

    assert "THREAD STATE" not in sent
    assert "Older history. " * 50 in sent


# --- the call -----------------------------------------------------------------------------


def test_the_extraction_carries_the_checked_updates_and_the_state_ids(model):
    model.replies = [
        response(
            reply(
                thread_updates=[
                    *json.loads(reply())["thread_updates"],
                    {"evidence": "Agreed", "item": "D9", "change": "done", "replaced_by": ""},
                    {"evidence": "not in the mail", "item": "A1", "change": "done",
                     "replaced_by": ""},
                ]
            )
        )
    ]  # fmt: skip

    extraction = claude_extract.extract_one(_with_state())

    assert [(u["item"], u["change"]) for u in extraction["thread_updates"]] == [
        ("D1", "superseded"),
        ("A1", "done"),
    ]
    assert extraction["thread_state_ids"] == {"D1": ["decisions", 11], "A1": ["action_items", 21]}
    assert extraction["quotes"]["stripped"] is True


def test_without_a_state_no_update_survives(model):
    model.replies = [response(reply())]

    extraction = claude_extract.extract_one(EMAIL)

    assert extraction["thread_updates"] == []
    assert "thread_state_ids" not in extraction
