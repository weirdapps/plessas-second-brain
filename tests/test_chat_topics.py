"""Topics of chats and meetings (issue 180, part 2): Teams threads, WhatsApp sessions and
calendar events are asked for topic tags only with BRAIN_EXTRACT_CHAT_TOPICS=1 and on a
store with the gated topic_registry step; the tags land in the registry's link tables,
mapped at load like any new topic. Without both, every prompt and every row is as before.
Every value is invented.
"""

import json
from unittest.mock import patch

import pytest

from src import config
from src.extract import calendar_extractor as ce
from src.extract import teams_prompt
from src.extract.teams_pipeline import extract_threads as extract_teams
from src.extract.whatsapp_pipeline import extract_threads as extract_whatsapp
from src.store import topic_registry as tr
from src.store.schema import create_database
from tests.teams.test_teams_pipeline import _seed_thread
from tests.whatsapp.test_pipeline import _chat


def _reply(**extra):
    return json.dumps(
        {
            "summary": "The team agreed the okapi budget.",
            "decisions": [],
            "action_items": [],
            "key_facts": [],
            "sentiment": "neutral",
            "language": "en",
            **extra,
        }
    )


@pytest.fixture(autouse=True)
def vertex(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    monkeypatch.setattr("src.extract.whatsapp_pipeline.touch_sentinel", lambda: None)
    monkeypatch.setattr(config, "USER_NAME", "Olga Owner")
    monkeypatch.setenv(tr.CHAT_TOPICS_ENV, "")


@pytest.fixture
def store(tmp_path):
    db = create_database(str(tmp_path / "brain.db"))
    yield db
    db.close()


@pytest.fixture
def old(tmp_path):
    db = create_database(str(tmp_path / "old.db"), gated=False)
    yield db
    db.close()


def _links(conn, table):
    return [
        r[0]
        for r in conn.execute(
            f"SELECT t.name FROM {table} l JOIN topics t ON t.id = l.topic_id ORDER BY t.name"
        )
    ]


def test_teams_prompts_are_unchanged_without_the_env(store):
    seen = []
    _seed_thread(store)
    with patch(
        "src.extract.teams_pipeline._call_llm",
        side_effect=lambda s, u: seen.append((s, u)) or _reply(topics=["okapi"]),
    ):
        extract_teams(store, workers=1)
    (system, user) = seen[0]
    assert '"topics"' not in user and tr.TOPICS_RULE not in system
    assert _links(store, "teams_thread_topics") == []


def test_teams_threads_get_topics_with_the_env(store, monkeypatch):
    monkeypatch.setenv(tr.CHAT_TOPICS_ENV, "1")
    seen = []
    thread = _seed_thread(store)
    with patch(
        "src.extract.teams_pipeline._call_llm",
        side_effect=lambda s, u: seen.append((s, u)) or _reply(topics=["Okapi Budget", "Cards"]),
    ):
        assert extract_teams(store, workers=1)["extracted"] == 1
    system, user = seen[0]
    assert tr.TOPICS_RULE in system and '"topics"' in user
    assert _links(store, "teams_thread_topics") == ["cards", "okapi budget"]

    store.execute("UPDATE teams_threads SET extraction_status = 'pending' WHERE id = ?", (thread,))
    store.commit()
    with patch("src.extract.teams_pipeline._call_llm", return_value=_reply(topics=["Cards"])):
        extract_teams(store, workers=1)
    assert _links(store, "teams_thread_topics") == ["cards"]


def test_the_env_does_nothing_on_a_store_without_the_step(old, monkeypatch):
    monkeypatch.setenv(tr.CHAT_TOPICS_ENV, "1")
    seen = []
    _seed_thread(old)
    with patch(
        "src.extract.teams_pipeline._call_llm",
        side_effect=lambda s, u: seen.append((s, u)) or _reply(),
    ):
        extract_teams(old, workers=1)
    assert seen[0][0] == teams_prompt.SYSTEM_PROMPT


def test_whatsapp_sessions_get_topics_with_the_env(store, tmp_path, monkeypatch):
    monkeypatch.setenv(tr.CHAT_TOPICS_ENV, "1")
    _chat(tmp_path, store)
    with patch(
        "src.extract.whatsapp_pipeline._call_llm", return_value=_reply(topics=["Sailing trip"])
    ):
        assert extract_whatsapp(store)["extracted"] == 1
    assert _links(store, "whatsapp_thread_topics") == ["sailing trip"]


# --- calendar ----------------------------------------------------------------------------------

EVENT = {
    "subject": "Okapi budget review",
    "organizer_name": "Alice",
    "attendees": [],
    "start_at": "2026-01-10T09:00:00Z",
    "end_at": "2026-01-10T10:00:00Z",
}
BODY = "Notes: we agreed the okapi budget for 2026 and Bob will circulate the final numbers."


class _Response:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"


def test_a_calendar_prompt_asks_for_topics_only_when_told(monkeypatch):
    prompts = []

    def complete(**kw):
        prompts.append(kw["messages"][0]["content"])
        return _Response(
            json.dumps(
                {
                    "body_summary": "Budget.",
                    "decisions": [],
                    "action_items": [],
                    "topics": ["Okapi budget"],
                }
            )
        )

    monkeypatch.setattr(ce, "complete", complete)
    plain = ce.extract_event(EVENT, BODY)
    asked = ce.extract_event(EVENT, BODY, topics=True)

    assert '"topics"' not in prompts[0] and '"topics"' in prompts[1]
    assert "topics" not in plain
    assert asked["topics"] == ["Okapi budget"]
    assert ce.event_prompt_hash(EVENT, BODY) != ce.event_prompt_hash(EVENT, BODY, topics=True)
    assert ce.event_prompt_hash(EVENT, BODY) == ce.event_prompt_hash(EVENT, BODY, topics=False)


def test_calendar_sync_links_a_fresh_extractions_topics_with_the_env(tmp_path, monkeypatch):
    import sqlite3
    import types

    from src.export import calendar_export
    from src.pipeline import calendar_sync
    from tests.pipeline.test_calendar_sync import Outlook, _event

    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    calls = []

    def extract(event, body_html, topics=False):
        calls.append(topics)
        return {
            "body_summary": "Okapi",
            "decisions": [],
            "action_items": [],
            "topics": ["Okapi plan"],
        } | ({} if topics else {"topics": None})

    monkeypatch.setattr(calendar_export, "run_outlook_cli", Outlook([_event("ev1")]))
    monkeypatch.setattr("src.extract.calendar_extractor.extract_event", extract)
    monkeypatch.setattr(calendar_sync, "DATA_ROOT", tmp_path)
    monkeypatch.setenv(tr.CHAT_TOPICS_ENV, "1")
    args = {
        "db": str(db),
        "backfill": False,
        "since": None,
        "until": None,
        "skip_extraction": False,
        "dry_run": False,
    }

    assert calendar_sync.run(types.SimpleNamespace(**args)) == 0

    assert calls == [True]
    conn = sqlite3.connect(str(db))
    names = conn.execute(
        "SELECT t.name FROM event_topics l JOIN topics t ON t.id = l.topic_id"
    ).fetchall()
    conn.close()
    assert names == [("okapi plan",)]
