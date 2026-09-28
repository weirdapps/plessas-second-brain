"""WhatsApp is visible everywhere Teams is: recall, search, decisions, actions,
person context, stats coverage and the MCP tools."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.export.whatsapp_export import ingest_snapshot
from src.extract.whatsapp_pipeline import extract_threads
from src.extract.whatsapp_threads import bound_threads
from src.store.action_lifecycle import dedup_exact_open_actions
from src.store.context import get_person_context
from src.store.query import find_overdue_actions, get_coverage, query_action_items, query_decisions
from src.store.recall import recall
from src.store.whatsapp_query import search_whatsapp
from tests.whatsapp.conftest import ALICE, DIRECT_JID, GROUP_JID, OWNER, build_snapshot

REPLY = json.dumps(
    {
        "summary": "Agreed to sail to the island on Saturday.",
        "decisions": [{"decision": "Sail to the island on Saturday", "decided_by": "Alice Example"}],
        "action_items": [
            {"task": "Book the mooring for the island trip", "owner": "Alice Example",
             "deadline": "2026-01-10"}
        ],
        "key_facts": [],
        "sentiment": "positive",
        "language": "en",
    }
)  # fmt: skip


def _ts(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S+00:00")


@pytest.fixture
def filled(db, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    monkeypatch.setattr("src.extract.whatsapp_pipeline.touch_sentinel", lambda: None)
    rows = [
        ("m1", DIRECT_JID, ALICE, "Shall we take the boat to the island on Saturday morning?", _ts(3), 0, "", ""),
        ("m2", DIRECT_JID, OWNER, "Yes, if the forecast is good. I will bring the sails and lunch.", _ts(3), 1, "", ""),
        ("m3", GROUP_JID, ALICE, "Η παρουσίαση για το σχολείο είναι έτοιμη", _ts(40), 0, "", ""),
    ]  # fmt: skip
    ingest_snapshot(db, build_snapshot(tmp_path, rows))
    bound_threads(db)
    with patch("src.extract.whatsapp_pipeline._call_llm", return_value=REPLY):
        extract_threads(db)
    return db


def test_recall_returns_a_whatsapp_hit(filled):
    out = recall(filled, "island")
    assert out["whatsapp"], out["summary"]
    assert "whatsapp" in out["summary"]["kinds_with_results"]
    assert out["whatsapp"][0]["chat_name"] == "Alice Example"


def test_recall_finds_raw_message_text_too(filled):
    out = recall(filled, "σχολείο")
    assert any(hit["match"] == "message" for hit in out["whatsapp"])


def test_search_ignores_greek_accents(filled):
    hits = search_whatsapp(filled, "παρουσιαση")
    assert hits and hits[0]["chat_name"] == "Chat A"


def test_search_filters_by_chat_and_by_days(filled):
    assert search_whatsapp(filled, "island", chat="Alice")
    assert not search_whatsapp(filled, "island", chat="Chat A")
    assert search_whatsapp(filled, "παρουσίαση", days=60)
    assert not search_whatsapp(filled, "παρουσίαση", days=10)


def test_decisions_and_actions_carry_their_whatsapp_source(filled):
    decisions = query_decisions(filled, limit=10)
    assert [d["source"] for d in decisions] == ["whatsapp"]
    assert decisions[0]["email_subject"].startswith("WhatsApp: Alice Example")
    actions = query_action_items(filled, limit=10)
    assert [a["source"] for a in actions] == ["whatsapp"]
    assert query_action_items(filled, limit=10, sources=("whatsapp",))
    assert not query_action_items(filled, limit=10, sources=("email",))


def test_recall_decisions_bucket_names_whatsapp(filled):
    out = recall(filled, "island Saturday")
    assert out["decisions"] and out["decisions"][0]["source"] == "whatsapp"


def test_overdue_whatsapp_actions_are_listed(filled):
    assert [a["source"] for a in find_overdue_actions(filled)] == ["whatsapp"]


def test_the_dedup_never_merges_actions_of_two_sessions(filled):
    filled.execute(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at) "
        "VALUES (1, 'other', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')"
    )
    tid = filled.execute("SELECT MAX(id) FROM whatsapp_threads").fetchone()[0]
    filled.execute(
        "INSERT INTO action_items (task, owner, deadline, status, whatsapp_thread_id) "
        "VALUES ('Book the mooring for the island trip', 'Alice Example', '2026-01-10', 'open', ?)",
        (tid,),
    )
    assert dedup_exact_open_actions(filled) == 0


def test_person_context_shows_what_they_wrote_on_whatsapp(filled):
    filled.execute("INSERT INTO people (name, email) VALUES ('Alice Example', 'alice@example.com')")
    ctx = get_person_context(filled, "Alice Example", days=365)
    assert ctx["whatsapp"]["message_count"] == 2
    assert ctx["whatsapp"]["recent_threads"][0]["chat"] == "Alice Example"


def test_a_one_word_name_never_claims_someone_elses_messages(filled):
    filled.execute("INSERT INTO people (name, email) VALUES ('Alice', 'a@example.com')")
    assert get_person_context(filled, "Alice", days=365)["whatsapp"]["message_count"] == 0


def test_coverage_says_where_whatsapp_starts_and_ends(filled):
    whatsapp = get_coverage(filled)["whatsapp"]
    assert whatsapp["items"] == 3
    assert whatsapp["first"] < whatsapp["last"]


def test_the_mcp_tool_searches_whatsapp(filled, monkeypatch):
    from src import mcp_server

    monkeypatch.setattr(mcp_server, "_get_conn", lambda: filled)
    out = mcp_server.search_whatsapp("island", chat="Alice", days=30, limit=5)
    assert out["results"] and out["results"][0]["chat_name"] == "Alice Example"


def test_the_mcp_routing_text_no_longer_calls_whatsapp_uncovered():
    from src.mcp_server import _INSTRUCTIONS

    not_covered = _INSTRUCTIONS.split("Not covered:", 1)[1]
    assert "plus WhatsApp" not in not_covered
    assert "last hour" in not_covered  # only the unsynced tail sits elsewhere
    assert "search_whatsapp" in _INSTRUCTIONS
