"""`brain maint vectors-rebuild`: every vector again in this code's space, then one swap.

An index never mixes two spaces, so moving to gemini-embedding-2 at 768 dimensions means
embedding every summary and every chunk of the long documents again. The rebuild does it into
a side directory, in parts, as Gemini batch jobs or online, resumes where a token budget or a
crash stopped it, catches up with what the store gained meanwhile, and swaps the finished
index in, keeping the old one beside it.
"""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from src.store import embeddings, vector_rebuild
from src.store.embeddings import (
    CHUNK_ID_OFFSET,
    TEAMS_THREAD_ID_OFFSET,
    embedding_space,
    index_space,
    legacy_space,
)
from src.store.schema import create_database
from tests import factories
from tests.test_vectors_v2 import FakeClient, _paragraphs, _save, _vector


class QueueFull(Exception):
    code = 429

    def __str__(self):
        return "429 RESOURCE_EXHAUSTED: batch enqueued tokens"


class BatchClient(FakeClient):
    """FakeClient with the files and batches services: a job reads its request file and
    finishes when it is next looked at, unless `fail` names how many jobs fail first; `drop`
    keys get no line. `queue` caps the characters the jobs not yet looked at may hold, as the
    service caps a key's enqueued tokens, refusing a job past it with a 429."""

    def __init__(self, fail: int = 0, drop: tuple[int, ...] = (), queue: int | None = None):
        super().__init__()
        self.store: dict[str, bytes] = {}
        self.jobs: dict[str, str] = {}
        self.queued: dict[str, int] = {}
        self.refused = 0
        self.queue = queue
        self.deleted: list[str] = []
        self.fail = fail
        self.drop = set(drop)
        self.files = SimpleNamespace(
            upload=self._upload, download=self._download, delete=self._delete
        )
        self.batches = SimpleNamespace(
            create_embeddings=self._create, get=self._get, delete=self._delete
        )
        self.batch_texts = 0

    def _upload(self, file, config=None):
        name = f"files/in{len(self.store)}"
        with open(file, "rb") as f:
            self.store[name] = f.read()
        return SimpleNamespace(name=name)

    def _download(self, file):
        return self.store[file]

    def _delete(self, name):
        self.deleted.append(name)

    def _create(self, model, src, config=None):
        size = len(self.store[src["file_name"]])
        if self.queue is not None and sum(self.queued.values()) + size > self.queue:
            self.refused += 1
            raise QueueFull()
        name = f"batches/{len(self.jobs)}"
        self.queued[name] = size
        lines = []
        for line in self.store[src["file_name"]].decode().splitlines():
            record = json.loads(line)
            key = int(record["key"])
            self.batch_texts += 1
            if key in self.drop:
                continue
            text = record["request"]["content"]["parts"][0]["text"]
            dim = record["request"].get("output_dimensionality", 3072)
            response = {
                "embedding": {"values": _vector(text, dim)},
                "usageMetadata": {"promptTokenCount": 7},
            }
            lines.append(json.dumps({"key": record["key"], "response": response}))
        out = f"files/out{len(self.jobs)}"
        self.store[out] = "\n".join(lines).encode()
        self.jobs[name] = out
        return SimpleNamespace(name=name)

    def _get(self, name):
        self.queued.pop(name, None)
        if self.fail:
            self.fail -= 1
            return SimpleNamespace(state=SimpleNamespace(name="JOB_STATE_FAILED"), dest=None)
        return SimpleNamespace(
            state=SimpleNamespace(name="JOB_STATE_SUCCEEDED"),
            dest=SimpleNamespace(file_name=self.jobs[name]),
        )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store with every kind, its index in the legacy space as the producer holds it now."""
    path = tmp_path / "embeddings.npz"
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", path)
    monkeypatch.setattr(embeddings.time, "sleep", lambda _s: None)
    monkeypatch.setattr(vector_rebuild.time, "sleep", lambda _s: None)
    monkeypatch.setenv("BRAIN_EMBED_BACKEND", "gemini")
    conn = create_database(str(tmp_path / "brain.db"))
    mails = [factories.email(conn, subject=f"Plan {i}") for i in range(3)]
    factories.attachment(
        conn, email_id=mails[0], filename="deck.pptx", text=_paragraphs(30), summary="A deck."
    )
    factories.attachment(conn, email_id=mails[1], filename="memo.pdf", text="short", summary="m")
    thread = factories.teams_thread(conn, extracted_at="2026-10-01T08:00:00+00:00")
    _save(path, [*mails, TEAMS_THREAD_ID_OFFSET - thread], [_vector("o", 3072)] * 4, legacy_space())
    return conn, path, thread


def _kinds(path):
    ids = np.load(path)["ids"]
    return {
        "email": int((ids > 0).sum()),
        "chunk": int((ids <= CHUNK_ID_OFFSET).sum()),
        "summary": int(((ids < 0) & (ids > embeddings.CONVERSATION_ID_OFFSET)).sum()),
        "teams": int(((ids <= TEAMS_THREAD_ID_OFFSET) & (ids > -1_000_000_000)).sum()),
    }


def test_the_estimate_sends_nothing_and_writes_nothing(store):
    conn, path, _thread = store
    before = path.read_bytes()

    found = vector_rebuild.estimate(conn)

    assert found["by_kind"]["email"]["texts"] == 3
    assert found["by_kind"]["attachment"]["texts"] == 2
    assert found["by_kind"]["teams"]["texts"] == 1
    chunk = found["by_kind"]["chunk"]
    assert chunk["documents"] == 1 and chunk["texts"] > 3
    assert found["tokens"] == sum(r["tokens"] for r in found["by_kind"].values())
    assert found["usd_batch"] == pytest.approx(found["usd_online"] / 2, abs=0.01)
    assert conn.execute("SELECT COUNT(*) FROM attachment_chunks").fetchone()[0] == 0
    assert path.read_bytes() == before
    assert not vector_rebuild.side_dir().exists()


@pytest.mark.parametrize("online", [True, False], ids=["online", "batch"])
def test_a_rebuild_swaps_in_every_kind_in_this_codes_space(store, online):
    conn, path, thread = store
    client = BatchClient()

    report = vector_rebuild.rebuild(conn, online=online, client=client)

    assert report["state"] == "swapped"
    assert index_space(path) == embedding_space()
    data = np.load(path)
    assert data["vectors"].dtype == np.float16 and data["vectors"].shape[1] == 768
    chunks = conn.execute("SELECT COUNT(*) FROM attachment_chunks").fetchone()[0]
    assert _kinds(path) == {"email": 3, "chunk": chunks, "summary": 2, "teams": 1}
    assert report["vectors"]["chunk"] == chunks
    backup = np.load(embeddings.backup_path())
    assert backup["vectors"].shape == (4, 3072), "the old index is kept for a rollback"
    stored = conn.execute("SELECT value FROM sync_metadata WHERE key = 'embedding_space'")
    assert json.loads(stored.fetchone()[0]) == embedding_space()
    assert not vector_rebuild.side_dir().exists()
    stamp = conn.execute("SELECT embedding_at FROM teams_threads WHERE id = ?", (thread,))
    assert stamp.fetchone()[0] is not None, "the thread reads as embedded after the swap"
    if not online:
        assert report["jobs_collected"] >= 1 and report["tokens_billed"] > 0
        sent = {f for f in client.store if f.startswith("files/in")} | set(client.jobs)
        assert sent <= set(client.deleted), "the texts sent, and the jobs, are deleted"


def test_the_writers_continue_in_the_new_space_after_the_swap(store, monkeypatch):
    conn, path, _thread = store
    client = BatchClient()
    vector_rebuild.rebuild(conn, online=True, client=client)
    monkeypatch.setattr(embeddings, "_get_client", lambda: client)
    factories.email(conn, subject="After the swap", summary="new mail")
    client.calls.clear()

    assert embeddings.build_index(conn) == 1

    assert client.calls[0]["model"] == "gemini-embedding-2"
    assert client.calls[0]["texts"][0].startswith("title: After the swap | text: ")
    assert np.load(path)["vectors"].dtype == np.float16


def test_a_token_budget_stops_the_run_and_the_next_carries_on(store, monkeypatch):
    conn, path, _thread = store
    monkeypatch.setattr(vector_rebuild, "ONLINE_PART_TEXTS", 2)
    client = BatchClient()

    first = vector_rebuild.rebuild(conn, online=True, token_budget=40, client=client)

    assert first["state"] == "budget"
    assert index_space(path) == legacy_space(), "nothing swapped yet"
    sent = sum(len(c["texts"]) for c in client.calls)
    assert 0 < sent < first.get("embedded_online", 0) + 100
    state = vector_rebuild.status()
    assert state["vectors"] == first["embedded_online"] and not state["running"]

    factories.email(conn, subject="Late mail", summary="stored while the rebuild paused")
    second = vector_rebuild.rebuild(conn, online=True, client=client)

    assert second["state"] == "swapped"
    total = sum(len(c["texts"]) for c in client.calls)
    texts = [t for c in client.calls for t in c["texts"]]
    assert len(set(texts)) == len(texts) == total, "no text was embedded twice"
    assert _kinds(path)["email"] == 4, "the mail stored meanwhile is in"


def test_a_failed_job_goes_out_again_and_a_missing_line_is_embedded_online(store):
    conn, path, _thread = store
    first_email = int(np.load(path)["ids"][0])
    client = BatchClient(fail=1, drop=(first_email,))

    report = vector_rebuild.rebuild(conn, client=client)

    assert report["state"] == "swapped"
    assert report["jobs_sent"] == 2, "the failed job was sent once more"
    assert report["embedded_online"] == 1, "the line the job left out went online"
    assert first_email in np.load(path)["ids"].tolist()


def test_jobs_close_at_their_token_size_and_wait_for_room_in_the_queue(store, monkeypatch):
    """The service caps a key's enqueued batch tokens and refuses a job past the cap with a
    429; the rebuild sends what fits, collects, and sends the rest as jobs finish."""
    conn, path, _thread = store
    monkeypatch.setattr(vector_rebuild, "BATCH_JOB_TOKENS", 2_000)
    client = BatchClient(queue=12_000)

    report = vector_rebuild.rebuild(conn, client=client)

    assert report["state"] == "swapped"
    assert report["jobs_sent"] >= 3
    assert client.refused >= 1, "the queue filled at least once"
    assert index_space(path) == embedding_space()
    texts = sum(len(job) for job in client.jobs)
    assert texts and not client.queued


def test_a_queue_smaller_than_one_job_halves_the_job_size(store, monkeypatch):
    conn, path, _thread = store
    monkeypatch.setattr(vector_rebuild, "BATCH_JOB_MIN_TOKENS", 500)
    client = BatchClient(queue=12_000)  # one 1,000,000-token job could never go

    report = vector_rebuild.rebuild(conn, client=client)

    assert report["state"] == "swapped"
    assert client.refused >= 1
    assert index_space(path) == embedding_space()
    assert not [f for f in client.store if f.startswith("files/in") and f not in client.deleted]


def test_a_queue_that_takes_nothing_gives_up_instead_of_spinning(store, monkeypatch):
    conn, path, _thread = store
    client = BatchClient(queue=10)

    with pytest.raises(vector_rebuild.RebuildRefused, match="--online"):
        vector_rebuild.rebuild(conn, client=client)

    assert index_space(path) == legacy_space()
    assert vector_rebuild.status() is not None, "the side build stays, to resume"


def test_no_wait_leaves_the_jobs_out_and_the_next_run_collects_them(store):
    conn, path, _thread = store
    client = BatchClient()

    first = vector_rebuild.rebuild(conn, wait=False, client=client)
    assert first["state"] in ("pending", "swapped")
    if first["state"] == "pending":
        assert vector_rebuild.status()["jobs_pending"] == first["jobs_pending"]
        assert index_space(path) == legacy_space()
        assert vector_rebuild.rebuild(conn, client=client)["state"] == "swapped"
    assert index_space(path) == embedding_space()


def test_a_second_rebuild_waits_its_turn(store):
    conn, _path, _thread = store
    with embeddings.hold_rebuild_lock(), pytest.raises(embeddings.RebuildRunning):
        vector_rebuild.rebuild(conn, client=BatchClient())


def test_a_side_build_of_another_space_is_refused_until_abandoned(store):
    conn, _path, _thread = store
    vector_rebuild.side_dir().mkdir(parents=True)
    vector_rebuild._save_state(vector_rebuild._new_state(legacy_space(768), "batch"))

    with pytest.raises(vector_rebuild.RebuildRefused, match="--abandon"):
        vector_rebuild.rebuild(conn, client=BatchClient())

    assert vector_rebuild.abandon()
    assert vector_rebuild.rebuild(conn, client=BatchClient())["state"] == "swapped"


def test_a_thread_extracted_again_during_the_rebuild_stays_stale(store, monkeypatch):
    """Its text was read before it changed, so teams-sync must embed it again after the swap."""
    conn, _path, thread = store
    monkeypatch.setattr(
        vector_rebuild,
        "_now",
        lambda: "2026-10-01T07:00:00+00:00",  # before the thread's extraction at 08:00
    )

    vector_rebuild.rebuild(conn, online=True, client=BatchClient())

    stale = embeddings._teams_threads_to_embed(conn)
    assert [tid for tid, _text in stale] == [thread]


def test_the_cli_estimate_runs_on_a_replica_and_the_build_does_not(store, monkeypatch, capsys):
    from tests.brain_cli import brain

    conn, _path, _thread = store
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    # A pulled copy holds no WAL; a host with a pull stamp opens one immutable.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code, out, err = brain(
        monkeypatch, capsys, "--db", db, "maint", "vectors-rebuild", "--estimate"
    )
    assert (code, err) == (0, "")
    assert "as batch jobs" in out

    code, _out, err = brain(monkeypatch, capsys, "--db", db, "maint", "vectors-rebuild")
    assert code == 2 and "replica" in err
