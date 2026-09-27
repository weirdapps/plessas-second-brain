"""search_conversations filters by workspace the same way in every branch, and
before it ranks.

The summary branch matched the workspace path, the turn branch the project
name, and the semantic branch the project name after taking the global top
2 x limit. A path filter dropped every turn-level hit and emptied semantic
search, and a semantic search whose best matches sat in other projects came
back empty although matches existed in this one.
"""

import numpy as np
import pytest

from src.store.embeddings import CONVERSATION_ID_OFFSET
from src.store.schema import create_database, get_connection

PATH = "/work/src/brain-repo"


def _conversation(conn, cid, workspace, project, summary="a session"):
    conn.execute(
        "INSERT INTO conversations (id, session_id, started_at, ended_at, workspace, "
        "project_name, turn_count, summary, created_at) "
        "VALUES (?, ?, '2026-09-01', '2026-09-01', ?, ?, 1, ?, '2026-09-01')",
        (cid, f"s{cid}", workspace, project, summary),
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src import mcp_server

    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    for cid in (1, 2, 3):
        _conversation(conn, cid, "/work/src/other", "other")
    for cid in (4, 5):
        _conversation(conn, cid, PATH, "brain-repo")
    conn.execute(
        "INSERT INTO conversation_turns (conversation_id, turn_index, timestamp, speaker, content) "
        "VALUES (4, 0, '2026-09-01', 'user', 'the okapi lock on the index')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))

    # The other project's sessions are the closest vectors to the query.
    index = tmp_path / "emb.npz"
    ids = [CONVERSATION_ID_OFFSET - cid for cid in (1, 2, 3, 4, 5)]
    vectors = [[0.0, 1.0, 0.0]] * 3 + [[0.6, 0.8, 0.0], [0.8, 0.6, 0.0]]
    np.savez(
        str(index),
        ids=np.array(ids, dtype=np.int64),
        vectors=np.array(vectors, dtype=np.float32),
    )
    import src.store.embeddings as embeddings

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", index)
    monkeypatch.setattr(
        embeddings, "generate_embeddings", lambda texts: np.array([[0.0, 1.0, 0.0]], np.float32)
    )
    return path


@pytest.mark.parametrize("workspace", ["brain-repo", PATH])
def test_semantic_search_ranks_inside_the_workspace(db, workspace):
    from src.mcp_server import search_conversations

    rows = search_conversations("okapi", search_type="semantic", workspace=workspace, limit=1)

    assert [r["session_id"] for r in rows] == ["s4"]


def test_semantic_search_in_a_workspace_with_no_sessions_is_empty(db):
    from src.mcp_server import search_conversations

    assert search_conversations("okapi", search_type="semantic", workspace="nowhere") == []


@pytest.mark.parametrize("workspace", ["brain-repo", PATH])
def test_keyword_search_finds_turns_by_path_or_project(db, workspace):
    from src.mcp_server import search_conversations

    rows = search_conversations("okapi lock", workspace=workspace)

    assert [(r["session_id"], r["match_type"]) for r in rows] == [("s4", "turn_content")]
    assert not rows[0].get("partial_match")


def test_top_indices_keeps_only_the_allowed_ids():
    from src.store.embeddings import _top_indices

    ids = np.array([CONVERSATION_ID_OFFSET - c for c in (1, 2, 3)], dtype=np.int64)
    similarities = np.array([0.9, 0.8, 0.1])

    top = _top_indices(
        similarities, ids, 1, kinds={"conversation"}, allowed_ids={CONVERSATION_ID_OFFSET - 3}
    )

    assert list(top) == [2]
