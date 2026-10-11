"""A search embeds its query with a short timeout and at most one retry.

The query went through generate_embeddings, the bulk-ingest loop: five tries on
a 429 with 5 + 10 + 20 + 40 + 60 s sleeps, on a client with no timeout. A
throttled model stalled a search for up to 135 s, a hung connection forever,
and the keyword-seeded fallback, which exists for exactly that case, waited
behind it.
"""

import time

import numpy as np
import pytest

from src.store.schema import create_database


class _RateLimited(Exception):
    """What google-genai raises for a 429: an error carrying the HTTP status as `code`."""

    code = 429

    def __init__(self):
        super().__init__("429 RESOURCE_EXHAUSTED. Quota exceeded for this project")


class _Refused(Exception):
    """A 403: a policy refusal or a bad key, which no retry changes."""

    code = 403

    def __init__(self):
        super().__init__("403 PERMISSION_DENIED")


class _Client:
    """A google-genai client whose embed_content raises `error`, or returns `vector`."""

    def __init__(self, error=None, vector=(1.0, 0.0, 0.0)):
        self.calls: list[dict] = []
        self.error = error
        self.vector = list(vector)
        self.models = self

    def embed_content(self, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.error is not None:
            raise self.error()

        class _Embedding:
            values = self.vector

        class _Result:
            embeddings = [_Embedding() for _ in contents]

        return _Result()


def _store(tmp_path, monkeypatch):
    """Three emails and their vectors; only the first holds the word 'lighthouse'."""
    import src.store.embeddings as embeddings

    conn = create_database(":memory:")
    for email_id, summary in (
        (1, "harbour lighthouse repairs"),
        (2, "coastal beacon maintenance"),
        (3, "quarterly tax filing"),
    ):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary) "
            "VALUES (?, ?, '2026-01-01T10:00:00Z', ?, ?)",
            (email_id, email_id, summary, summary),
        )
    conn.commit()
    index = tmp_path / "emb.npz"
    np.savez(
        str(index),
        ids=np.array([1, 2, 3], dtype=np.int64),
        vectors=np.array([[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32),
    )
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", index)
    return conn


def test_a_rate_limited_query_gives_up_after_one_retry():
    from src.store.embeddings import embed_query

    client = _Client(error=_RateLimited)
    started = time.monotonic()

    with pytest.raises(_RateLimited):
        embed_query(["lighthouse"], client=client)

    assert len(client.calls) == 2
    assert time.monotonic() - started < 2.0


def test_a_refusal_is_not_retried():
    from src.store.embeddings import embed_query

    client = _Client(error=_Refused)

    with pytest.raises(_Refused):
        embed_query(["lighthouse"], client=client)

    assert len(client.calls) == 1


def test_the_query_call_carries_a_four_second_timeout():
    from src.store.embeddings import embed_query

    client = _Client()

    vectors = embed_query(["lighthouse"], client=client)

    assert vectors.shape == (1, 3)
    assert client.calls[0]["config"].http_options.timeout == 4000


@pytest.mark.parametrize("tool", ["search_emails", "search_conversations"])
def test_a_throttled_model_falls_back_to_the_keyword_seed_within_the_budget(
    tmp_path, monkeypatch, tool
):
    import src.store.embeddings as embeddings
    from src import mcp_server

    conn = _store(tmp_path, monkeypatch)
    client = _Client(error=_RateLimited)
    monkeypatch.setattr(embeddings, "_get_client", lambda: client)
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)
    started = time.monotonic()

    if tool == "search_emails":
        rows = mcp_server.search_emails("lighthouse", search_type="semantic", limit=2)
        assert [r["email_id"] for r in rows] == [1, 2]
        assert {r["semantic"] for r in rows} == {"keyword_seeded: _RateLimited"}
    else:
        # No conversation vector exists, so the fallback ranks nothing: an
        # empty answer, returned at once rather than after two minutes.
        assert mcp_server.search_conversations("lighthouse", search_type="semantic") == []

    assert len(client.calls) <= 2
    assert time.monotonic() - started < 2.0


def test_recall_ranks_around_its_keyword_matches_when_the_model_is_throttled(tmp_path, monkeypatch):
    import src.store.embeddings as embeddings
    from src import mcp_server

    conn = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(embeddings, "_get_client", lambda: _Client(error=_RateLimited))
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)
    started = time.monotonic()

    out = mcp_server.recall("lighthouse")

    assert out["summary"]["semantic"] == "keyword_seeded: _RateLimited"
    # Email 2 does not hold the word: only the seeded vectors can have found it.
    assert 2 in [r["email_id"] for r in out["emails"]]
    assert time.monotonic() - started < 2.0


def test_recall_names_the_embedding_error_when_no_keyword_match_can_stand_in(tmp_path, monkeypatch):
    import src.store.embeddings as embeddings
    from src import mcp_server

    conn = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(embeddings, "_get_client", lambda: _Client(error=_RateLimited))
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = mcp_server.recall("zeppelin")

    assert out["summary"]["semantic"] == "unavailable: _RateLimited"


def test_ingest_keeps_its_long_backoff(monkeypatch):
    """Waiting is cheap for a sync job, and a batch that gives up early fails the run."""
    import src.store.embeddings as embeddings

    slept: list[float] = []
    monkeypatch.setattr(embeddings.time, "sleep", slept.append)
    client = _Client(error=_RateLimited)

    with pytest.raises(RuntimeError, match="still rate-limited"):
        embeddings.generate_embeddings(["a"], client=client)

    assert len(client.calls) == 5
    assert slept == [5, 10, 20, 40, 60]
