"""The vector index says which embedding space its vectors are in, and refuses to mix two.

embeddings.npz held ids and vectors only. Model and dimension were module constants and the
backend an environment switch, so a change of model (gemini-embedding-2's space is not 001's),
of task type, of dimension or of the text each vector is made from would have added vectors
of another space to the same index without a word, and every search would have compared the
two. The index now carries {model, dim, task, template}; every writer refuses to merge vectors
of another space into it, and a search embeds its query only into its own index's space.
"""

import json
import zipfile

import numpy as np
import pytest

from src.store import embeddings
from src.store.embeddings import (
    EMBEDDING_MODEL,
    EMBEDDING_TASK,
    EMBEDDING_TEMPLATE_VERSION,
    IndexSpaceMismatch,
    embedding_space,
    index_space,
    legacy_space,
    query_semantic,
    semantic_email_candidates,
)
from src.store.schema import create_database

DIM = 4
OTHER_MODEL = {"model": "another-embedding-model", "dim": DIM, "task": None, "template": 1}


@pytest.fixture
def index(tmp_path, monkeypatch):
    """The index file, with an embedder that returns DIM-wide vectors and counts its calls."""
    path = tmp_path / "embeddings.npz"
    calls = []

    def fake(texts, client=None):
        calls.append(list(texts))
        return np.ones((len(texts), DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", path)
    monkeypatch.setattr(embeddings, "_get_client", lambda: None)
    monkeypatch.setattr(embeddings, "generate_embeddings", fake)
    return path, calls


def _store():
    conn = create_database(":memory:")
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (5, 5, '2026-01-01', 'Lighthouse', 'harbour lighthouse repairs')"
    )
    conn.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, first_seen_at) "
        "VALUES ('19:t', 'channel', '2026-04-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO teams_threads (chat_id, thread_kind, anchor_message_id, started_at, ended_at,"
        " extraction_status, summary, extracted_at) VALUES (1, 'channel_post', 'P1',"
        " '2026-04-29T08:00', '2026-04-29T08:30', 'extracted', 'a thread', '2026-04-29T08:31')"
    )
    conn.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at) "
        "VALUES ('c@g.us', 'Chat A', 'group', '2026-09-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at, summary,"
        " extraction_status, extracted_at) VALUES (1, 'a0', '2026-09-01T10:00:00Z',"
        " '2026-09-01T11:00:00Z', 'a session', 'extracted', '2026-09-01T11:05:00Z')"
    )
    conn.commit()
    return conn


def _legacy(path, ids, width=DIM):
    """An index as every writer saved it before the space was recorded."""
    np.savez(
        str(path),
        ids=np.array(ids, dtype=np.int64),
        vectors=np.ones((len(ids), width), np.float32),
    )


def _in_space(path, ids, space):
    np.savez(
        str(path),
        ids=np.array(ids, dtype=np.int64),
        vectors=np.ones((len(ids), space["dim"]), np.float32),
        space=np.array(json.dumps(space)),
    )


def _ids(path):
    return sorted(int(i) for i in np.load(path, allow_pickle=False)["ids"])


def _recorded(conn):
    row = conn.execute("SELECT value FROM sync_metadata WHERE key = 'embedding_space'").fetchone()
    return json.loads(row[0]) if row else None


# --- what the index records ------------------------------------------------------------------


def test_the_space_names_the_model_dimension_task_and_template():
    assert embedding_space() == {
        "model": EMBEDDING_MODEL,
        "dim": embeddings.EMBEDDING_DIM,
        "task": EMBEDDING_TASK,
        "template": EMBEDDING_TEMPLATE_VERSION,
    }


def test_a_saved_index_records_its_space_and_the_store_does_too(index):
    path, _calls = index
    conn = _store()

    embeddings.build_index(conn)

    assert index_space(path) == embedding_space(DIM)
    assert _recorded(conn) == embedding_space(DIM)


def test_the_store_is_written_only_when_the_space_changes(index):
    """Every hourly save would otherwise write a row that says the same thing, and commit
    whatever the caller had not committed yet."""
    conn = _store()
    embeddings.build_index(conn)
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (6, 6, '2026-01-02', 'Beacon', 'coastal beacon maintenance')"
    )
    before = conn.total_changes

    assert embeddings.build_index(conn) == 1

    assert conn.total_changes == before
    assert conn.in_transaction, "the caller's own write was committed for it"
    conn.rollback()


def test_the_teams_and_whatsapp_writers_record_it_as_well(index):
    path, _calls = index
    conn = _store()

    embeddings.build_teams_index(conn)
    assert index_space(path) == embedding_space(DIM)
    assert _recorded(conn) == embedding_space(DIM)
    embeddings.build_whatsapp_index(conn)
    assert index_space(path) == embedding_space(DIM)


def test_the_space_is_read_without_the_vectors(tmp_path):
    """The vectors are 1.7 GB on the producer; the space is read from its own member alone."""
    path = tmp_path / "embeddings.npz"
    with zipfile.ZipFile(path, "w") as archive:
        with archive.open("space.npy", "w") as member:
            np.lib.format.write_array(member, np.array(json.dumps(OTHER_MODEL)))
        archive.writestr("ids.npy", b"not an array")
        archive.writestr("vectors.npy", b"not an array either")

    assert index_space(path) == OTHER_MODEL


def test_an_index_from_before_has_no_space_and_none_is_no_index(tmp_path):
    _legacy(tmp_path / "old.npz", [1])

    assert index_space(tmp_path / "old.npz") is None
    assert index_space(tmp_path / "missing.npz") is None


# --- an index from before the space was recorded --------------------------------------------


def test_an_index_from_before_is_taken_as_the_legacy_space_and_tagged_on_its_next_save(index):
    """Every index saved before the space was recorded was built with gemini-embedding-001 from
    the template-1 texts, so it is in the legacy space, at its own vectors' width, and a writer
    goes on adding vectors in it: the producer's hourly passes keep working between the merge
    of the new code and the rebuild that moves the index to this code's space."""
    path, calls = index
    _legacy(path, [1])

    assert embeddings.build_index(_store()) == 1

    assert _ids(path) == [1, 5]
    assert index_space(path) == legacy_space(DIM)
    template_1 = embeddings._email_embed_text(
        "Lighthouse", None, "2026-01-01", "harbour lighthouse repairs"
    )
    assert calls == [[template_1]]


def test_an_append_tags_an_index_from_before_too(index):
    path, _calls = index
    _legacy(path, [1])

    embeddings._append_to_index([-10_000_001], np.ones((1, DIM), np.float32))

    assert index_space(path) == legacy_space(DIM)


def test_a_removal_tags_an_index_from_before_and_keeps_a_recorded_space(index):
    path, _calls = index
    _legacy(path, [1, 2])
    embeddings.remove_vectors([2])
    assert (_ids(path), index_space(path)) == ([1], legacy_space(DIM))

    _in_space(path, [1, 2], OTHER_MODEL)
    embeddings.remove_vectors([2])

    assert (_ids(path), index_space(path)) == ([1], OTHER_MODEL)


# --- merging across spaces is refused --------------------------------------------------------


def _unchanged(path):
    return path.read_bytes()


@pytest.mark.parametrize(
    "stored",
    [
        OTHER_MODEL,
        {**embedding_space(DIM), "template": EMBEDDING_TEMPLATE_VERSION + 1},
        {**embedding_space(DIM), "task": "RETRIEVAL_DOCUMENT"},
    ],
    ids=["model", "template", "task"],
)
def test_build_index_refuses_another_space_before_it_embeds_anything(index, stored):
    path, calls = index
    _in_space(path, [1], stored)
    before = _unchanged(path)

    with pytest.raises(IndexSpaceMismatch, match="embed --force"):
        embeddings.build_index(_store())

    assert calls == []
    assert _unchanged(path) == before


def test_the_teams_and_whatsapp_writers_refuse_it_before_they_embed(index):
    path, calls = index
    _in_space(path, [1], OTHER_MODEL)
    before = _unchanged(path)
    conn = _store()

    with pytest.raises(IndexSpaceMismatch):
        embeddings.build_teams_index(conn)
    with pytest.raises(IndexSpaceMismatch):
        embeddings.build_whatsapp_index(conn)

    assert calls == []
    assert _unchanged(path) == before
    assert conn.execute("SELECT embedding_at FROM teams_threads").fetchone()[0] is None


def test_vectors_of_another_width_are_refused_with_the_reason(index):
    """Before, a narrower index met the new vectors in np.vstack, which raised a shape error
    that named neither the index nor the cause."""
    path, _calls = index
    _legacy(path, [1], width=DIM - 1)
    before = _unchanged(path)

    with pytest.raises(IndexSpaceMismatch, match="dim"):
        embeddings.build_index(_store())
    with pytest.raises(IndexSpaceMismatch, match="dim"):
        embeddings._append_to_index([-10_000_001], np.ones((1, DIM), np.float32))

    assert _unchanged(path) == before


def test_an_index_rewritten_in_another_space_while_embedding_is_not_merged(index, monkeypatch):
    """The merge reads the file again under the lock, as it is at save time, and checks it there."""
    path, _calls = index
    _legacy(path, [1])

    def embed_while_another_writer_rebuilds(texts, client=None):
        _in_space(path, [7], OTHER_MODEL)
        return np.ones((len(texts), DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "generate_embeddings", embed_while_another_writer_rebuilds)

    with pytest.raises(IndexSpaceMismatch):
        embeddings.build_index(_store())

    assert (_ids(path), index_space(path)) == ([7], OTHER_MODEL)


def test_a_forced_rebuild_starts_the_index_over_in_this_codes_space(index):
    path, _calls = index
    _in_space(path, [1], OTHER_MODEL)
    conn = _store()

    assert embeddings.build_index(conn, force=True) == 1

    assert (_ids(path), index_space(path)) == ([5], embedding_space(DIM))
    assert _recorded(conn) == embedding_space(DIM)


# --- the query path --------------------------------------------------------------------------


def _search_store(tmp_path, space=None, width=3):
    """Three emails, only the first holding 'lighthouse'; the second next to it in vector
    space, the third far away."""
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
    path = tmp_path / "emb.npz"
    vectors = np.array([[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 0.0, 1.0]], np.float32)
    members = {"ids": np.array([1, 2, 3], dtype=np.int64), "vectors": vectors[:, :width]}
    if space is not None:
        members["space"] = np.array(json.dumps(space))
    np.savez(str(path), **members)
    return conn, str(path)


def _must_not_embed(_texts):
    raise AssertionError("the query was embedded into another space")


def test_a_query_is_not_embedded_into_an_index_of_another_model(tmp_path, capsys):
    conn, path = _search_store(tmp_path, {**OTHER_MODEL, "dim": 3})

    out = query_semantic(conn, "lighthouse", limit=2, embed_fn=_must_not_embed, index_path=path)

    assert [r["email_id"] for r in out] == [1, 2]
    assert {r["semantic"] for r in out} == {"keyword_seeded: IndexSpaceMismatch"}
    assert "another-embedding-model" in capsys.readouterr().err


def test_hybrid_candidates_fall_back_the_same_way(tmp_path, capsys):
    conn, path = _search_store(tmp_path, {**OTHER_MODEL, "dim": 3})

    out = semantic_email_candidates(
        conn, "lighthouse", limit=2, embed_fn=_must_not_embed, index_path=path
    )

    assert list(out) == [1, 2]
    assert out.semantic == "keyword_seeded: IndexSpaceMismatch"
    assert capsys.readouterr().err


def test_a_query_vector_of_another_width_falls_back_instead_of_failing(tmp_path, capsys):
    conn, path = _search_store(tmp_path)  # an index from before, three wide
    four_wide = lambda _texts: np.array([[0.0, 0.0, 1.0, 0.0]], np.float32)  # noqa: E731

    out = query_semantic(conn, "lighthouse", limit=2, embed_fn=four_wide, index_path=path)

    assert [r["email_id"] for r in out] == [1, 2]
    assert {r["semantic"] for r in out} == {"keyword_seeded: IndexSpaceMismatch"}
    assert "dim" in capsys.readouterr().err


def test_a_template_change_alone_still_embeds_the_query(tmp_path, capsys):
    """A query is embedded as typed: the template is how the documents' texts were built, so a
    query still lands in the space of an index built from another template."""
    space = {**embedding_space(3), "template": EMBEDDING_TEMPLATE_VERSION + 1}
    conn, path = _search_store(tmp_path, space)
    far = lambda _texts: np.array([[0.0, 0.0, 1.0]], np.float32)  # noqa: E731

    out = query_semantic(conn, "lighthouse", limit=1, embed_fn=far, index_path=path)

    assert [r["email_id"] for r in out] == [3]
    assert "semantic" not in out[0]
    assert capsys.readouterr().err == ""


def test_a_query_in_the_index_space_is_embedded_as_before(tmp_path):
    conn, path = _search_store(tmp_path, embedding_space(3))
    far = lambda _texts: np.array([[0.0, 0.0, 1.0]], np.float32)  # noqa: E731

    out = query_semantic(conn, "lighthouse", limit=1, embed_fn=far, index_path=path)

    assert [r["email_id"] for r in out] == [3]
    assert "semantic" not in out[0]
