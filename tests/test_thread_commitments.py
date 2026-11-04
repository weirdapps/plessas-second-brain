"""Commitments beyond email (issue 168, part 3): Teams threads and WhatsApp sessions give
commitments as mail does, into the same table under a parent column of their own (schema
v43, gated with v42). A store before v43 has no such column, and its Teams and WhatsApp
extractions write no commitment, as before. Every value is invented.
"""

import json
from unittest.mock import patch

import pytest

from src import config
from src.extract import teams_prompt, whatsapp_prompt
from src.extract.teams_pipeline import extract_threads as extract_teams
from src.extract.teams_repair import THREAD_ITEMS
from src.extract.whatsapp_pipeline import extract_threads as extract_whatsapp
from src.store import ledger
from src.store.schema import create_database, get_schema_version, run_migrations
from tests.teams.test_teams_pipeline import _seed_thread
from tests.whatsapp.test_pipeline import _chat

PROMISE = {
    "commitment": "Send the signed contract to Bob",
    "by": "Alice",
    "to": "Bob",
    "due": "2026-05-15",
}


def _reply(*commitments):
    return json.dumps(
        {
            "summary": "Alice will send the contract.",
            "decisions": [],
            "action_items": [],
            "key_facts": [],
            "commitments": list(commitments),
            "sentiment": "neutral",
            "language": "en",
        }
    )


@pytest.fixture(autouse=True)
def vertex(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    monkeypatch.setattr("src.extract.whatsapp_pipeline.touch_sentinel", lambda: None)
    monkeypatch.setattr(config, "USER_NAME", "Olga Owner")


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


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_v43_is_gated_with_the_ledger(old):
    assert "teams_thread_id" not in _columns(old, "commitments")
    run_migrations(old, gated=True)
    assert get_schema_version(old) == config.CURRENT_SCHEMA_VERSION == 43
    assert {"teams_thread_id", "whatsapp_thread_id"} <= _columns(old, "commitments")


def test_the_prompt_asks_for_commitments_only_on_a_store_that_holds_them(store, old):
    seen = []

    def llm(system, user):
        seen.append((system, user))
        return _reply(PROMISE)

    for conn in (old, store):
        _seed_thread(conn)
        with patch("src.extract.teams_pipeline._call_llm", side_effect=llm):
            extract_teams(conn, workers=1)
    (old_system, old_user), (new_system, new_user) = seen
    assert '"commitments"' not in old_user and "commitment" not in old_system.lower()
    assert '"commitments"' in new_user and teams_prompt.COMMITMENT_RULE in new_system
    assert old_system == teams_prompt.SYSTEM_PROMPT


def test_with_commitments_keeps_both_prompts_otherwise_whole():
    for module in (teams_prompt, whatsapp_prompt):
        system, user = module.build_prompt(
            {"chat_label": "c", "participants": ["Alice"]},
            [{"composed_at": "2026-04-29T08:00:00", "sender": "Alice", "content": "hi"}],
        )
        wide_system, wide_user = teams_prompt.with_commitments(system, user)
        assert wide_system.startswith(system)
        assert wide_user.replace(teams_prompt._COMMITMENTS_KEY, "") == user
        assert wide_user.index('"commitments"') < wide_user.index('"key_facts"')


def test_a_teams_thread_gives_commitments(store):
    thread = _seed_thread(store)
    with patch("src.extract.teams_pipeline._call_llm", return_value=_reply(PROMISE)):
        assert extract_teams(store, workers=1)["extracted"] == 1

    row = store.execute(
        "SELECT commitment, by_person, to_person, due, status, email_id FROM commitments"
        " WHERE teams_thread_id = ?",
        (thread,),
    ).fetchone()
    assert tuple(row) == (
        "Send the signed contract to Bob",
        "Alice",
        "Bob",
        "2026-05-15",
        "open",
        None,
    )
    ledger.reconcile(store)
    entry = store.execute("SELECT parent_key, status FROM item_ledger").fetchone()
    assert tuple(entry) == (f"teams_thread:{thread}", "open")


def test_extracting_a_teams_thread_again_replaces_its_commitments(store):
    thread = _seed_thread(store)
    with patch("src.extract.teams_pipeline._call_llm", return_value=_reply(PROMISE)):
        extract_teams(store, workers=1)
    store.execute("UPDATE teams_threads SET extraction_status = 'pending' WHERE id = ?", (thread,))
    store.commit()
    other = PROMISE | {"commitment": "Call Bob"}
    with patch("src.extract.teams_pipeline._call_llm", return_value=_reply(other)):
        extract_teams(store, workers=1)

    rows = store.execute(
        "SELECT commitment FROM commitments WHERE teams_thread_id = ?", (thread,)
    ).fetchall()
    assert [r[0] for r in rows] == ["Call Bob"]


def test_a_store_before_v43_writes_no_thread_commitment(old):
    _seed_thread(old)
    with patch("src.extract.teams_pipeline._call_llm", return_value=_reply(PROMISE)):
        assert extract_teams(old, workers=1)["extracted"] == 1
    assert old.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0


def test_a_whatsapp_session_gives_commitments(store, tmp_path):
    _chat(tmp_path, store)
    promise = {"commitment": "Bring the sails", "by": "Olga Owner", "to": "Alice", "due": None}
    with patch("src.extract.whatsapp_pipeline._call_llm", return_value=_reply(promise)):
        assert extract_whatsapp(store)["extracted"] == 1

    row = store.execute(
        "SELECT commitment, by_person, to_person, whatsapp_thread_id FROM commitments"
    ).fetchone()
    assert tuple(row)[:3] == ("Bring the sails", "Olga Owner", "Alice")
    assert row[3] is not None


def test_a_commitment_with_no_text_is_left_out(store):
    _seed_thread(store)
    with patch(
        "src.extract.teams_pipeline._call_llm",
        return_value=_reply({"commitment": "", "by": "Alice"}, "not a dict"),
    ):
        extract_teams(store, workers=1)
    assert store.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0


def test_the_teams_repair_clears_commitments_too():
    assert "commitments" in THREAD_ITEMS


def test_a_thread_commitment_has_a_counterpart_by_name(store):
    thread = _seed_thread(store)
    store.execute("INSERT INTO people (name, email) VALUES ('Bob', 'bob@corp.example')")
    store.commit()
    owed = PROMISE | {"by": "Olga Owner", "to": "Bob"}
    with patch("src.extract.teams_pipeline._call_llm", return_value=_reply(owed)):
        extract_teams(store, workers=1)
    ledger.reconcile(store)
    bob = store.execute("SELECT id FROM people WHERE name = 'Bob'").fetchone()[0]
    row = store.execute(
        "SELECT counterpart_person_id FROM commitments WHERE teams_thread_id = ?", (thread,)
    ).fetchone()
    assert row[0] == bob
