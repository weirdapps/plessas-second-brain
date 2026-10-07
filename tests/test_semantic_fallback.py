"""Semantic search when the query cannot be embedded.

On 2026-10-06 an organisation policy on the Vertex project started refusing
gemini-embedding-001 with 400 FAILED_PRECONDITION. Every vector in the index
was still there; only the query embedding failed, and search_emails and
search_conversations died with an error that named nothing. The query now
stands at the centroid of its best keyword matches' vectors, so the ranking
still runs by meaning around them, and every row says it was ranked that way.
"""

import numpy as np
import pytest

from src.store.schema import create_database

POLICY_ERROR = (
    "400 FAILED_PRECONDITION. Organization Policy constraint "
    "constraints/vertexai.allowedModels violated"
)


def _write_npz(path, ids, vecs):
    np.savez(str(path), ids=np.array(ids, dtype=np.int64), vectors=np.array(vecs, dtype=np.float32))


def _refusing_embedder(_texts):
    raise RuntimeError(POLICY_ERROR)


def _store(tmp_path):
    """Three emails. Only the first holds the word 'lighthouse'; the second
    sits next to it in vector space without the word; the third is far away."""
    conn = create_database(":memory:")
    for email_id, summary in (
        (1, "harbour lighthouse repairs"),
        (2, "coastal beacon maintenance"),
        (3, "quarterly tax filing"),
    ):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary) "
            "VALUES (?, ?, '2026-01-01', ?, ?)",
            (email_id, email_id, summary, summary),
        )
    conn.commit()
    p = tmp_path / "emb.npz"
    _write_npz(p, [1, 2, 3], [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 0.0, 1.0]])
    return conn, p


def test_a_refused_query_embedding_ranks_around_the_keyword_matches(tmp_path):
    from src.store.embeddings import query_semantic

    conn, p = _store(tmp_path)

    out = query_semantic(
        conn, "lighthouse", limit=2, embed_fn=_refusing_embedder, index_path=str(p)
    )

    # Email 2 does not hold the word, so only the vectors can have found it.
    assert [r["email_id"] for r in out] == [1, 2]
    assert {r["semantic"] for r in out} == {"keyword_seeded: RuntimeError"}


def test_a_healthy_query_embedding_leaves_the_rows_unmarked(tmp_path):
    from src.store.embeddings import query_semantic

    conn, p = _store(tmp_path)
    embed = lambda _texts: np.array([[0.0, 0.0, 1.0]], dtype=np.float32)  # noqa: E731

    out = query_semantic(conn, "lighthouse", limit=1, embed_fn=embed, index_path=str(p))

    assert [r["email_id"] for r in out] == [3]
    assert "semantic" not in out[0]


@pytest.mark.parametrize("tool", ["search_emails", "search_conversations"])
def test_no_keyword_match_returns_the_cause_instead_of_a_bare_tool_error(
    tmp_path, monkeypatch, tool
):
    import src.store.embeddings as embeddings
    from src import mcp_server

    conn, p = _store(tmp_path)
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", p)
    monkeypatch.setattr(embeddings, "generate_embeddings", _refusing_embedder)
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: conn)

    out = getattr(mcp_server, tool)("zeppelin", search_type="semantic")

    assert isinstance(out, dict)
    assert "allowedModels" in out["error"]
