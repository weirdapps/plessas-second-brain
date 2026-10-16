"""Vectors v2: the index's space drives both sides, 768 dimensions in half precision, and the
passages of long documents get vectors of their own.

The index records {model, dim, task, template}. A writer adds vectors in the space the index
holds and a search embeds its query into it, so the producer keeps writing the legacy space
(gemini-embedding-001, 3072, template 1) until a rebuild swaps in this code's
(gemini-embedding-2, 768, its retrieval prompts, template 2), and a replica still holding
the old index answers correctly meanwhile. A chunk vector's match folds onto its attachment
and says which passage matched.
"""

import hashlib
import json

import numpy as np
import pytest

from src.store import embeddings
from src.store.chunks import plan_chunks
from src.store.embeddings import (
    CHUNK_ID_OFFSET,
    IndexSpaceMismatch,
    RebuildRunning,
    chunk_vector_id,
    embedding_space,
    index_space,
    legacy_space,
)
from src.store.items import read_item
from src.store.recall import recall
from src.store.schema import create_database
from tests import factories


class _Embedding:
    def __init__(self, values):
        self.values = values


class FakeClient:
    """A GenAI client that records each call and returns a vector per Content, seeded by its
    text. Given a plain list of strings it answers as gemini-embedding-2 does: one vector for
    all of them. `scale` stands for gemini-embedding-001's unnormalised truncated output."""

    def __init__(self, scale: float = 1.0):
        self.calls: list[dict] = []
        self.scale = scale
        self.models = self

    def embed_content(self, model, contents, config=None):
        texts = [c if isinstance(c, str) else c.parts[0].text for c in contents]
        dim = getattr(config, "output_dimensionality", None) or 3072
        self.calls.append({"model": model, "texts": texts, "config": config})
        if all(isinstance(c, str) for c in contents) and model == "gemini-embedding-2":
            texts = [" ".join(texts)]

        class R:
            embeddings = [_Embedding(_vector(t, dim, self.scale)) for t in texts]

        return R()


def _vector(text: str, dim: int, scale: float = 1.0) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
    v = np.random.default_rng(seed).standard_normal(dim)
    return list(scale * v / np.linalg.norm(v))


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "embeddings.npz"
    client = FakeClient()
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", path)
    monkeypatch.setattr(embeddings, "_get_client", lambda: client)
    monkeypatch.setattr(embeddings.time, "sleep", lambda _s: None)
    conn = create_database(str(tmp_path / "brain.db"))
    return conn, path, client


def _save(path, ids, vectors, space, dtype=np.float32):
    np.savez(
        str(path),
        ids=np.array(ids, dtype=np.int64),
        vectors=np.asarray(vectors, dtype=dtype),
        space=np.array(json.dumps(space)),
    )


def _paragraphs(n: int) -> str:
    return "\n\n".join(f"Paragraph {i}. " + "word " * 180 for i in range(n))


# --- writers write the index's space -------------------------------------------------------


def test_a_new_index_is_built_in_gemini_embedding_2_at_768_in_half_precision(store):
    conn, path, client = store
    factories.email(conn, subject="Harbour plan", summary="Repairs to the lighthouse.")

    assert embeddings.build_index(conn) == 1

    call = client.calls[0]
    assert call["model"] == "gemini-embedding-2"
    assert call["config"].output_dimensionality == 768
    assert call["config"].task_type is None, "gemini-embedding-2 takes no task_type"
    assert call["texts"][0].startswith("title: Harbour plan | text: Alice Smith, ")
    assert call["texts"][0].endswith(". Repairs to the lighthouse.")
    data = np.load(path)
    assert data["vectors"].dtype == np.float16
    assert data["vectors"].shape == (1, 768)
    assert (
        index_space(path)
        == embedding_space()
        == {
            "model": "gemini-embedding-2",
            "dim": 768,
            "task": "search result",
            "template": 2,
        }
    )


def test_a_legacy_index_keeps_being_written_in_its_own_space(store):
    """Between the merge of this code and the rebuild, the producer's hourly passes add to the
    index they hold: same model, no output_dimensionality, the template-1 texts, float32."""
    conn, path, client = store
    _save(path, [999], [_vector("old", 3072)], legacy_space())
    email_id = factories.email(conn, subject="Harbour plan", summary="Repairs to the lighthouse.")

    assert embeddings.build_index(conn) == 1

    call = client.calls[0]
    assert call["model"] == "gemini-embedding-001"
    assert call["config"] is None
    row = conn.execute(
        "SELECT subject, sender_name, date_received, summary FROM emails WHERE id = ?",
        (email_id,),
    ).fetchone()
    assert call["texts"] == [embeddings._email_embed_text(*row)]
    data = np.load(path)
    assert data["vectors"].dtype == np.float32
    assert data["ids"].tolist() == [999, email_id]
    assert index_space(path) == legacy_space()


def test_a_sliced_legacy_index_is_written_at_768_and_scaled_to_unit_length(store, monkeypatch):
    conn, path, _client = store
    client = FakeClient(scale=0.59)  # what gemini-embedding-001 returns at 768
    monkeypatch.setattr(embeddings, "_get_client", lambda: client)
    _save(path, [999], [_vector("old", 768)], legacy_space(768), np.float16)
    factories.email(conn, subject="Harbour plan")

    embeddings.build_index(conn)

    assert client.calls[0]["model"] == "gemini-embedding-001"
    assert client.calls[0]["config"].output_dimensionality == 768
    vectors = np.load(path)["vectors"].astype(np.float32)
    assert vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=2e-3)


def test_each_text_is_its_own_content_so_gemini_embedding_2_returns_one_vector_each():
    """A plain list of strings is one aggregated vector to gemini-embedding-2; the length
    check would refuse it, and before the Content wrapping every batch would have failed."""
    client = FakeClient()

    out = embeddings.generate_embeddings(["a", "b", "c"], client, space=embedding_space())

    assert out.shape == (3, 768)
    assert len(client.calls[0]["texts"]) == 3


def test_vectors_of_another_space_are_still_refused(store):
    conn, path, _client = store
    _save(path, [999], [_vector("old", 768)], embedding_space(), np.float16)

    with pytest.raises(IndexSpaceMismatch):
        embeddings._append_to_index(
            [-10_000_001], np.ones((1, 768), np.float32), space=legacy_space(768)
        )


def test_the_teams_and_whatsapp_writers_follow_the_index_too(store):
    conn, path, client = store
    _save(path, [999], [_vector("old", 3072)], legacy_space())
    factories.teams_thread(conn, summary="the pier budget", extraction_status="extracted")

    assert embeddings.build_teams_index(conn) == 1

    assert client.calls[0]["model"] == "gemini-embedding-001"
    assert client.calls[0]["texts"] == ["the pier budget"]
    assert index_space(path) == legacy_space()


# --- queries embed into the index's space ------------------------------------------------


def _one_email_index(conn, path, space, dtype=np.float32):
    email_id = factories.email(conn, subject="Lighthouse", summary="harbour lighthouse repairs")
    _save(path, [email_id], [_vector("x", int(space["dim"]))], space, dtype)
    return email_id


@pytest.mark.parametrize(
    ("space", "model", "text", "dim"),
    [
        (legacy_space(), "gemini-embedding-001", "lighthouse", None),
        (legacy_space(768), "gemini-embedding-001", "lighthouse", 768),
        (embedding_space(), "gemini-embedding-2", "task: search result | query: lighthouse", 768),
    ],
    ids=["legacy", "legacy sliced to 768", "gemini-embedding-2"],
)
def test_a_query_is_embedded_into_the_space_the_index_records(store, space, model, text, dim):
    conn, path, client = store
    email_id = _one_email_index(conn, path, space)

    rows = embeddings.query_semantic(conn, "lighthouse", limit=1, index_path=path)

    assert [r["email_id"] for r in rows] == [email_id]
    assert "semantic" not in rows[0], "the query was embedded, not stood in for"
    call = client.calls[-1]
    assert (call["model"], call["texts"]) == (model, [text])
    assert call["config"].output_dimensionality == dim


def test_an_embedder_called_without_a_space_takes_the_default_indexs(store):
    """The eval memoises embed_query and calls it with the texts alone."""
    conn, path, client = store
    _one_email_index(conn, path, embedding_space(), np.float16)

    out = embeddings.embed_query(["lighthouse"])

    assert out.shape == (1, 768)
    assert client.calls[-1]["texts"] == ["task: search result | query: lighthouse"]


def test_a_half_precision_index_ranks_as_the_full_one(tmp_path):
    rng = np.random.default_rng(3)
    vectors = rng.standard_normal((2_000, 64)).astype(np.float32)
    query = rng.standard_normal(64).astype(np.float32)
    query /= np.linalg.norm(query)
    ids = np.arange(1, 2_001)
    rankings = []
    for dtype in (np.float32, np.float16):
        path = tmp_path / f"{np.dtype(dtype).name}.npz"
        _save(path, ids, vectors, embedding_space(64), dtype)
        got_ids, unit, _space = embeddings._load_index_and_space(path)
        assert unit.dtype == dtype
        sims = embeddings._similarities(unit, query)
        rankings.append(got_ids[embeddings._top_indices(sims, got_ids, 10)].tolist())

    assert rankings[0] == rankings[1]


# --- chunk vectors fold onto their attachment ------------------------------------------------


@pytest.fixture
def documents(store):
    """Two emails, each with an attachment: a long cut deck, and a short memo. The index holds
    the emails, both summaries and the deck's chunks; chunk 2 is the passage nearest the
    query vector [1, 0, 0]."""
    conn, path, client = store
    deck_mail = factories.email(conn, subject="Board pack")
    memo_mail = factories.email(conn, subject="Memo")
    deck = factories.attachment(
        conn, email_id=deck_mail, filename="deck.pptx", text=_paragraphs(40), summary="A deck."
    )
    memo = factories.attachment(
        conn, email_id=memo_mail, filename="memo.pdf", text="short", summary="A memo."
    )
    plan = plan_chunks(conn)
    content = {
        aid: conn.execute(
            "SELECT id FROM attachment_content WHERE attachment_id = ?", (aid,)
        ).fetchone()[0]
        for aid in (deck, memo)
    }
    n = len(plan.spans[deck])
    assert n > 3
    ids = [deck_mail, memo_mail, -content[deck], -content[memo]]
    vectors = [[0, 0, 1], [0, 0.2, 1], [0, 1, 0], [0.8, 0.6, 0]]
    for no in range(n):
        ids.append(chunk_vector_id(content[deck], no))
        vectors.append([1, 0, 0] if no == 2 else [0.5, 0.5, 0.7])
    _save(path, ids, vectors, embedding_space(3), np.float16)
    span = conn.execute(
        "SELECT start_char, end_char FROM attachment_chunks WHERE attachment_id = ? "
        "AND chunk_no = 2",
        (deck,),
    ).fetchone()
    return (
        conn,
        path,
        {
            "deck": deck,
            "memo": memo,
            "deck_mail": deck_mail,
            "memo_mail": memo_mail,
            "span": tuple(span),
            "chunks": n,
        },
    )


def _towards(_texts):
    return np.array([[1.0, 0.0, 0.0]], np.float32)


def test_a_passage_match_is_one_row_of_its_attachment_and_says_which_passage(documents):
    conn, path, ids = documents

    rows = embeddings.query_semantic(
        conn, "q", limit=5, kinds=["attachment"], embed_fn=_towards, index_path=path
    )

    assert [r["attachment_id"] for r in rows] == [ids["deck"], ids["memo"]]
    start, end = ids["span"]
    assert {k: rows[0][k] for k in ("matched_in", "chunk_no", "chunk_offset", "chunk_end")} == {
        "matched_in": "chunk",
        "chunk_no": 2,
        "chunk_offset": start,
        "chunk_end": end,
    }
    assert "chunk_no" not in rows[1]


def test_a_passage_match_folds_onto_its_email_and_recalls_attachment_bucket(documents):
    conn, path, ids = documents

    found = embeddings.semantic_email_candidates(
        conn, "q", limit=5, embed_fn=_towards, index_path=path
    )

    assert list(found)[:2] == [ids["deck_mail"], ids["memo_mail"]]
    assert found.by_kind["attachment"] == [ids["deck"], ids["memo"]]
    start, end = ids["span"]
    assert found.chunks == {
        ids["deck"]: {
            "matched_in": "chunk",
            "chunk_no": 2,
            "chunk_offset": start,
            "chunk_end": end,
        }
    }


def test_recalls_attachment_bucket_shows_the_passage_that_matched(documents):
    conn, path, ids = documents

    def provider(c, query, limit):
        return embeddings.semantic_email_candidates(
            c, query, limit, embed_fn=_towards, index_path=path
        )

    out = recall(conn, "zzqx", limit_per_kind=5, semantic_candidates=provider)

    deck = next(r for r in out["attachments"] if r["attachment_id"] == ids["deck"])
    assert (deck["matched_in"], deck["chunk_no"]) == ("chunk", 2)
    assert deck["chunk_offset"] == ids["span"][0]
    assert "attachments" not in out["summary"]["partial_kinds"]


def test_search_attachments_with_a_provider_folds_the_passage_onto_its_document(documents):
    from src.store.query import search_attachments

    conn, path, ids = documents

    def provider(c, query, limit):
        return embeddings.semantic_email_candidates(
            c, query, limit, embed_fn=_towards, index_path=path
        )

    def broken(c, query, limit):
        raise embeddings.SemanticUnavailable("down")

    assert search_attachments(conn, "zzqx") == [], "no word matches: keyword-only finds none"
    rows = search_attachments(conn, "zzqx", semantic_candidates=provider)

    assert [r["attachment_id"] for r in rows] == [ids["deck"], ids["memo"]]
    assert (rows[0]["matched_in"], rows[0]["chunk_no"]) == ("chunk", 2)
    assert rows[1]["matched_in"] == "semantic"
    assert search_attachments(conn, "zzqx", semantic_candidates=broken) == []
    deck = search_attachments(conn, "deck", semantic_candidates=provider)
    assert deck[0]["attachment_id"] == ids["deck"], "the keyword row leads, fused"


def test_the_chunks_of_a_left_out_email_leave_with_it(documents):
    conn, path, ids = documents
    conn.execute(
        "UPDATE emails SET mailbox_name = 'News', source_class = 'news' WHERE id = ?",
        (ids["deck_mail"],),
    )
    conn.commit()

    rows = embeddings.query_semantic(
        conn, "q", limit=5, kinds=["attachment"], embed_fn=_towards, index_path=path
    )

    assert [r["attachment_id"] for r in rows] == [ids["memo"]]


def test_get_item_lists_where_each_passage_lies(documents):
    conn, _path, ids = documents

    item = read_item(conn, "attachment", ids["deck"])["item"]

    assert item["chunks_total"] == ids["chunks"]
    assert item["chunks"][2] == {
        "chunk_no": 2,
        "chunk_offset": ids["span"][0],
        "chunk_end": ids["span"][1],
    }
    assert "chunks" not in read_item(conn, "attachment", ids["memo"])["item"]


def test_removing_an_attachments_vector_removes_its_chunks_too(documents):
    conn, path, ids = documents
    content = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (ids["deck"],)
    ).fetchone()[0]

    assert embeddings.remove_vectors([-content]) == 1 + ids["chunks"]

    left = np.load(path)["ids"]
    assert not (left <= CHUNK_ID_OFFSET).any()
    assert np.load(path)["vectors"].dtype == np.float16


# --- the hourly pass adds the chunks ---------------------------------------------------------


def _chunk_ids(path):
    return sorted(int(i) for i in np.load(path)["ids"] if i <= CHUNK_ID_OFFSET)


def test_the_embedding_pass_cuts_and_embeds_new_long_documents(store):
    conn, path, client = store
    email_id = factories.email(conn, subject="Board pack", date_received="2026-09-30T08:00:00Z")
    deck = factories.attachment(
        conn, email_id=email_id, filename="deck.pptx", text=_paragraphs(30), summary="A deck."
    )
    content = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (deck,)
    ).fetchone()[0]

    embeddings.build_index(conn)

    n = conn.execute(
        "SELECT COUNT(*) FROM attachment_chunks WHERE attachment_id = ?", (deck,)
    ).fetchone()[0]
    assert _chunk_ids(path) == sorted(chunk_vector_id(content, no) for no in range(n))
    chunk_texts = [t for call in client.calls for t in call["texts"] if "\n\nParagraph" in t]
    assert len(chunk_texts) == n
    assert chunk_texts[0].startswith("title: deck.pptx | text: Board pack, 2026-09-30. A deck.")

    client.calls.clear()
    assert embeddings.build_index(conn) == 0
    assert client.calls == []


def test_a_document_cut_again_gets_new_chunk_vectors_and_loses_the_old(store):
    conn, path, client = store
    deck = factories.attachment(conn, filename="deck.pptx", text=_paragraphs(30), summary="d")
    embeddings.build_index(conn)
    before = _chunk_ids(path)
    conn.execute(
        "UPDATE attachment_content SET extracted_text = ?, extracted_at = '2030-01-01T00:00:00Z'"
        " WHERE attachment_id = ?",
        (_paragraphs(12), deck),
    )
    conn.commit()
    client.calls.clear()

    embeddings.build_index(conn)

    after = _chunk_ids(path)
    assert len(after) < len(before)
    assert set(after) <= set(before), "the same ids, made from the new passages"
    assert sum(len(c["texts"]) for c in client.calls) == len(after)


def test_a_document_no_longer_eligible_loses_its_chunk_vectors(store):
    conn, path, _client = store
    deck = factories.attachment(conn, filename="deck.pptx", text=_paragraphs(30), summary="d")
    embeddings.build_index(conn)
    assert _chunk_ids(path)
    conn.execute(
        "UPDATE attachment_content SET summary = NULL, llm_status = 'pending' "
        "WHERE attachment_id = ?",
        (deck,),
    )
    conn.commit()

    embeddings.build_index(conn)

    assert _chunk_ids(path) == []


def test_a_spent_deadline_leaves_the_chunks_for_the_next_pass(store):
    conn, path, _client = store
    factories.attachment(conn, filename="deck.pptx", text=_paragraphs(30), summary="d")

    embeddings.build_index(conn, chunk_deadline_s=0)
    assert _chunk_ids(path) == []
    assert len(np.load(path)["ids"]) == 2, "the email and the summary went in"

    embeddings.build_index(conn, chunk_deadline_s=60)
    assert _chunk_ids(path)


def test_the_switch_turns_chunk_vectors_off_and_keeps_those_made(store, monkeypatch):
    """BRAIN_CHUNK_VECTORS=0: no document is cut and no passage sent, by the pass or the
    rebuild; chunk vectors already made stay, and still fold onto their documents."""
    conn, path, client = store
    deck = factories.attachment(conn, filename="deck.pptx", text=_paragraphs(30), summary="d")
    embeddings.build_index(conn)
    made = _chunk_ids(path)
    assert made
    factories.attachment(conn, filename="other.pdf", text=_paragraphs(30), summary="o")
    monkeypatch.setenv("BRAIN_CHUNK_VECTORS", "0")
    client.calls.clear()

    embeddings.build_index(conn)

    assert _chunk_ids(path) == made
    assert all("\n\nParagraph" not in t for c in client.calls for t in c["texts"])
    cut = conn.execute("SELECT DISTINCT attachment_id FROM attachment_chunks").fetchall()
    assert [r[0] for r in cut] == [deck]


def test_a_legacy_index_gets_no_chunks(store):
    conn, path, _client = store
    _save(path, [999], [_vector("old", 3072)], legacy_space())
    factories.attachment(conn, filename="deck.pptx", text=_paragraphs(30), summary="d")

    embeddings.build_index(conn)

    assert _chunk_ids(path) == []
    assert conn.execute("SELECT COUNT(*) FROM attachment_chunks").fetchone()[0] == 0


# --- the rebuild lock -----------------------------------------------------------------------


def test_no_writer_adds_vectors_while_a_rebuild_holds_its_lock(store):
    conn, path, client = store
    factories.email(conn, subject="Harbour plan")
    factories.teams_thread(conn, summary="the pier budget", extraction_status="extracted")

    with embeddings.hold_rebuild_lock():
        assert embeddings.rebuild_running()
        assert embeddings.build_index(conn) == 0
        assert embeddings.build_teams_index(conn) == 0
        assert embeddings.build_whatsapp_index(conn) == 0
        with pytest.raises(RebuildRunning), embeddings.hold_rebuild_lock():
            pass

    assert client.calls == []
    assert not path.exists()
    assert not embeddings.rebuild_running()
    assert embeddings.build_index(conn) == 1


# --- 768 dimensions at no cost ----------------------------------------------------------------


def test_slicing_cuts_the_legacy_index_to_768_unit_vectors_and_keeps_the_old_one(store):
    conn, path, client = store
    rng = np.random.default_rng(5)
    full = rng.standard_normal((300, 3072)).astype(np.float32)
    full /= np.linalg.norm(full, axis=1, keepdims=True)
    ids = list(range(1, 301))
    np.savez(str(path), ids=np.array(ids, dtype=np.int64), vectors=full)  # unrecorded: legacy

    assert embeddings.slice_index(conn, 768) == legacy_space(768)

    data = np.load(path)
    assert data["ids"].tolist() == ids
    assert data["vectors"].dtype == np.float16 and data["vectors"].shape == (300, 768)
    want = full[:, :768] / np.linalg.norm(full[:, :768], axis=1, keepdims=True)
    cos = np.sum(data["vectors"].astype(np.float32) * want, axis=1)
    assert cos.min() > 0.9999
    assert index_space(path) == legacy_space(768)
    stored = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = 'embedding_space'"
    ).fetchone()[0]
    assert json.loads(stored) == legacy_space(768)
    backup = np.load(embeddings.backup_path())
    assert backup["vectors"].shape == (300, 3072)
    assert client.calls == [], "slicing calls no model"
