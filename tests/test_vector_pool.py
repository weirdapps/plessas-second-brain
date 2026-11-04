"""Issue 171, part 3: recall's semantic candidates on a store whose vectors are in its table.

Every bucket ranks its own kind by each item's best vector (its summary or its best
passage, attachment or email chunk), and an item under the similarity floor is none. One
pool fused by reciprocal rank across the summary and passage lists was built and measured
on the owner's 45 labelled questions: recall@10 of the recall bundle fell from 0.807 to
0.700, so it was not kept. With BRAIN_WIDE_SUMMARIES=1 and every summary at 1536, the
summaries are scored at their full width (the query embedded once at 1536, its first 768
scaled for everything else). A store on the npz answers as before. Every value is invented.
"""

import numpy as np
import pytest

from src.store import embeddings, vector_store
from src.store.embeddings import (
    TEAMS_THREAD_ID_OFFSET,
    chunk_vector_id,
    email_chunk_vector_id,
    embedding_space,
)
from src.store.schema import apply_gated, create_database
from tests import factories

SPACE = embedding_space()


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _at(cos: float, base: np.ndarray, other: np.ndarray) -> np.ndarray:
    """A unit vector at cosine `cos` from the unit `base`, in the plane of `other`."""
    o = other - (other @ base) * base
    o /= np.linalg.norm(o)
    return _unit(cos * base + np.sqrt(1 - cos**2) * o)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "embeddings.npz")
    conn = create_database(str(tmp_path / "brain.db"))
    conn.execute("BEGIN IMMEDIATE")
    apply_gated(conn, "test")
    conn.commit()
    yield conn
    conn.close()


RNG = np.random.default_rng(3)
Q = _unit(RNG.standard_normal(768))


def _noise(i):
    return _unit(np.random.default_rng(100 + i).standard_normal(768))


def _write(conn, rows):
    """rows: [(vector id, vector)] of one width."""
    vector_store.write(conn, [r[0] for r in rows], np.array([r[1] for r in rows]), 2)
    conn.commit()


def _candidates(conn, query_vector=Q, limit=10):
    return embeddings.semantic_email_candidates(
        conn, "x", limit=limit, embed_fn=lambda _t: np.array([query_vector], np.float32)
    )


def test_a_passage_brings_its_attachment_and_email_into_the_pool(store):
    conn = store
    mail = factories.email(conn, subject="Annual report")
    att = factories.attachment(conn, email_id=mail, summary="annual report")
    cid = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (att,)
    ).fetchone()[0]
    other = factories.email(conn, subject="Lunch")
    conn.execute(
        "INSERT INTO attachment_chunks (attachment_id, chunk_no, content_id, start_char,"
        " end_char, chars, text_chars, text_extracted_at, chunked_at)"
        " VALUES (?, 4, ?, 16000, 20000, 4000, 40000, '2026-11-01', '2026-11-01')",
        (att, cid),
    )
    _write(
        conn,
        [
            (-cid, _at(0.60, Q, _noise(1))),  # its summary is far
            (chunk_vector_id(cid, 4), _at(0.95, Q, _noise(2))),  # one passage is close
            (other, _at(0.70, Q, _noise(3))),
        ],
    )

    found = _candidates(conn)

    assert list(found) == [mail, other]
    assert found.by_kind["attachment"] == [att]
    assert found.chunks[att]["chunk_no"] == 4 and found.chunks[att]["chunk_offset"] == 16000


def test_an_email_passage_folds_onto_its_email(store):
    conn = store
    mail = factories.email(conn, subject="Harbour plan")
    other = factories.email(conn, subject="Lunch")
    _write(
        conn,
        [
            (mail, _at(0.62, Q, _noise(1))),
            (email_chunk_vector_id(mail, 2), _at(0.93, Q, _noise(2))),
            (other, _at(0.80, Q, _noise(3))),
        ],
    )

    assert list(_candidates(conn)) == [mail, other]


def test_items_under_the_floor_never_enter_the_pool(store):
    conn = store
    near = factories.email(conn, subject="Harbour plan")
    far = factories.email(conn, subject="Lunch")
    tid = factories.teams_thread(conn, summary="pier", extraction_status="extracted")
    floor = embeddings.SIMILARITY_FLOOR
    _write(
        conn,
        [
            (near, _at(floor + 0.2, Q, _noise(1))),
            (far, _at(floor - 0.05, Q, _noise(2))),
            (TEAMS_THREAD_ID_OFFSET - tid, _at(floor - 0.05, Q, _noise(3))),
        ],
    )

    found = _candidates(conn)

    assert list(found) == [near]
    assert found.by_kind["teams"] == []


def test_each_bucket_ranks_its_own_kind(store):
    """A kind never takes another's slots: Teams threads rank among Teams threads."""
    conn = store
    e1, e2 = factories.email(conn, subject="a"), factories.email(conn, subject="b")
    t1 = factories.teams_thread(conn, summary="pier", extraction_status="extracted")
    t2 = factories.teams_thread(conn, summary="quay", extraction_status="extracted")
    _write(
        conn,
        [
            (e1, _at(0.90, Q, _noise(1))),
            (TEAMS_THREAD_ID_OFFSET - t1, _at(0.85, Q, _noise(2))),
            (e2, _at(0.80, Q, _noise(3))),
            (TEAMS_THREAD_ID_OFFSET - t2, _at(0.75, Q, _noise(4))),
        ],
    )

    found = _candidates(conn)

    assert list(found) == [e1, e2]
    assert found.by_kind["teams"] == [t1, t2]


def _same_head_pair(conn):
    a, b = factories.email(conn, subject="a"), factories.email(conn, subject="b")
    head = _at(0.9, Q, _noise(1))
    tail_q = _unit(np.random.default_rng(7).standard_normal(768))
    _write(conn, [(a, np.concatenate([head, -tail_q])), (b, np.concatenate([head, tail_q]))])
    _write(conn, [(chunk_vector_id(1, 0), _noise(9))])
    return a, b, np.concatenate([Q, tail_q])


def test_full_width_scoring_waits_for_its_switch(store):
    conn = store
    a, b, query = _same_head_pair(conn)

    found = _candidates(conn, query_vector=query[:768])

    assert set(list(found)[:2]) == {a, b}, "on the shared 768 the two are one vector"


def test_full_width_summaries_are_scored_at_1536_once_every_summary_is(store, monkeypatch):
    monkeypatch.setenv("BRAIN_WIDE_SUMMARIES", "1")
    conn = store
    a, b = factories.email(conn, subject="a"), factories.email(conn, subject="b")
    head = _at(0.9, Q, _noise(1))
    tail_q = _unit(np.random.default_rng(7).standard_normal(768))
    query = np.concatenate([Q, tail_q])
    # Same first 768: only the tail tells them apart, and only b's tail matches the query's.
    _write(
        conn,
        [
            (a, np.concatenate([head, -tail_q])),
            (b, np.concatenate([head, tail_q])),
        ],
    )
    # A passage at 768, as the table holds them: the table is read at 768, the summaries
    # again at 1536.
    _write(conn, [(chunk_vector_id(1, 0), _noise(9))])
    assert vector_store.load(conn)[1].shape[1] == 768

    found = _candidates(conn, query_vector=query)

    assert list(found)[0] == b, "only b's tail matches; a falls under the floor at 1536"


def test_a_query_that_cannot_be_embedded_still_stands_on_its_keyword_matches(store):
    conn = store
    mail = factories.email(conn, subject="Okapi budget", summary="okapi budget")
    _write(conn, [(mail, _at(0.9, Q, _noise(1)))])

    def refuse(_texts):
        raise RuntimeError("no embedding today")

    found = embeddings.semantic_email_candidates(conn, "okapi budget", embed_fn=refuse)

    assert list(found) == [mail]
    assert found.semantic.startswith("keyword_seeded")
