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
