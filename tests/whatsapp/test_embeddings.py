"""brain whatsapp-sync, step 4: session summaries into the shared vector index.

WhatsApp vectors take the namespace below Teams, WHATSAPP_THREAD_ID_OFFSET - id.
Like Teams, a force rebuild (`embed --force`, build_index) drops them, because it
rewrites the file from the namespaces it owns, and like Teams they are selected
by membership in the file, so the next whatsapp-sync puts them back.
"""

import numpy as np
import pytest

from src.store import embeddings
from src.store.embeddings import (
    TEAMS_THREAD_ID_OFFSET,
    WHATSAPP_THREAD_ID_OFFSET,
    _kind_mask,
    build_index,
    build_whatsapp_index,
    query_semantic,
)

DIM = 4


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "embeddings.npz"
    texts = []

    def fake(batch, client=None):
        texts.extend(batch)
        return np.ones((len(batch), DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", path)
    monkeypatch.setattr(embeddings, "_get_client", lambda: None)
    monkeypatch.setattr(embeddings, "generate_embeddings", fake)
    return path, texts


def _ids(path):
    return sorted(int(i) for i in np.load(path, allow_pickle=False)["ids"])


def _extracted_session(db, n=1):
    db.execute(
        "INSERT INTO whatsapp_chats (chat_jid, name, chat_kind, first_seen_at) "
        "VALUES ('c@g.us', 'Chat A', 'group', '2026-09-01T00:00:00Z')"
    )
    for i in range(n):
        db.execute(
            "INSERT INTO whatsapp_threads (chat_id, anchor_message_id, started_at, ended_at, "
            "summary, extraction_status, extracted_at) VALUES (1, ?, '2026-09-01T10:00:00Z', "
            "'2026-09-01T11:00:00Z', 'Planned the sailing trip', 'extracted', "
            "'2026-09-01T11:05:00Z')",
            (f"a{i}",),
        )
    db.commit()


def test_a_session_summary_lands_below_the_teams_range(db, index):
    path, _ = index
    _extracted_session(db)
    assert build_whatsapp_index(db) == 1
    assert _ids(path) == [WHATSAPP_THREAD_ID_OFFSET - 1]
    assert WHATSAPP_THREAD_ID_OFFSET - 1 < TEAMS_THREAD_ID_OFFSET


def test_whatsapp_vectors_survive_a_force_rebuild_through_the_next_sync(db, index):
    path, _ = index
    _extracted_session(db)
    build_whatsapp_index(db)
    db.execute(
        "INSERT INTO emails (message_id, date_received, summary, subject) "
        "VALUES (1, '2026-09-01T00:00:00Z', 's', 'x')"
    )
    db.commit()
    build_index(db, force=True)
    assert WHATSAPP_THREAD_ID_OFFSET - 1 not in _ids(path)  # dropped, as Teams is
    assert build_whatsapp_index(db) == 1  # the next whatsapp-sync
    assert WHATSAPP_THREAD_ID_OFFSET - 1 in _ids(path)


def test_an_already_embedded_session_is_not_embedded_again(db, index):
    _extracted_session(db)
    build_whatsapp_index(db)
    assert build_whatsapp_index(db) == 0


def test_a_run_embeds_at_most_its_cap(db, index):
    _extracted_session(db, n=3)
    assert build_whatsapp_index(db, limit=2) == 2
    assert build_whatsapp_index(db, limit=2) == 1


def test_the_embedded_text_names_the_chat(db, index):
    _, texts = index
    _extracted_session(db)
    build_whatsapp_index(db)
    assert texts == ["[WhatsApp: Chat A] Planned the sailing trip"]


def test_the_kind_mask_tells_teams_from_whatsapp():
    ids = np.array([5, -3, -2_000_001, TEAMS_THREAD_ID_OFFSET - 7, WHATSAPP_THREAD_ID_OFFSET - 7])
    assert list(_kind_mask(ids, {"teams_thread"})) == [False, False, False, True, False]
    assert list(_kind_mask(ids, {"whatsapp_thread"})) == [False, False, False, False, True]


def test_semantic_search_resolves_a_whatsapp_vector_to_its_session(db, index):
    path, _ = index
    _extracted_session(db)
    build_whatsapp_index(db)
    embeddings._INDEX_CACHE.update({"path": None, "mtime": None})
    hits = query_semantic(
        db, "sailing", limit=5, embed_fn=lambda q: np.ones((1, DIM), np.float32), index_path=path
    )
    assert hits and hits[0]["type"] == "whatsapp_thread"
    assert hits[0]["chat_name"] == "Chat A"
