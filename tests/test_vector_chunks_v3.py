"""Issue 171, part 2: what a store whose vectors are in its table embeds.

- Email bodies: the newest message of a long body (quoted history left out, as the
  extraction does) is cut into chunks, each embedded under the email's subject, sender,
  date and summary as its contextual header, and a chunk's match folds onto its email.
- Attachment copies: of the long attachments with the same bytes (sha256), one is chunked;
  the copies keep their summary vectors.
- Summaries at 1536 dimensions (Matryoshka), searched by their first 768 beside the 768
  of the older rows, until the re-embed lifts the rest.

New items only: the hourly pass chunks the mail received since the step, and the history
waits for `brain maint vectors-backfill`, a dry run unless --apply. A store without the
step (the npz) embeds as before. Every value is invented.
"""

import numpy as np
import pytest

from src.store import embeddings, vector_store
from src.store.embeddings import (
    CHUNK_ID_OFFSET,
    EMAIL_CHUNK_ID_OFFSET,
    TEAMS_THREAD_ID_OFFSET,
    chunk_vector_id,
    email_chunk_of,
    email_chunk_vector_id,
    embedding_space,
)
from src.store.schema import apply_gated, create_database
from tests import factories
from tests.brain_cli import brain
from tests.test_vectors_v2 import FakeClient, _save, _vector

SPACE = embedding_space()
OLD = TEAMS_THREAD_ID_OFFSET - 999


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "embeddings.npz"
    client = FakeClient()
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", path)
    monkeypatch.setattr(embeddings, "_get_client", lambda: client)
    monkeypatch.setattr(embeddings.time, "sleep", lambda _s: None)
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn, path, client
    conn.close()


def _apply(conn, path):
    _save(path, [OLD], [_vector("old", 768)], SPACE, np.float16)
    conn.execute("BEGIN IMMEDIATE")
    apply_gated(conn, "test")
    conn.commit()


def _long_body(words: str, n: int = 12) -> str:
    return "\n\n".join(f"Paragraph {i} about {words}. " + "detail " * 60 for i in range(n))


QUOTED = (
    "\n\nFrom: Bob Brown <bob@corp.example>\nSent: Monday, 2 November 2026 10:00\n"
    "To: Alice Smith <alice@corp.example>\nSubject: harbour\n\n"
    + "the old quoted history repeats here "
    * 200
)


def _rows(conn, kind):
    return conn.execute(
        "SELECT id, parent_id, dim FROM vectors WHERE kind = ? ORDER BY id", (kind,)
    ).fetchall()


# --- ids -----------------------------------------------------------------------------------


def test_an_email_chunk_id_names_its_email_and_lies_below_every_other_namespace():
    vid = email_chunk_vector_id(123_456, 7)
    assert email_chunk_of(vid) == (123_456, 7)
    assert vid <= EMAIL_CHUNK_ID_OFFSET < chunk_vector_id(10**9, 998) <= CHUNK_ID_OFFSET
    assert vector_store.kind_of(vid) == "email_chunk"
    assert vector_store.parent_of(vid) == 123_456
    assert vector_store.kind_of(chunk_vector_id(10**9, 998)) == "chunk"


# --- email bodies ----------------------------------------------------------------------------


def test_a_long_new_email_is_chunked_under_its_header_without_its_quoted_history(store):
    conn, path, client = store
    _apply(conn, path)
    email_id = factories.email(
        conn,
        subject="Harbour plan",
        summary="Repairs to the lighthouse are approved.",
        content=_long_body("the lighthouse") + QUOTED,
    )
    short = factories.email(conn, subject="Thanks", content="Thanks, noted.")

    embeddings.build_index(conn)

    chunks = _rows(conn, "email_chunk")
    assert chunks and all(r["parent_id"] == email_id for r in chunks)
    assert all(r["dim"] == 768 for r in chunks)
    texts = [t for call in client.calls for t in call["texts"] if "Paragraph" in t]
    assert len(texts) == len(chunks)
    assert all(t.startswith("title: Harbour plan | text: Alice Smith, ") for t in texts)
    assert all("Repairs to the lighthouse are approved." in t for t in texts)
    assert not any("old quoted history" in t for t in texts)
    assert not any(r["parent_id"] == short for r in chunks)


def test_mail_received_before_the_step_waits_for_the_backfill(store, monkeypatch, capsys):
    conn, path, _client = store
    old = factories.email(
        conn,
        subject="Old plan",
        content=_long_body("the quay"),
        date_received="2026-01-05T09:00:00Z",
    )
    _apply(conn, path)

    embeddings.build_index(conn)
    assert _rows(conn, "email_chunk") == []

    db = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    code, out, _err = brain(monkeypatch, capsys, "--db", db, "maint", "vectors-backfill")
    assert code == 0, out
    assert "email chunks: 1 emails" in out and "dry run" in out
    assert _rows(conn, "email_chunk") == []

    code, out, _err = brain(
        monkeypatch, capsys, "--db", db, "maint", "vectors-backfill", "--apply", "--limit", "5"
    )
    assert code == 0, out
    assert {r["parent_id"] for r in _rows(conn, "email_chunk")} == {old}


def test_the_backfill_refuses_a_store_without_the_step(store, monkeypatch, capsys):
    conn, _path, _client = store
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    code, _out, err = brain(monkeypatch, capsys, "--db", db, "maint", "vectors-backfill")
    assert code == 2 and "vectors" in err


def test_a_store_without_the_step_chunks_no_email(store):
    conn, path, client = store
    factories.email(conn, subject="Harbour plan", content=_long_body("the lighthouse"))

    embeddings.build_index(conn)

    assert np.load(path)["ids"].tolist() == [1]
    assert not any("Paragraph" in t for call in client.calls for t in call["texts"])


def test_a_passage_match_finds_its_email_once_and_says_where(store):
    conn, path, client = store
    _apply(conn, path)
    email_id = factories.email(conn, subject="Harbour plan", content=_long_body("the lighthouse"))
    embeddings.build_index(conn)
    passage = next(t for call in client.calls for t in call["texts"] if "Paragraph 0 " in t)

    def embed(_texts):
        return np.array([_vector(passage, 768)], dtype=np.float32)

    rows = embeddings.query_semantic(conn, "x", embed_fn=embed)
    assert [r["email_id"] for r in rows if r["type"] == "email"] == [email_id]
    top = rows[0]
    assert top["matched_in"] == "chunk" and top["chunk_no"] == 0
    assert top["chunk_offset"] == 0 and top["chunk_end"] > 0
    assert list(embeddings.semantic_email_candidates(conn, "x", embed_fn=embed)) == [email_id]


def test_forgetting_an_email_takes_its_chunks(store):
    conn, path, _client = store
    _apply(conn, path)
    email_id = factories.email(conn, subject="Harbour plan", content=_long_body("the lighthouse"))
    embeddings.build_index(conn)
    assert _rows(conn, "email_chunk")

    embeddings.remove_vectors([email_id], conn=conn)

    assert _rows(conn, "email_chunk") == [] and _rows(conn, "email") == []


# --- attachment copies -------------------------------------------------------------------------


def _deck(conn, sha):
    return factories.attachment(
        conn,
        filename="deck.pdf",
        text="\n\n".join("Slide text " * 60 for _ in range(40)),
        summary="A deck.",
        sha256=sha,
    )


def test_of_two_copies_of_one_file_only_the_first_is_chunked(store):
    conn, path, _client = store
    _apply(conn, path)
    first, second = _deck(conn, "aa" * 32), _deck(conn, "aa" * 32)
    other = _deck(conn, "bb" * 32)

    embeddings.build_index(conn)

    parents = {r["parent_id"] for r in _rows(conn, "chunk")}
    content = dict(conn.execute("SELECT attachment_id, id FROM attachment_content"))
    assert parents == {-content[first], -content[other]}
    summaries = {r["id"] for r in _rows(conn, "attachment")}
    assert {-content[first], -content[second], -content[other]} <= summaries


def test_the_backfill_drops_the_chunks_copies_already_hold(store, monkeypatch, capsys):
    """The npz chunked every copy; the step copies those rows in, and the backfill drops
    the copies' chunks, keeping the first file's."""
    conn, path, _client = store
    first, second = _deck(conn, "aa" * 32), _deck(conn, "aa" * 32)
    embeddings.build_index(conn)
    conn.execute("BEGIN IMMEDIATE")
    apply_gated(conn, "test")
    conn.commit()
    content = dict(conn.execute("SELECT attachment_id, id FROM attachment_content"))
    held = {r["parent_id"] for r in _rows(conn, "chunk")}
    assert held == {-content[first], -content[second]}
    copies = len([r for r in _rows(conn, "chunk") if r["parent_id"] == -content[second]])
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    code, out, _err = brain(monkeypatch, capsys, "--db", db, "maint", "vectors-backfill", "--apply")

    assert code == 0, out
    assert f"copy chunks dropped: {copies}" in out
    assert {r["parent_id"] for r in _rows(conn, "chunk")} == {-content[first]}


# --- summaries at 1536 -----------------------------------------------------------------------


def test_new_summaries_are_stored_at_1536_and_searched_by_their_first_768(store):
    conn, path, client = store
    _apply(conn, path)
    email_id = factories.email(conn, subject="Harbour plan", summary="harbour lighthouse")

    embeddings.build_index(conn)

    assert [r["dim"] for r in _rows(conn, "email")] == [1536]
    summary_call = next(
        c for c in client.calls if any("harbour lighthouse" in t for t in c["texts"])
    )
    assert summary_call["config"].output_dimensionality == 1536
    _ids, unit, space = vector_store.load(conn)
    assert unit.shape[1] == 768 and space["dim"] == 768, "the older rows set the width"
    text = summary_call["texts"][0]

    def embed(_texts):
        return np.array([_vector(text, 1536)[:768]], dtype=np.float32)

    rows = embeddings.query_semantic(conn, "x", embed_fn=embed)
    assert rows[0]["email_id"] == email_id and rows[0]["similarity"] > 0.99


def test_the_teams_writer_stores_1536_too(store):
    conn, path, _client = store
    _apply(conn, path)
    tid = factories.teams_thread(conn, summary="the pier budget", extraction_status="extracted")

    embeddings.build_teams_index(conn)

    row = conn.execute(
        "SELECT dim FROM vectors WHERE id = ?", (TEAMS_THREAD_ID_OFFSET - tid,)
    ).fetchone()
    assert row["dim"] == 1536
