"""The vectors table (issue 171, gated migration `vectors`).

Before the step the index is embeddings.npz, rewritten whole by every writer and loaded
privately by every process. The step moves it into brain.db: one row per vector, int8 with a
per-row scale, and the vector's kind, parent item, dimension and template beside it. From
then on every writer adds, replaces and deletes rows, and every search reads the table; the
npz is left as it was, untouched. A store without the step keeps the npz, as before. Every
value is invented.
"""

import json
import sqlite3

import numpy as np
import pytest

from src.store import embeddings, vector_store
from src.store.embeddings import (
    CHUNK_ID_OFFSET,
    TEAMS_THREAD_ID_OFFSET,
    WHATSAPP_THREAD_ID_OFFSET,
    chunk_vector_id,
    embedding_space,
)
from src.store.schema import apply_gated, create_database, pending_gated
from tests import factories
from tests.brain_cli import brain
from tests.test_vectors_v2 import FakeClient, _save, _vector

SPACE = embedding_space()
OLD = TEAMS_THREAD_ID_OFFSET - 999  # a vector no email row stands for


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "embeddings.npz"
    client = FakeClient()
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", path)
    monkeypatch.setattr(embeddings, "_get_client", lambda: client)
    monkeypatch.setattr(embeddings.time, "sleep", lambda _s: None)
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn, path, client
    try:
        conn.close()
    except sqlite3.ProgrammingError:
        pass


def _apply(conn):
    conn.execute("BEGIN IMMEDIATE")
    assert apply_gated(conn, "test") == ["vectors"]
    conn.commit()


def _rows(conn):
    return conn.execute(
        "SELECT id, kind, parent_id, dim, template FROM vectors ORDER BY id"
    ).fetchall()


# --- the step ------------------------------------------------------------------------------


def test_a_new_store_leaves_the_step_to_the_owner(store):
    """The step moves vectors that live outside brain.db, so no store takes it unasked."""
    conn, _path, _client = store
    assert pending_gated(conn) == ["vectors"]
    assert not vector_store.enabled(conn)
    assert "vectors" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_the_step_imports_the_npz_as_int8_with_kind_and_parent(store):
    conn, path, _client = store
    att = factories.attachment(conn, summary="a long report")
    content_id = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (att,)
    ).fetchone()[0]
    ids = [
        5,
        -content_id,
        chunk_vector_id(content_id, 3),
        TEAMS_THREAD_ID_OFFSET - 7,
        WHATSAPP_THREAD_ID_OFFSET - 2,
        embeddings.CONVERSATION_ID_OFFSET - 4,
    ]
    vectors = np.array([_vector(str(i), 768) for i in ids], dtype=np.float32)
    _save(path, ids, vectors, SPACE, np.float16)
    before = path.read_bytes()

    _apply(conn)

    assert vector_store.enabled(conn)
    kinds = {r["id"]: (r["kind"], r["parent_id"], r["dim"], r["template"]) for r in _rows(conn)}
    assert kinds == {
        5: ("email", 5, 768, 2),
        -content_id: ("attachment", -content_id, 768, 2),
        chunk_vector_id(content_id, 3): ("chunk", -content_id, 768, 2),
        TEAMS_THREAD_ID_OFFSET - 7: ("teams_thread", TEAMS_THREAD_ID_OFFSET - 7, 768, 2),
        WHATSAPP_THREAD_ID_OFFSET - 2: ("whatsapp_thread", WHATSAPP_THREAD_ID_OFFSET - 2, 768, 2),
        embeddings.CONVERSATION_ID_OFFSET - 4: (
            "conversation",
            embeddings.CONVERSATION_ID_OFFSET - 4,
            768,
            2,
        ),
    }
    blob = conn.execute("SELECT vec FROM vectors WHERE id = 5").fetchone()[0]
    assert len(blob) == 768, "one byte a dimension"
    assert path.read_bytes() == before, "the npz is left exactly as it was"
    space = json.loads(
        conn.execute(
            "SELECT value FROM sync_metadata WHERE key = ?", (embeddings.EMBEDDING_SPACE_KEY,)
        ).fetchone()[0]
    )
    assert space == SPACE


def test_int8_keeps_each_vector_within_a_cosine_of_0_9997():
    rng = np.random.default_rng(1)
    vectors = rng.standard_normal((200, 768)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)

    q = vector_store.quantise(vectors)
    back = q.astype(np.float32)
    back /= np.linalg.norm(back, axis=1, keepdims=True)

    assert q.dtype == np.int8
    assert (np.einsum("ij,ij->i", vectors, back) > 0.9997).all()


def test_the_dry_run_counts_the_vectors_and_writes_nothing(store, monkeypatch, capsys):
    conn, path, _client = store
    _save(path, [1, 2, 3], [_vector(str(i), 768) for i in range(3)], SPACE, np.float16)
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()

    code, out, _err = brain(
        monkeypatch, capsys, "--db", db, "maint", "migrate", "--apply-gated", "--dry-run"
    )

    assert code == 0, out
    assert "Dry run, rolled back: vectors" in out
    assert "vectors rows: 0 -> 3  (+3)" in out
    check = sqlite3.connect(db)
    assert pending_gated(check) == ["vectors"]
    assert "vectors" not in {
        r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    check.close()


# --- writers write rows, never the npz ---------------------------------------------------------


def test_after_the_step_the_embedding_pass_adds_rows_and_leaves_the_npz(store):
    conn, path, client = store
    _save(path, [OLD], [_vector("old", 768)], SPACE, np.float16)
    _apply(conn)
    before = path.read_bytes()
    email_id = factories.email(conn, subject="Harbour plan", summary="Repairs to the lighthouse.")

    assert embeddings.build_index(conn) == 1
    assert embeddings.build_index(conn) == 0, "the table says what is indexed"

    assert [r["id"] for r in _rows(conn)] == [OLD, email_id]
    assert path.read_bytes() == before


def test_the_teams_writer_replaces_its_row(store):
    conn, path, _client = store
    _save(path, [OLD], [_vector("old", 768)], SPACE, np.float16)
    _apply(conn)
    tid = factories.teams_thread(conn, summary="the pier budget", extraction_status="extracted")

    assert embeddings.build_teams_index(conn) == 1
    first = conn.execute("SELECT vec FROM vectors WHERE id = ?", (TEAMS_THREAD_ID_OFFSET - tid,))
    first_blob = first.fetchone()[0]
    conn.execute("UPDATE teams_threads SET summary = 'the quay budget', embedding_at = NULL")
    conn.commit()
    assert embeddings.build_teams_index(conn) == 1

    rows = conn.execute(
        "SELECT vec FROM vectors WHERE id = ?", (TEAMS_THREAD_ID_OFFSET - tid,)
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] != first_blob


def test_removing_an_attachment_takes_its_chunks_with_it(store):
    conn, path, _client = store
    ids = [-12, chunk_vector_id(12, 0), chunk_vector_id(12, 1), -13, 4]
    _save(path, ids, [_vector(str(i), 768) for i in ids], SPACE, np.float16)
    _apply(conn)

    assert embeddings.remove_vectors([-12], conn=conn) == 3

    assert [r["id"] for r in _rows(conn)] == sorted([-13, 4])


def test_every_production_caller_of_remove_vectors_passes_the_store():
    """A call without the store would edit the retired npz and leave the table's row."""
    import pathlib
    import re

    root = pathlib.Path(__file__).parent.parent / "src"
    calls = [
        (p, line)
        for p in root.rglob("*.py")
        for line in p.read_text().splitlines()
        if re.search(r"\bremove_vectors\(", line) and "def remove_vectors" not in line
    ]
    assert calls
    assert all("conn=" in line for _p, line in calls), calls


def test_installing_an_index_keeps_the_rows_it_replaced_as_an_npz(store):
    conn, path, _client = store
    _save(path, [1, 2], [_vector("1", 768), _vector("2", 768)], SPACE, np.float16)
    _apply(conn)

    backup = embeddings.install_index(
        conn, [3], np.array([_vector("3", 768)], dtype=np.float32), SPACE
    )

    assert [r["id"] for r in _rows(conn)] == [3]
    assert backup is not None and np.load(backup)["ids"].tolist() == [1, 2]


# --- searches read the table ---------------------------------------------------------------


def _three_emails(conn):
    return [
        factories.email(conn, subject=s, summary=s)
        for s in ("harbour lighthouse repairs", "budget for the quay", "pier insurance claim")
    ]


def test_a_search_ranks_the_same_from_the_table_as_from_the_npz(store):
    conn, path, client = store
    ids = _three_emails(conn)
    embeddings.build_index(conn)

    def embed(texts):
        return np.array([_vector("harbour lighthouse repairs", 768)], dtype=np.float32)

    from_npz = embeddings.query_semantic(conn, "lighthouse", embed_fn=embed)
    _apply(conn)
    path.unlink()  # the table alone answers now
    from_table = embeddings.query_semantic(conn, "lighthouse", embed_fn=embed)

    assert [r["email_id"] for r in from_table] == [r["email_id"] for r in from_npz]
    for a, b in zip(from_table, from_npz, strict=True):
        assert abs(a["similarity"] - b["similarity"]) < 0.01
    assert {r["email_id"] for r in from_table} == set(ids)


def test_a_search_sees_a_vector_added_after_it_loaded_the_table(store, monkeypatch):
    conn, path, _client = store
    monkeypatch.setenv("BRAIN_SEMANTIC_FLOOR", "-1")  # random vectors: the cache is the subject
    _save(path, [OLD], [_vector("old", 768)], SPACE, np.float16)
    _apply(conn)

    def embed(texts):
        return np.array([_vector(texts[0], 768)], dtype=np.float32)

    assert (
        embeddings.query_semantic(conn, "x", embed_fn=embed) == []
    )  # OLD is a Teams thread no row stands for
    email_id = factories.email(conn, subject="Harbour plan", summary="harbour")
    embeddings.build_index(conn)

    found = embeddings.semantic_email_candidates(conn, "x", embed_fn=embed)
    assert list(found) == [email_id]


def test_the_chunk_of_a_document_still_folds_onto_it(store):
    conn, path, _client = store
    email_id = factories.email(conn)
    att = factories.attachment(conn, email_id=email_id, summary="annual report")
    content_id = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (att,)
    ).fetchone()[0]
    target = _vector("the passage", 768)
    _save(
        path,
        [-content_id, chunk_vector_id(content_id, 2)],
        [_vector("summary", 768), target],
        SPACE,
        np.float16,
    )
    _apply(conn)

    rows = embeddings.query_semantic(
        conn, "x", kinds=["attachment"], embed_fn=lambda t: np.array([target], np.float32)
    )

    assert [r["attachment_id"] for r in rows] == [att]
    assert rows[0]["similarity"] > 0.99


def test_a_store_without_the_step_reads_the_npz_as_before(store):
    conn, path, _client = store
    ids = _three_emails(conn)
    embeddings.build_index(conn)

    assert np.load(path)["ids"].tolist() == ids
    assert not vector_store.enabled(conn)


def test_counts_by_kind_for_the_owner(store):
    conn, path, _client = store
    _save(path, [1, -2, chunk_vector_id(2, 0)], [_vector(str(i), 768) for i in range(3)], SPACE)
    _apply(conn)

    assert vector_store.counts(conn) == {
        "vectors rows": 3,
        "vectors email": 1,
        "vectors attachment": 1,
        "vectors chunk": 1,
    }


def test_the_chunk_namespace_boundary_is_a_chunk():
    assert vector_store.kind_of(CHUNK_ID_OFFSET) == "chunk"
    assert vector_store.kind_of(CHUNK_ID_OFFSET + 1) == "whatsapp_thread"
