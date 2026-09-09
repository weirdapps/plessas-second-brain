"""Tests for the cached vector index + hybrid email-candidate helper.

These avoid the real 1 GB embeddings.npz and any Vertex call by writing a tiny
temp index and injecting a fake embedder.
"""

import numpy as np

from src.store.embeddings import (
    CONVERSATION_ID_OFFSET,
    _atomic_savez,
    _email_embed_text,
    _load_index,
    semantic_email_candidates,
)
from src.store.schema import create_database


def _write_npz(path, ids, vecs):
    np.savez(str(path), ids=np.array(ids, dtype=np.int64), vectors=np.array(vecs, dtype=np.float32))


def _fake_embedder(vec):
    """Return an embed_fn that always yields `vec` as the query embedding."""
    arr = np.array([vec], dtype=np.float32)
    return lambda _texts: arr


class TestLoadIndex:
    def test_caches_by_mtime_and_normalizes(self, tmp_path):
        p = tmp_path / "emb.npz"
        _write_npz(p, [1, 2], [[3.0, 0.0, 0.0], [0.0, 5.0, 0.0]])
        _, unit1 = _load_index(str(p))
        _, unit2 = _load_index(str(p))
        assert unit1 is unit2  # served from cache, not reloaded
        assert np.allclose(np.linalg.norm(unit1, axis=1), 1.0)  # unit vectors


class TestSemanticEmailCandidates:
    def test_ranks_closest_email_first(self, tmp_path):
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (10, 10, '2026-01-01', 'a')"
        )
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (20, 20, '2026-01-01', 'b')"
        )
        conn.commit()
        p = tmp_path / "emb.npz"
        _write_npz(p, [10, 20], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        out = semantic_email_candidates(
            conn, "q", limit=5, embed_fn=_fake_embedder([0.0, 1.0, 0.0]), index_path=str(p)
        )
        assert out[0] == 20  # query aligned with email 20's vector
        assert set(out) == {10, 20}

    def test_folds_attachment_vector_onto_parent_email(self, tmp_path):
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (5, 5, '2026-01-01', 'e')"
        )
        cur = conn.execute(
            "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size, "
            "file_path, is_inline, exported_at) VALUES (5, 5, 'f.pdf', 'application/pdf', 1, "
            "'/x', 0, '2026-01-01')"
        )
        aid = cur.lastrowid
        assert aid is not None
        cur2 = conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method, "
            "extraction_status, extracted_at, summary, language, llm_status) VALUES "
            "(?, 't', 'm', 'extracted', '2026-01-01', 's', 'english', 'extracted')",
            (aid,),
        )
        acid = cur2.lastrowid
        assert acid is not None
        conn.commit()
        p = tmp_path / "emb.npz"
        _write_npz(p, [-acid], [[1.0, 0.0, 0.0]])  # attachment namespace = -attachment_content.id
        out = semantic_email_candidates(
            conn, "q", limit=5, embed_fn=_fake_embedder([1.0, 0.0, 0.0]), index_path=str(p)
        )
        assert out == [5]  # attachment folded onto parent email

    def test_skips_non_email_namespaces(self, tmp_path):
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (7, 7, '2026-01-01', 'e')"
        )
        conn.commit()
        p = tmp_path / "emb.npz"
        conv_id = CONVERSATION_ID_OFFSET - 1  # conversation namespace, not an email
        _write_npz(p, [7, conv_id], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        out = semantic_email_candidates(
            conn, "q", limit=5, embed_fn=_fake_embedder([0.0, 1.0, 0.0]), index_path=str(p)
        )
        assert out == [7]  # conversation vector skipped even though it's the closest


class TestAtomicSave:
    def test_roundtrips_and_leaves_no_tmp(self, tmp_path):
        p = tmp_path / "emb.npz"
        _atomic_savez(p, [1, 2], np.array([[1.0, 0.0, 0.0], [0.0, 2.0, 0.0]], dtype=np.float32))
        d = np.load(p, allow_pickle=False)
        assert list(d["ids"]) == [1, 2]
        assert d["vectors"].shape == (2, 3)
        assert not (tmp_path / "emb.tmp.npz").exists()  # temp file cleaned up on success

    def test_failed_write_leaves_existing_index_intact(self, tmp_path, monkeypatch):
        """A crash mid-save must never truncate the previous good index."""
        import src.store.embeddings as emb

        p = tmp_path / "emb.npz"
        _write_npz(p, [1], [[1.0, 0.0, 0.0]])  # existing good index
        good_bytes = p.read_bytes()

        def _boom(*_a, **_k):  # simulate SIGKILL/ENOSPC during np.savez
            raise OSError("interrupted mid-write")

        monkeypatch.setattr(emb.np, "savez", _boom)
        try:
            _atomic_savez(p, [2], np.array([[0.0, 1.0, 0.0]], dtype=np.float32))
        except OSError:
            pass
        assert p.read_bytes() == good_bytes  # original archive untouched, not corrupted


class TestEmailEmbedText:
    def test_enriches_with_metadata(self):
        t = _email_embed_text("Q3 budget", "Maria Novak", "2026-05-01T10:00:00", "approved it")
        assert "Q3 budget" in t
        assert "Maria Novak" in t
        assert "2026-05-01" in t  # date present
        assert "10:00" not in t  # ...truncated to the 10-char date
        assert "approved it" in t

    def test_falls_back_to_subject_when_summary_missing(self):
        assert _email_embed_text("Subject only", None, None, None) == "Subject only"

    def test_falls_back_to_summary_when_no_metadata(self):
        assert _email_embed_text(None, None, None, "just a summary") == "just a summary"

    def test_empty_when_all_missing(self):
        assert _email_embed_text(None, None, None, None) == ""


class TestIncrementalMergeAlignment:
    """The incremental merge in build_index had no test, and was wrong from the
    initial public release until 2026-09-09.

    It concatenated `list(existing_ids)`, a SET, onto the new ids while stacking
    the vectors in FILE order. Set iteration order is hash order, and this index
    namespaces attachments/conversations/Teams as negative ids, so the two orders
    diverge as soon as a negative id is present. 67,723 of 117,101 live vectors
    ended up labelled with another row's id, and semantic search answered with
    confident, unrelated documents. These tests pin the property that was
    violated: whatever else build_index does, position i of `ids` must keep
    describing position i of `vectors`.
    """

    def _existing(self, tmp_path, ids):
        p = tmp_path / "emb.npz"
        # One distinctive vector per id, so a permutation is detectable.
        vecs = [[float(i), 0.0, 0.0] for i in range(len(ids))]
        _write_npz(p, ids, vecs)
        return p

    def test_set_ordering_would_have_permuted_negative_ids(self, tmp_path):
        """Guard the assumption, not just the code: prove the old approach breaks
        on this index's own id shape, so nobody reintroduces it as a tidy-up.
        """
        ids = [1, 2, 3, -1, -2, -3]
        assert list({int(x) for x in ids}) != ids

    def test_merge_preserves_file_order(self, tmp_path, monkeypatch):
        import src.store.embeddings as emb

        ids = [1, 2, 3, -1, -2, -3]
        path = self._existing(tmp_path, ids)
        monkeypatch.setattr(emb, "EMBEDDINGS_FILE", path)
        # One new email row to embed, so the merge branch is exercised.
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary) "
            "VALUES (99, 99, '2026-09-01T00:00:00Z', 'S', 'a summary')"
        )
        conn.commit()
        monkeypatch.setattr(
            emb,
            "generate_embeddings",
            lambda texts, client=None: np.zeros((len(texts), 3), dtype=np.float32),
        )
        monkeypatch.setattr(emb, "_get_client", lambda: None)

        emb.build_index(conn, force=False)

        data = np.load(path, allow_pickle=False)
        assert data["ids"].tolist() == ids + [99]
        assert len(data["ids"]) == len(data["vectors"])
        # Position i still describes vector i: the first six rows are unchanged.
        for i in range(len(ids)):
            assert data["vectors"][i][0] == float(i)
        conn.close()

    def test_refuses_to_extend_a_misaligned_index(self, tmp_path, monkeypatch):
        import src.store.embeddings as emb

        p = tmp_path / "emb.npz"
        np.savez(
            str(p),
            ids=np.array([1, 2, 3], dtype=np.int64),
            vectors=np.array([[1.0, 0, 0], [2.0, 0, 0]], dtype=np.float32),
        )
        monkeypatch.setattr(emb, "EMBEDDINGS_FILE", p)
        conn = create_database(":memory:")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary) "
            "VALUES (99, 99, '2026-09-01T00:00:00Z', 'S', 'a summary')"
        )
        conn.commit()
        try:
            emb.build_index(conn, force=False)
            raise AssertionError("expected a RuntimeError on a misaligned index")
        except RuntimeError as e:
            assert "misaligned" in str(e)
        conn.close()


class TestGenerateEmbeddingsAlignment:
    """A batch that contributes fewer rows than it was given shifts every id
    after it onto the wrong vector. Both paths must raise, not return short.
    """

    def _client(self, n_returned):
        class _Emb:
            def __init__(self, v):
                self.values = v

        class _Models:
            def embed_content(self, model, contents):
                class R:
                    embeddings = [_Emb([0.0, 0.0, 0.0]) for _ in range(n_returned)]

                return R()

        class _C:
            models = _Models()

        return _C()

    def test_raises_when_api_returns_fewer_than_requested(self):
        from src.store.embeddings import generate_embeddings

        try:
            generate_embeddings(["a", "b", "c"], client=self._client(2))
            raise AssertionError("expected a RuntimeError on a short batch")
        except RuntimeError as e:
            assert "refusing to misalign" in str(e)

    def test_raises_when_rate_limit_retries_are_exhausted(self, monkeypatch):
        import src.store.embeddings as emb

        monkeypatch.setattr(emb.time, "sleep", lambda _s: None)

        class _Models:
            def embed_content(self, model, contents):
                raise RuntimeError("429 RESOURCE_EXHAUSTED")

        class _C:
            models = _Models()

        try:
            emb.generate_embeddings(["a"], client=_C())
            raise AssertionError("expected a RuntimeError after retry exhaustion")
        except RuntimeError as e:
            assert "still rate-limited" in str(e)
