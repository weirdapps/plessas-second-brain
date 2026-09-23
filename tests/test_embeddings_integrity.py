"""The embedding index must not lose vectors, and must say when it has.

On 2026-09-23 the replica held vectors for 133 of 5,469 extracted Teams threads.
Two causes, both in src/store/embeddings.py:

* `embed --force` (the 2026-09-09 alignment repair) saves only the email,
  attachment and conversation namespaces, and Teams threads are selected by
  `embedding_at` alone, which the rebuild never cleared. So every Teams vector
  it dropped stayed dropped. Emails self-heal because they are selected by
  membership in the file.
* build_index and _append_to_index are separate read-modify-writes of the same
  file, run by different timers, with no lock. A Teams append landing while
  build_index was embedding was overwritten when build_index saved its stale copy.

And scripts/health_check.py checked only the file's age and total count, so it
reported OK throughout.
"""

import fcntl

import numpy as np
import pytest

from src.store import embeddings
from src.store.schema import create_database

DIM = 4


@pytest.fixture
def index(tmp_path, monkeypatch):
    path = tmp_path / "embeddings.npz"
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", path)
    monkeypatch.setattr(embeddings, "_get_client", lambda: None)
    monkeypatch.setattr(
        embeddings,
        "generate_embeddings",
        lambda texts, client=None: np.ones((len(texts), DIM), dtype=np.float32),
    )
    return path


def _ids(path):
    return sorted(int(i) for i in np.load(path, allow_pickle=False)["ids"])


def _db_with_threads(n):
    conn = create_database(":memory:")
    conn.execute(
        "INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at) "
        "VALUES ('19:t', 'channel', '2026-04-01T00:00:00')"
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO teams_threads(chat_id, thread_kind, anchor_message_id, started_at, "
            "ended_at, extraction_status, summary, extracted_at) VALUES "
            "(1, 'channel_post', ?, '2026-04-29T08:00', '2026-04-29T08:30', 'extracted', "
            "'summary', '2026-04-29T08:31')",
            (f"P{i}",),
        )
    conn.commit()
    return conn


def _teams_vid(thread_id):
    return embeddings.TEAMS_THREAD_ID_OFFSET - thread_id


def test_a_teams_thread_missing_from_the_index_is_re_embedded(index):
    conn = _db_with_threads(2)
    assert embeddings.build_teams_index(conn) == 2

    # What `embed --force` or a lost update leaves behind: embedding_at is set,
    # the vector is gone.
    np.savez(str(index), ids=np.array([7], dtype=np.int64), vectors=np.ones((1, DIM), np.float32))

    assert embeddings.build_teams_index(conn) == 2
    assert _ids(index) == sorted([7, _teams_vid(1), _teams_vid(2)])


def test_a_teams_sync_run_embeds_at_most_its_cap(index):
    conn = _db_with_threads(3)

    assert embeddings.build_teams_index(conn, limit=2) == 2
    assert embeddings.build_teams_index(conn, limit=2) == 1
    assert embeddings.build_teams_index(conn, limit=2) == 0


def test_fresh_threads_are_embedded_before_the_repair_backlog(index):
    """The cap must not park this week's threads behind 5,336 old ones whose
    vectors went missing: real staleness first, then the repair, newest first."""
    conn = _db_with_threads(3)
    embeddings.build_teams_index(conn)
    # Threads 1 and 2 lost their vectors (repair backlog); thread 3 is re-extracted.
    np.savez(
        str(index),
        ids=np.array([_teams_vid(3)], dtype=np.int64),
        vectors=np.ones((1, DIM), np.float32),
    )
    conn.execute("UPDATE teams_threads SET extracted_at = '2030-01-01T00:00:00' WHERE id = 3")
    conn.commit()

    pairs = embeddings._teams_threads_to_embed(conn)

    assert [thread_id for thread_id, _ in pairs] == [3, 2, 1]


def test_a_lock_file_this_user_cannot_write_does_not_block_the_index(index):
    """A root-owned or read-only lock file (a manual sudo run creates one) must
    not stop every later writer: flock needs a descriptor, not write access."""
    lock = embeddings._index_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    lock.chmod(0o444)

    embeddings._append_to_index([_teams_vid(1)], np.ones((1, DIM), np.float32))

    assert _ids(index) == [_teams_vid(1)]


def test_a_force_rebuild_no_longer_strands_teams_threads(index):
    conn = _db_with_threads(1)
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, summary) "
        "VALUES (5, 5, '2026-01-01', 'an email')"
    )
    conn.commit()
    embeddings.build_teams_index(conn)
    embeddings.build_index(conn)
    assert _ids(index) == sorted([5, _teams_vid(1)])

    embeddings.build_index(conn, force=True)  # rewrites the email namespace only
    embeddings.build_teams_index(conn)

    assert _ids(index) == sorted([5, _teams_vid(1)])


def test_a_teams_append_during_build_index_is_not_overwritten(index, monkeypatch):
    """The lost update itself: another writer appends while build_index is still
    embedding. build_index must merge into the file as it is at save time."""
    conn = _db_with_threads(0)
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, summary) "
        "VALUES (5, 5, '2026-01-01', 'an email')"
    )
    conn.commit()
    np.savez(str(index), ids=np.array([1], dtype=np.int64), vectors=np.ones((1, DIM), np.float32))

    def embed_while_teams_appends(texts, client=None):
        embeddings._append_to_index([_teams_vid(9)], np.ones((1, DIM), np.float32))
        return np.ones((len(texts), DIM), dtype=np.float32)

    monkeypatch.setattr(embeddings, "generate_embeddings", embed_while_teams_appends)

    embeddings.build_index(conn)

    assert _ids(index) == sorted([1, 5, _teams_vid(9)])


def _assert_saved_under_lock(index, monkeypatch):
    """Patch the save to probe the lock: a non-blocking exclusive flock from a
    second descriptor must fail while a writer holds it."""
    real_save = embeddings._atomic_savez
    probes = []

    def probing_save(path, ids, vectors):
        with open(embeddings._index_lock_path(), "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                probes.append("held")
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)
                probes.append("free")
        real_save(path, ids, vectors)

    monkeypatch.setattr(embeddings, "_atomic_savez", probing_save)
    return probes


def test_build_index_saves_under_the_index_lock(index, monkeypatch):
    conn = _db_with_threads(0)
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, summary) "
        "VALUES (5, 5, '2026-01-01', 'an email')"
    )
    conn.commit()
    probes = _assert_saved_under_lock(index, monkeypatch)

    embeddings.build_index(conn)

    assert probes == ["held"]


def test_teams_append_saves_under_the_index_lock(index, monkeypatch):
    conn = _db_with_threads(1)
    probes = _assert_saved_under_lock(index, monkeypatch)

    embeddings.build_teams_index(conn)

    assert probes == ["held"]
