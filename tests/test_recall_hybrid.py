"""recall fuses keyword and semantic ranking in every bucket that has vectors, collapses
thread siblings before it cuts the semantic pool, and runs its buckets in parallel
with results equal to the serial run (src/store/recall.py, src/store/embeddings.py)."""

import threading

import numpy as np
import pytest

from src.store.embeddings import (
    CONVERSATION_ID_OFFSET,
    TEAMS_THREAD_ID_OFFSET,
    WHATSAPP_THREAD_ID_OFFSET,
    SemanticCandidates,
    semantic_email_candidates,
)
from src.store.recall import recall
from src.store.schema import create_database, get_connection
from tests import factories, seeded_store
from tests.patching import unset_env


@pytest.fixture(autouse=True)
def _fused(monkeypatch):
    unset_env(monkeypatch, "BRAIN_QUERY_RANKING", "BRAIN_RECALL_WORKERS")


@pytest.fixture
def store(tmp_path):
    """The seeded store, plus one item of each vector kind that no keyword finds."""
    path = seeded_store.build(tmp_path / "brain.db")
    conn = get_connection(str(path))
    zebra_mail = factories.email(conn, subject="Zebra memo", summary="zebra", content="zebra")
    zebra_att = factories.attachment(
        conn, email_id=zebra_mail, filename="zebra.pdf", text="zebra", summary="Zebra figures"
    )
    conn.executescript(
        """
        INSERT INTO teams_threads (id, chat_id, thread_kind, started_at, ended_at, message_count,
                                   title, summary, extraction_status)
            VALUES (2, 1, 'chat_session', '2026-09-01T09:00:00Z', '2026-09-01T10:00:00Z', 1,
                    'Zebra thread', 'The zebra thread', 'extracted');
        INSERT INTO whatsapp_threads (id, chat_id, anchor_message_id, started_at, ended_at,
                                      message_count, participant_names, title, summary)
            VALUES (2, 1, 'w9', '2026-09-01T09:00:00Z', '2026-09-01T10:00:00Z', 1, '[]',
                    'Zebra chat', 'Planning the zebra visit');
        INSERT INTO conversations (id, session_id, started_at, ended_at, project_name,
                                   turn_count, summary, created_at)
            VALUES (2, 's2', '2026-09-01T09:00:00Z', '2026-09-01T10:00:00Z', 'zebra', 1,
                    'A zebra session', '2026-09-01T10:00:00Z'),
                   (3, 's3', '2026-09-02T09:00:00Z', '2026-09-02T10:00:00Z', 'zebra', 1,
                    'An agent zebra session', '2026-09-02T10:00:00Z');
        INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker, content)
            VALUES (2, 0, '2026-09-01T09:00:00Z', 'user', 'a zebra question'),
                   (3, 0, '2026-09-02T09:00:00Z', 'agent', 'an agent zebra task');
        """
    )
    conn.commit()
    return conn, {"email": zebra_mail, "attachment": zebra_att}


def _provider(made, emails=(), **by_kind):
    """A semantic provider ranking the given ids, by kind, as semantic_email_candidates does."""

    def provider(conn, query, limit):
        return SemanticCandidates(list(emails)[:limit], semantic=made, by_kind=by_kind)

    return provider


def test_every_vector_bucket_takes_its_semantic_hits(store):
    conn, made = store
    provider = _provider(
        "ok",
        emails=[made["email"]],
        attachment=[made["attachment"]],
        teams=[2],
        whatsapp=[2],
        conversation=[2],
    )

    out = recall(conn, "okapi", semantic_candidates=provider)

    def refs(bucket, key):
        return [r[key] for r in out[bucket]]

    assert made["email"] in refs("emails", "email_id")
    assert made["attachment"] in refs("attachments", "attachment_id")
    assert 2 in refs("teams", "thread_id")
    assert 2 in refs("whatsapp", "thread_id")
    assert 2 in refs("conversations", "conversation_id")
    for bucket in ("attachments", "teams", "whatsapp", "conversations"):
        semantic = [r for r in out[bucket] if r.get("matched_in") == "semantic"]
        assert len(semantic) == 1, bucket
        assert semantic[0]["ref"] is not None and semantic[0]["kind"]  # enveloped
    assert out["summary"]["semantic"] == "ok"


def test_the_keyword_hits_stay_in_every_bucket(store):
    conn, made = store
    provider = _provider("ok", attachment=[made["attachment"]], teams=[2])

    out = recall(conn, "okapi", semantic_candidates=provider)

    assert 1 in [r["attachment_id"] for r in out["attachments"]]
    assert 1 in [r["thread_id"] for r in out["teams"]]


def test_a_plain_list_provider_fuses_the_email_bucket_only(store):
    conn, made = store

    out = recall(conn, "okapi", semantic_candidates=lambda c, q, n: [made["email"]])

    assert made["email"] in [r["email_id"] for r in out["emails"]]
    assert all(r.get("matched_in") != "semantic" for r in out["attachments"])


def test_relevance_ranking_fuses_the_email_bucket_only(store, monkeypatch):
    monkeypatch.setenv("BRAIN_QUERY_RANKING", "relevance")
    conn, made = store
    provider = _provider("ok", emails=[made["email"]], attachment=[made["attachment"]])

    out = recall(conn, "okapi", semantic_candidates=provider)

    assert made["email"] in [r["email_id"] for r in out["emails"]]
    assert made["attachment"] not in [r["attachment_id"] for r in out["attachments"]]


def test_an_unavailable_semantic_half_leaves_every_bucket_keyword_only(store):
    conn, made = store

    def boom(conn, query, limit):
        raise RuntimeError("no credentials")

    out = recall(conn, "okapi", semantic_candidates=boom)

    assert out["summary"]["semantic"] == "unavailable: RuntimeError"
    assert [r["attachment_id"] for r in out["attachments"]] == [1]
    assert all(r.get("matched_in") != "semantic" for b in ("teams", "whatsapp") for r in out[b])


def test_a_keyword_seeded_vector_is_reported_and_still_fused(store):
    conn, made = store
    provider = _provider("keyword_seeded: ClientError", attachment=[made["attachment"]])

    out = recall(conn, "okapi", semantic_candidates=provider)

    assert out["summary"]["semantic"] == "keyword_seeded: ClientError"
    assert made["attachment"] in [r["attachment_id"] for r in out["attachments"]]


def test_kinds_not_asked_for_take_no_semantic_rows(store):
    conn, made = store
    provider = _provider("ok", attachment=[made["attachment"]], teams=[2])

    out = recall(conn, "okapi", semantic_candidates=provider, kinds=["teams"])

    assert out["attachments"] == []
    assert 2 in [r["thread_id"] for r in out["teams"]]


def test_a_hidden_or_vanished_semantic_hit_is_dropped(store):
    conn, made = store
    provider = _provider("ok", attachment=[999], teams=[999], whatsapp=[999], conversation=[999])

    out = recall(conn, "okapi", semantic_candidates=provider)

    assert all(r.get("matched_in") != "semantic" for b in ("attachments", "teams") for r in out[b])


# --- thread siblings ----------------------------------------------------------------------


def test_thread_siblings_no_longer_eat_the_semantic_pool(tmp_path):
    """Thirty replies of one thread rank first by meaning, then five other threads.
    Cut at the pool before collapsing, the bucket held one thread; it holds five."""
    conn = create_database(str(tmp_path / "brain.db"))
    sibling_ids = [
        factories.email(conn, subject="RE: Gnu plan", conversation_id="gnu", summary="gnu")
        for _ in range(30)
    ]
    others = [
        factories.email(conn, subject=f"Plan {n}", conversation_id=f"t{n}", summary="plan")
        for n in range(5)
    ]
    provider = _provider("ok", emails=sibling_ids + others)

    out = recall(conn, "zzz nothing matches", limit_per_kind=5, semantic_candidates=provider)

    threads = [r["email_id"] for r in out["emails"]]
    assert len(threads) == 5
    assert len(set(threads) & set(sibling_ids)) == 1


# --- the provider: one query vector for every kind ----------------------------------------


def test_semantic_email_candidates_ranks_every_kind_from_one_vector(tmp_path):
    path = seeded_store.build(tmp_path / "brain.db")
    conn = get_connection(str(path))
    content_id = conn.execute("SELECT id FROM attachment_content").fetchone()[0]
    conn.executescript(
        """
        INSERT INTO conversations (id, session_id, started_at, project_name, turn_count, summary,
                                   created_at)
            VALUES (5, 'agent', '2026-09-01', 'x', 1, 'agent session', '2026-09-01');
        INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker, content)
            VALUES (5, 0, '2026-09-01', 'agent', 'subagent task');
        """
    )
    conn.commit()
    ids = [1, -content_id, TEAMS_THREAD_ID_OFFSET - 1, WHATSAPP_THREAD_ID_OFFSET - 1]
    ids += [CONVERSATION_ID_OFFSET - 1, CONVERSATION_ID_OFFSET - 5]
    index = tmp_path / "emb.npz"
    np.savez(
        str(index),
        ids=np.array(ids, dtype=np.int64),
        vectors=np.eye(len(ids), dtype=np.float32) + 0.1,
    )
    calls = []

    def embed(texts):
        calls.append(texts)
        return np.ones((1, len(ids)), dtype=np.float32)

    out = semantic_email_candidates(conn, "q", 10, embed_fn=embed, index_path=str(index))

    assert len(calls) == 1  # one query vector
    assert list(out) == [1]
    assert out.by_kind["attachment"] == [1]  # attachments.id, not the text's id
    assert out.by_kind["teams"] == [1]
    assert out.by_kind["whatsapp"] == [1]
    assert out.by_kind["conversation"] == [1]  # the agent session is left out


# --- parallel ------------------------------------------------------------------------------


def test_parallel_recall_returns_what_the_serial_one_does(store, monkeypatch):
    conn, made = store
    provider = _provider(
        "ok",
        emails=[made["email"], 1],
        attachment=[made["attachment"]],
        teams=[2],
        whatsapp=[2],
        conversation=[2],
    )

    monkeypatch.setenv("BRAIN_RECALL_WORKERS", "1")
    serial = recall(conn, "okapi", semantic_candidates=provider, include_context=True)
    monkeypatch.setenv("BRAIN_RECALL_WORKERS", "6")
    parallel = recall(conn, "okapi", semantic_candidates=provider, include_context=True)

    assert parallel == serial
    assert serial["summary"]["total_hits"] > 10


def test_parallel_recall_reads_each_bucket_on_its_own_connection(store, monkeypatch):
    conn, made = store
    seen = []

    def provider(c, query, limit):
        seen.append((threading.get_ident(), c is conn))
        return SemanticCandidates([], semantic="ok")

    monkeypatch.setenv("BRAIN_RECALL_WORKERS", "4")
    recall(conn, "okapi", semantic_candidates=provider)

    assert seen and seen[0][0] != threading.get_ident() and not seen[0][1]


def test_an_in_memory_store_or_an_open_transaction_runs_serially(monkeypatch):
    monkeypatch.setenv("BRAIN_RECALL_WORKERS", "4")
    conn = create_database(":memory:")
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (1, 1, '2026-09-01', 'Okapi', 'okapi')"
    )  # not committed: another connection would not see it

    out = recall(conn, "okapi", semantic_candidates=lambda c, q, n: [1])

    assert [r["email_id"] for r in out["emails"]] == [1]


def test_a_bucket_that_raises_raises_out_of_recall(store, monkeypatch):
    import src.store.recall as recall_module

    conn, _made = store

    def broken(*_args, **_kwargs):
        raise ValueError("bucket broke")

    monkeypatch.setattr(recall_module, "_search_teams", broken)
    for workers in ("1", "4"):
        monkeypatch.setenv("BRAIN_RECALL_WORKERS", workers)
        with pytest.raises(ValueError, match="bucket broke"):
            recall(conn, "okapi")
