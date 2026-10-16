"""The nightly report names the index's space and counts its vectors by kind, chunks
included, and says when a vector rebuild has stalled.

After the switch to gemini-embedding-2 the index holds a new kind, the chunks of long
documents, and the question "is the producer on the new index yet" had no answer short of
opening the file.
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.store.chunks import plan_chunks
from src.store.embeddings import (
    TEAMS_THREAD_ID_OFFSET,
    chunk_vector_id,
    embedding_space,
    legacy_space,
)
from src.store.schema import create_database
from tests import factories

HEALTH_CHECK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "health_check.py"


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check_vectors", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _save(path, ids, space):
    np.savez(
        str(path),
        ids=np.array(ids, dtype=np.int64),
        vectors=np.ones((len(ids), int(space["dim"])), np.float16),
        space=np.array(json.dumps(space)),
    )


def _deck(conn):
    attachment_id = factories.attachment(
        conn,
        filename="deck.pptx",
        text="\n\n".join("Slide text " * 60 for _ in range(40)),
        summary="A deck.",
    )
    plan = plan_chunks(conn)
    cid = plan.content[attachment_id]
    return (
        attachment_id,
        [chunk_vector_id(cid, no) for no, _s, _e in plan.spans[attachment_id]],
        cid,
    )


def test_the_report_counts_vectors_by_kind_and_names_the_space(hc, tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    _aid, chunk_ids, cid = _deck(conn)
    path = tmp_path / "embeddings.npz"
    _save(path, [1, 2, -cid, TEAMS_THREAD_ID_OFFSET - 4, *chunk_ids], embedding_space())

    result = hc.check_embeddings(conn, npz_path=path)

    assert result["space"] == embedding_space()
    assert result["by_kind"] == {
        "email": 2,
        "attachment": 1,
        "chunk": len(chunk_ids),
        "conversation": 0,
        "teams": 1,
        "whatsapp": 0,
    }
    assert result["coverage"]["chunks"] == (len(chunk_ids), len(chunk_ids))


def test_missing_chunk_vectors_are_a_gap(hc, tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    for _ in range(7):
        _deck(conn)
    rows = conn.execute("SELECT COUNT(*) FROM attachment_chunks").fetchone()[0]
    assert rows >= 50
    path = tmp_path / "embeddings.npz"
    _save(path, [1], embedding_space())

    result = hc.check_embeddings(conn, npz_path=path)

    assert result["gaps"]["chunks"] == (0, rows)
    assert result["status"] == "WARN"


def test_a_legacy_index_owes_no_chunks(hc, tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    _deck(conn)
    path = tmp_path / "embeddings.npz"
    _save(path, [1], legacy_space())

    result = hc.check_embeddings(conn, npz_path=path)

    assert "chunks" not in result["coverage"]
    assert result["space"] == legacy_space()


def test_a_stalled_rebuild_is_a_warning(hc, tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    path = tmp_path / "embeddings.npz"
    _save(path, [1], legacy_space())
    side = tmp_path / "embeddings.rebuild"
    side.mkdir()
    started = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    (side / "state.json").write_text(
        json.dumps({"started_at": started, "parts": ["p"], "jobs": [{}], "space": {}})
    )

    result = hc.check_embeddings(conn, npz_path=path)

    assert result["rebuild"]["jobs_pending"] == 1
    assert result["status"] == "WARN"
    assert "vectors-rebuild --status" in result["note"]
