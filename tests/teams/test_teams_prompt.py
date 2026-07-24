"""Tests for teams_prompt — build_prompt + parse_response."""

import pytest

from src.extract.teams_prompt import SYSTEM_PROMPT, build_prompt, parse_response


def test_build_prompt_includes_metadata_and_transcript():
    thread = {
        "chat_label": "Cards & Digital / Novak — Cards Strategy",
        "thread_kind": "channel_post",
        "started_at": "2026-04-29T08:00:00",
        "ended_at": "2026-04-29T08:30:00",
        "message_count": 3,
        "participants": ["Novak, Maria", "Papadopoulos, Nikos"],
    }
    messages = [
        {
            "composed_at": "2026-04-29T08:00:00",
            "sender": "Novak, Maria",
            "content": "Q2 roadmap",
        },
        {
            "composed_at": "2026-04-29T08:30:00",
            "sender": "Papadopoulos, Nikos",
            "content": "ship it",
        },
    ]
    sys_prompt, user_prompt = build_prompt(thread, messages)

    # System prompt is the constant.
    assert sys_prompt == SYSTEM_PROMPT
    # Metadata fields surface in the user prompt.
    assert "Cards & Digital / Novak — Cards Strategy" in user_prompt
    assert "channel_post" in user_prompt
    assert "Novak, Maria, Papadopoulos, Nikos" in user_prompt
    # Transcript lines present.
    assert "[2026-04-29T08:00:00] Novak, Maria: Q2 roadmap" in user_prompt
    assert "[2026-04-29T08:30:00] Papadopoulos, Nikos: ship it" in user_prompt
    # JSON shape mentioned (for the LLM contract).
    assert "summary" in user_prompt
    assert "decisions" in user_prompt
    assert "action_items" in user_prompt
    assert "key_facts" in user_prompt
    assert "sentiment" in user_prompt
    assert "language" in user_prompt


def test_build_prompt_handles_empty_participants_and_messages():
    thread = {
        "chat_label": "X / Y",
        "thread_kind": "channel_post",
        "started_at": "t1",
        "ended_at": "t2",
        "message_count": 0,
        "participants": [],
    }
    sys_prompt, user_prompt = build_prompt(thread, [])
    assert sys_prompt == SYSTEM_PROMPT
    assert "(unknown)" in user_prompt  # participants placeholder
    # No transcript lines, but the section header is still present.
    assert "MESSAGES" in user_prompt


def test_parse_response_plain_json():
    raw = '{"summary": "hi", "decisions": [], "action_items": [], "key_facts": []}'
    parsed = parse_response(raw)
    assert parsed["summary"] == "hi"
    assert parsed["decisions"] == []


def test_parse_response_strips_code_fences():
    raw = '```json\n{"summary": "hi"}\n```'
    parsed = parse_response(raw)
    assert parsed == {"summary": "hi"}


def test_parse_response_strips_bare_code_fence():
    raw = '```\n{"summary": "hi"}\n```'
    parsed = parse_response(raw)
    assert parsed == {"summary": "hi"}


def test_parse_response_raises_on_garbage():
    import json as json_mod

    with pytest.raises(json_mod.JSONDecodeError):
        parse_response("not json at all")


def test_build_prompt_greek_content_round_trips_utf8():
    """Greek transcript content must survive verbatim through the user prompt."""
    thread = {
        "chat_label": "Cards & Digital / Στρατηγική Καρτών",
        "thread_kind": "channel_post",
        "started_at": "2026-04-29T08:00:00",
        "ended_at": "2026-04-29T09:00:00",
        "message_count": 2,
        "participants": ["Βολιώτη, Μαρία", "Παπαδόπουλος, Νίκος"],
    }
    messages = [
        {
            "composed_at": "2026-04-29T08:00:00",
            "sender": "Βολιώτη, Μαρία",
            "content": "Καλημέρα — οδικός χάρτης Q2",
        },
        {
            "composed_at": "2026-04-29T08:30:00",
            "sender": "Παπαδόπουλος, Νίκος",
            "content": "Εγκρίνω, προχωράμε.",
        },
    ]
    sys_prompt, user_prompt = build_prompt(thread, messages)

    # Greek metadata, participants, and transcript content all present verbatim.
    assert "Στρατηγική Καρτών" in user_prompt
    assert "Βολιώτη, Μαρία" in user_prompt
    assert "Παπαδόπουλος, Νίκος" in user_prompt
    assert "Καλημέρα — οδικός χάρτης Q2" in user_prompt
    assert "Εγκρίνω, προχωράμε." in user_prompt
    # System prompt now mentions language matching.
    assert "language" in sys_prompt.lower()
