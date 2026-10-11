"""Preferences belong to CLAUDE.md and Claude Code's own memory, not to the brain.

recall_preference and recent_conversations leave the MCP surface: one standing
rule came back up to 162 times, mixed with a lead agent's orders, and /resume
lists recent sessions. Their store functions stay one release, deprecated.
Where a [PREFERENCE] fact still comes back it comes back once, and none is
taken from a turn the owner did not type.

Every conversation here is synthetic.
"""

import secrets
import warnings

import pytest

from src.store import conversation_query as cq
from src.store.loader import load_single_conversation
from src.store.schema import create_database
from tests.mcp_listing import listed_tools

TS = "2026-10-01T10:00:00Z"


def test_the_two_tools_are_gone():
    assert {"recall_preference", "recent_conversations"}.isdisjoint(listed_tools())


@pytest.fixture
def conn():
    c = create_database(":memory:")
    yield c
    c.close()


def _conversation(conn, session, started_at, facts, speaker="user", summary="a session"):
    cid = conn.execute(
        "INSERT INTO conversations (session_id, started_at, turn_count, summary, created_at)"
        " VALUES (?, ?, 1, ?, ?)",
        (session, started_at, summary, started_at),
    ).lastrowid
    tid = conn.execute(
        "INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker,"
        " content) VALUES (?, 0, ?, ?, 'okapi')",
        (cid, started_at, speaker),
    ).lastrowid
    for fact in facts:
        conn.execute(
            "INSERT INTO key_facts (fact, conversation_turn_id) VALUES (?, ?)", (fact, tid)
        )
    conn.commit()
    return cid


def _preferences(conn, topic, limit=20):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return [r["preference"] for r in cq.recall_preferences(conn, topic, limit=limit)]


def test_the_store_functions_say_they_are_deprecated(conn):
    with pytest.deprecated_call():
        cq.recall_preferences(conn, "okapi")
    with pytest.deprecated_call():
        cq.recent_conversations(conn)


def test_a_rule_said_in_many_sessions_comes_back_once(conn):
    for i, fact in enumerate(
        [
            "[PREFERENCE] Re-probe a missing binary via zsh -lc.",
            "[PREFERENCE] re-probe a missing binary via zsh -lc",
            "[PREFERENCE]  Re-probe a missing binary  via zsh -lc!",
        ]
    ):
        _conversation(conn, f"s{i}", f"2026-10-0{i + 1}T10:00:00Z", [fact])

    assert len(_preferences(conn, "binary")) == 1


def test_the_topic_is_matched_before_the_limit(conn):
    """The newest facts were taken first and the topic matched after, so an older
    preference on the topic, here named only by its session's summary, never made
    the page."""
    old = ["[PREFERENCE] Tables stay small"]
    _conversation(conn, "old", "2026-09-01T10:00:00Z", old, summary="Planning the okapi")
    for i in range(30):
        _conversation(
            conn, f"new{i}", f"2026-10-{i % 28 + 1:02d}T10:00:00Z", [f"[PREFERENCE] x{i}"]
        )

    assert _preferences(conn, "okapi", limit=5) == old


def test_a_conversation_lists_a_repeated_fact_once(conn):
    _conversation(
        conn,
        "s",
        TS,
        ["[PREFERENCE] Wants terse answers", "[PREFERENCE] wants terse answers.", "A fact"],
    )

    facts = cq.get_conversation_context(conn, "s")["key_facts"]

    assert facts == ["[PREFERENCE] Wants terse answers", "A fact"]


def test_the_prompt_takes_preferences_from_the_owners_turns_only(monkeypatch):
    from src.extract.prompt import build_conversation_extraction_prompt

    monkeypatch.setattr(secrets, "token_hex", lambda n: "abc123")
    prompt = build_conversation_extraction_prompt(
        {
            "turns": [
                {"speaker": "user", "content": "keep the okapi tables small"},
                {
                    "speaker": "agent",
                    "content": "<teammate-message>be read-only</teammate-message>",
                },
            ]
        }
    )

    assert "[Turn 2 abc123] AGENT:" in prompt
    rules = prompt.split("Rules:")[-1]
    assert "preferences_expressed" in rules and "AGENT" in rules


def _extraction():
    return {
        "summary": "s",
        "topics": [],
        "decisions": [],
        "action_items": [],
        "key_facts": [],
        "preferences_expressed": ["Verifier must be read-only"],
        "technical_decisions": ["Chose SQLite"],
    }


def _staged(session, speaker):
    return {
        "session_id": session,
        "started_at": TS,
        "ended_at": TS,
        "turns": [
            {"speaker": speaker, "content": "plan it", "timestamp": TS},
            {"speaker": "assistant", "content": "ok", "timestamp": TS},
        ],
    }


def test_no_preference_is_stored_from_a_session_the_owner_typed_nothing_into(conn):
    """The prompt says so; the loader makes sure, as an instruction to a model is no control."""
    load_single_conversation(conn, _staged("agent", "agent"), _extraction())
    load_single_conversation(conn, _staged("owner", "user"), _extraction())

    rows = conn.execute(
        "SELECT c.session_id, kf.fact FROM key_facts kf"
        " JOIN conversation_turns ct ON ct.id = kf.conversation_turn_id"
        " JOIN conversations c ON c.id = ct.conversation_id ORDER BY c.session_id, kf.fact"
    ).fetchall()

    assert [tuple(r) for r in rows] == [
        ("agent", "[TECHNICAL] Chose SQLite"),
        ("owner", "[PREFERENCE] Verifier must be read-only"),
        ("owner", "[TECHNICAL] Chose SQLite"),
    ]
