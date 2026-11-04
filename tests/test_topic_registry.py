"""Topic registry (issue 180), the gated step `topic_registry`.

146k flat topics, most used once, synonyms split. The registry clusters raw topics into
canonical topics (fold, then embedding), names each cluster (the LLM for a cluster of
several names, the most used name otherwise), keeps every raw topic as an alias of its
canonical one, maps a new topic at load when its folded words match an alias, and lets
topic_context read the whole alias set. Teams threads, WhatsApp sessions and calendar
events get topic links. A store without the step behaves exactly as before. Every value
is invented.
"""

import json

import numpy as np
import pytest

from src.store import topic_registry as tr
from src.store.context import get_topic_context
from src.store.normalizer import find_or_create_topic
from src.store.schema import GATED_MIGRATIONS, apply_gated, create_database, pending_gated
from tests import factories as f


@pytest.fixture
def old(tmp_path):
    db = create_database(str(tmp_path / "old.db"), gated=False)
    yield db
    db.close()


@pytest.fixture
def conn(tmp_path):
    db = create_database(str(tmp_path / "brain.db"))
    yield db
    db.close()


def _tag(conn, name, times):
    for _ in range(times):
        f.email(conn, subject=f"About {name}", topics=(name,))
    return conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchone()[0]


def _vectors(table):
    """vectors_of from a name -> 2-d direction table; unknown names get their own axis."""
    extra: dict[str, np.ndarray] = {}

    def vectors_of(names):
        out = []
        for n in names:
            if n in table:
                v = np.asarray(table[n], dtype=np.float32)
            else:
                v = extra.setdefault(n, np.eye(64, dtype=np.float32)[2 + len(extra) % 62])
            if v.shape[0] < 64:
                v = np.pad(v, (0, 64 - v.shape[0]))
            out.append(v / np.linalg.norm(v))
        return np.stack(out)

    return vectors_of


# --- the gated step ----------------------------------------------------------------------------


def test_the_step_is_gated_and_last(old):
    assert "topic_registry" in GATED_MIGRATIONS
    assert "topic_registry" in pending_gated(old)
    assert not tr.enabled(old)


def test_the_step_adds_tables_and_changes_no_row(old):
    _tag(old, "credit cards", 3)
    before = old.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    links = old.execute("SELECT COUNT(*) FROM email_topics").fetchone()[0]

    apply_gated(old)
    old.commit()

    assert tr.enabled(old)
    assert old.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == before
    assert old.execute("SELECT COUNT(*) FROM email_topics").fetchone()[0] == links
    for table in tr.TABLES:
        assert old.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_a_new_store_has_the_registry(conn):
    assert tr.enabled(conn)


# --- without the step: as before ---------------------------------------------------------------


def test_without_the_step_a_new_topic_is_not_mapped_and_reads_are_unchanged(old):
    a = _tag(old, "credit cards", 2)
    tid = find_or_create_topic(old, "Credit Card")
    assert tr.alias_ids(old, a) == [a]
    assert tr.canonical_of(old, tid) is None
    out = get_topic_context(old, "credit cards")
    assert out["topic"]["family_total"] == 1
    assert "canonical" not in out["topic"]


# --- clustering --------------------------------------------------------------------------------


def test_fold_variants_form_one_cluster_named_by_the_most_used_without_the_llm(conn):
    card = _tag(conn, "credit card", 5)
    cards = _tag(conn, "credit cards", 2)

    plan = tr.plan_batch(conn, _vectors({}), limit=100)
    calls = []
    plan = tr.name_clusters(conn, plan, ask=lambda *a: calls.append(a), max_calls=5)
    tr.apply_plan(conn, plan)

    assert calls == []  # one fold group's names differ only in plurals: no model call
    assert tr.canonical_of(conn, card) == tr.canonical_of(conn, cards) is not None
    row = conn.execute(
        "SELECT name, named_by FROM canonical_topics WHERE id = ?", (tr.canonical_of(conn, card),)
    ).fetchone()
    assert tuple(row) == ("credit card", "rule")


def test_embedding_neighbours_join_and_other_numbers_stay_apart(conn):
    watch = _tag(conn, "competitor watch", 6)
    monitoring = _tag(conn, "competitor monitoring", 2)
    q1 = _tag(conn, "results q1 2026", 3)
    q2 = _tag(conn, "results q2 2026", 2)
    vectors = _vectors(
        {
            "competitor watch": [1, 0],
            "competitor monitoring": [0.98, 0.2],
            "results q1 2026": [0, 1],
            "results q2 2026": [0.05, 1],
        }
    )

    plan = tr.plan_batch(conn, vectors, limit=100, floor=0.9)
    tr.apply_plan(conn, tr.name_clusters(conn, plan, ask=None, max_calls=0))

    assert tr.canonical_of(conn, watch) == tr.canonical_of(conn, monitoring)
    assert tr.canonical_of(conn, q1) != tr.canonical_of(conn, q2)
    method = conn.execute(
        "SELECT method FROM topic_aliases WHERE topic_id = ?", (monitoring,)
    ).fetchone()[0]
    assert method == "embedding"


def test_a_cluster_of_several_names_is_named_by_the_model(conn):
    watch = _tag(conn, "competitor watch", 6)
    _tag(conn, "competitor monitoring", 2)
    vectors = _vectors({"competitor watch": [1, 0], "competitor monitoring": [0.99, 0.1]})
    prompts = []

    def ask(system, user):
        prompts.append(user)
        keys = json.loads(user[user.index("{") :])
        return json.dumps(dict.fromkeys(keys, "Competitor monitoring"))

    plan = tr.plan_batch(conn, vectors, limit=100, floor=0.9)
    tr.apply_plan(conn, tr.name_clusters(conn, plan, ask=ask, max_calls=3))

    assert len(prompts) == 1
    row = conn.execute(
        "SELECT name, display_name, named_by FROM canonical_topics WHERE id = ?",
        (tr.canonical_of(conn, watch),),
    ).fetchone()
    assert tuple(row) == ("competitor monitoring", "Competitor monitoring", "llm")


def test_a_bad_model_reply_or_no_budget_falls_back_to_the_most_used_name(conn):
    watch = _tag(conn, "competitor watch", 6)
    _tag(conn, "competitor monitoring", 2)
    vectors = _vectors({"competitor watch": [1, 0], "competitor monitoring": [0.99, 0.1]})

    plan = tr.plan_batch(conn, vectors, limit=100, floor=0.9)
    tr.apply_plan(conn, tr.name_clusters(conn, plan, ask=lambda s, u: "not json", max_calls=3))

    row = conn.execute(
        "SELECT name, named_by FROM canonical_topics WHERE id = ?",
        (tr.canonical_of(conn, watch),),
    ).fetchone()
    assert tuple(row) == ("competitor watch", "rule")


def test_batches_take_the_most_used_first_and_a_later_batch_joins_existing_canonicals(conn):
    watch = _tag(conn, "competitor watch", 6)
    monitoring = _tag(conn, "competitor monitoring", 1)
    vectors = _vectors({"competitor watch": [1, 0], "competitor monitoring": [0.99, 0.1]})

    tr.apply_plan(conn, tr.plan_batch(conn, vectors, limit=1, floor=0.9))
    assert tr.canonical_of(conn, watch) is not None
    assert tr.canonical_of(conn, monitoring) is None

    tr.apply_plan(conn, tr.plan_batch(conn, vectors, limit=1, floor=0.9))
    assert tr.canonical_of(conn, monitoring) == tr.canonical_of(conn, watch)
    assert conn.execute("SELECT COUNT(*) FROM canonical_topics").fetchone()[0] == 1


def test_apply_reports_what_the_plan_placed(conn):
    _tag(conn, "credit card", 3)
    _tag(conn, "credit cards", 1)
    _tag(conn, "okapi", 1)
    result = tr.apply_plan(conn, tr.plan_batch(conn, _vectors({}), limit=10))
    assert (result["joined"], result["clusters"], result["placed"]) == (0, 2, 3)
    assert conn.execute("SELECT COUNT(*) FROM topic_aliases").fetchone()[0] == 3


def test_a_dry_run_plan_writes_nothing(conn):
    _tag(conn, "credit card", 2)
    tr.plan_batch(conn, _vectors({}), limit=10)
    assert conn.execute("SELECT COUNT(*) FROM topic_aliases").fetchone()[0] == 0


# --- map at load -------------------------------------------------------------------------------


def test_a_new_topic_whose_folded_words_match_an_alias_is_mapped_at_load(conn):
    card = _tag(conn, "credit card", 3)
    tr.apply_plan(conn, tr.plan_batch(conn, _vectors({}), limit=10))

    new = find_or_create_topic(conn, "Cards Credit")

    assert new != card
    assert tr.canonical_of(conn, new) == tr.canonical_of(conn, card)
    assert conn.execute("SELECT method FROM topic_aliases WHERE topic_id = ?", (new,)).fetchone()[
        0
    ] == ("load")


def test_a_new_topic_with_no_match_waits_for_the_batch(conn):
    _tag(conn, "credit card", 3)
    tr.apply_plan(conn, tr.plan_batch(conn, _vectors({}), limit=10))
    new = find_or_create_topic(conn, "okapi migration")
    assert tr.canonical_of(conn, new) is None


# --- reads -------------------------------------------------------------------------------------


def test_topic_context_reads_the_alias_set(conn):
    watch = _tag(conn, "competitor watch", 3)
    _tag(conn, "competitor monitoring", 2)
    vectors = _vectors({"competitor watch": [1, 0], "competitor monitoring": [0.99, 0.1]})
    tr.apply_plan(conn, tr.plan_batch(conn, vectors, limit=10, floor=0.9))

    out = get_topic_context(conn, "competitor monitoring")

    assert out["email_count"] == 5
    assert out["topic"]["canonical"] == "Competitor Watch"
    assert out["topic"]["family_total"] == 2
    assert len(tr.alias_ids(conn, watch)) == 2


def test_a_query_naming_the_canonical_reads_its_aliases(conn):
    _tag(conn, "competitor watch", 3)
    _tag(conn, "competitor monitoring", 2)
    vectors = _vectors({"competitor watch": [1, 0], "competitor monitoring": [0.99, 0.1]})
    plan = tr.plan_batch(conn, vectors, limit=10, floor=0.9)

    def ask(system, user):
        keys = json.loads(user[user.index("{") :])
        return json.dumps(dict.fromkeys(keys, "Rival tracking"))

    tr.apply_plan(conn, tr.name_clusters(conn, plan, ask=ask, max_calls=1))

    out = get_topic_context(conn, "rival tracking")

    assert out["email_count"] == 5


def test_chat_and_calendar_links(conn):
    thread = f.teams_thread(conn)
    event = f.calendar_event(conn)
    tr.link_topics(conn, "teams", thread, ["Credit Cards", "credit cards", ""])
    tr.link_topics(conn, "calendar", event, ["Okapi budget"])
    chat = conn.execute(
        "INSERT INTO whatsapp_chats (chat_jid, chat_kind, first_seen_at)"
        " VALUES ('1@g.us', 'group', '2026-01-01')"
    ).lastrowid
    session = conn.execute(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at)"
        " VALUES (?, 'm1', '2026-01-01', '2026-01-01')",
        (chat,),
    ).lastrowid
    tr.link_topics(conn, "whatsapp", session, ["Okapi budget"])

    assert conn.execute("SELECT COUNT(*) FROM teams_thread_topics").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_topics").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM whatsapp_thread_topics").fetchone()[0] == 1


def test_link_topics_is_a_no_op_without_the_step(old):
    thread = f.teams_thread(old)
    assert tr.link_topics(old, "teams", thread, ["Credit Cards"]) == 0
    assert old.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0


# --- embedding ---------------------------------------------------------------------------------


def test_names_are_embedded_once_and_a_new_space_starts_the_cache_afresh(tmp_path, monkeypatch):
    sent = []

    def embed(names):
        sent.append(list(names))
        return np.eye(len(names), 4, dtype=np.float32)

    path = tmp_path / "v.npz"
    vectors_of = tr.cached_topic_vectors(path, embed=embed)
    assert vectors_of(["a", "b"]).shape == (2, 4)
    assert vectors_of(["b", "a"]).shape == (2, 4)
    assert sent == [["a", "b"]]

    monkeypatch.setattr(tr, "TOPIC_TEMPLATE", "topic-2")
    tr.cached_topic_vectors(path, embed=embed)(["a"])
    assert sent[-1] == ["a"]


def test_a_name_is_embedded_as_a_prompted_clustering_query(monkeypatch):
    from src.store import embeddings

    seen = {}

    def fake(texts, client=None, *, space=None):
        seen["texts"], seen["space"] = texts, space
        return np.ones((len(texts), tr.TOPIC_DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "generate_embeddings", fake)
    out = tr.embed_topic_names(["credit cards"])
    assert seen["space"]["dim"] == tr.TOPIC_DIM
    assert seen["texts"] == ["task: clustering | query: credit cards"]
    assert np.allclose(np.linalg.norm(out, axis=1), 1)


# --- the command -------------------------------------------------------------------------------


def _args(db, **kw):
    import argparse

    base = {"db": db, "apply": False, "limit": 10, "llm_calls": 0, "floor": 0.98, "show": 5}
    return argparse.Namespace(**{**base, **kw})


def test_the_command_refuses_a_store_without_the_step(tmp_path, capsys):
    from src.store import topic_registry_maint as maint

    create_database(str(tmp_path / "old.db"), gated=False).close()
    assert maint.cmd_topic_registry(_args(tmp_path / "old.db")) == 2
    assert "--apply-gated" in capsys.readouterr().err


def test_the_command_counts_then_writes(tmp_path, monkeypatch, capsys):
    from src.store import topic_registry_maint as maint

    db = create_database(str(tmp_path / "brain.db"))
    _tag(db, "credit card", 2)
    _tag(db, "credit cards", 1)
    db.close()
    monkeypatch.setattr(maint, "cached_topic_vectors", lambda path: _vectors({}))

    assert maint.cmd_topic_registry(_args(tmp_path / "brain.db")) == 0
    assert "dry run" in capsys.readouterr().out
    assert maint.cmd_topic_registry(_args(tmp_path / "brain.db", apply=True)) == 0
    out = capsys.readouterr().out
    assert "'placed': 2" in out and "'unplaced': 0" in out


# --- part 2: topics asked of chats and meetings ------------------------------------------------


def test_topics_are_asked_only_with_the_env_and_the_step(conn, old, monkeypatch):
    monkeypatch.setenv(tr.CHAT_TOPICS_ENV, "")
    assert not tr.asks_topics(conn)
    monkeypatch.setenv(tr.CHAT_TOPICS_ENV, "1")
    assert tr.asks_topics(conn)
    assert not tr.asks_topics(old)


def test_a_thread_prompt_gains_the_topics_key_before_key_facts():
    from src.extract.teams_prompt import build_prompt

    system, user = build_prompt({"chat_label": "Okapi"}, [])
    s2, u2 = tr.with_topics(system, user)
    assert s2.startswith(system) and tr.TOPICS_RULE in s2
    assert u2.index('"topics"') < u2.index('"key_facts"')
    with pytest.raises(ValueError):
        tr.with_topics(system, "no keys here")


def test_a_calendar_prompt_gains_the_ask_before_its_closing_line():
    prompt = "Extract JSON.\nRespond with ONLY the JSON object, no other text."
    out = tr.with_calendar_topics(prompt)
    assert out.index('"topics"') < out.index("Respond with ONLY")
    with pytest.raises(ValueError):
        tr.with_calendar_topics("nothing")


def test_replace_links_swaps_a_parents_topics(conn):
    thread = f.teams_thread(conn)
    tr.replace_links(conn, "teams", thread, {"topics": ["Okapi budget", 3, " ", "Cards"]})
    tr.replace_links(conn, "teams", thread, {"topics": ["Cards"]})
    names = [
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM teams_thread_topics l JOIN topics t ON t.id = l.topic_id"
            " WHERE l.thread_id = ?",
            (thread,),
        )
    ]
    assert names == ["cards"]
    assert tr.topics_of({"topics": "not a list"}) == []
    assert tr.replace_links(conn, "teams", thread, None) == 0


def test_topic_context_lists_the_chats_and_meetings_of_the_alias_set(conn, old):
    _tag(conn, "okapi budget", 2)
    thread = f.teams_thread(conn, title="Okapi chat")
    event = f.calendar_event(conn, subject="Okapi review", body_summary="Budget review.")
    tr.link_topics(conn, "teams", thread, ["Okapi Budget"])
    tr.link_topics(conn, "calendar", event, ["okapi budgets"])
    tr.apply_plan(conn, tr.plan_batch(conn, _vectors({}), limit=10))

    out = get_topic_context(conn, "okapi budget")

    assert [r["title"] for r in out["teams_threads"]] == ["Okapi chat"]
    assert out["teams_threads_total"] == 1
    assert [r["subject"] for r in out["meetings"]] == ["Okapi review"]
    assert out["whatsapp_sessions_total"] == 0
    _tag(old, "okapi budget", 1)
    assert "teams_threads" not in get_topic_context(old, "okapi budget")
