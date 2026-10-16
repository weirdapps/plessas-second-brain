"""Semantic search via embeddings for the second-brain knowledge store.

Embeds item summaries, and the passages of long PDF, Word and PowerPoint attachments
(src/store/chunks.py), with Google's embedding models, and ranks them by cosine similarity.

The index records the space its vectors are in, {model, dim, task, template}, and that
space drives both sides: a writer adds vectors in the space the index holds and a search
embeds its query into it, so a producer holding a rebuilt index and a replica still
holding the old one both answer correctly while the new one travels. A new index is built
in this code's space, embedding_space(): gemini-embedding-2 at 768 dimensions, with its
retrieval prompts and the template-2 texts. An index from before 2026-10-16 is in
legacy_space(): gemini-embedding-001 at 3072, no task, the template-1 texts.
"""

import contextvars
import fcntl
import gc
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from src.config import DATA_ROOT, embed_backend
from src.store.chunks import chunk_texts, plan_chunks
from src.store.schema import SUMMARISED_ATTACHMENT_IDS_SQL
from src.store.source_class import hidden_ids_sql, with_source_class

# Summaries fetched per query for the attachments missing from the index; under SQLite's
# 999-variable limit on older builds.
ATTACHMENT_FETCH_CHUNK = 500

# The space this code builds a new index in (embedding_space): gemini-embedding-2, MMTEB
# 69.9 against 68.32 for 001, 8,192 input tokens, $0.20 per million tokens online and half
# in batch mode. 768 dimensions: Google's Matryoshka table puts 768 about 0.2 points below
# full, and the index takes a quarter of the memory. The model takes no task_type: its task
# is a prompt, "task: search result | query: ..." for a query and "title: ... | text: ..."
# for a document (the template-2 texts below).
EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIM = 768
EMBEDDING_TASK: str | None = "search result"
# The version of the texts the vectors are made from. 1: _email_embed_text, the attachment,
# conversation and WhatsApp prefixes and the bare Teams summary, as every index before
# 2026-10-16 holds them. 2: the same items as "title: ... | text: ...", and the chunks of
# long documents. Bump it with any change to the texts: a vector made from another text is
# in another space, which an index refuses to mix with its own.
EMBEDDING_TEMPLATE_VERSION = 2

# The space of every index saved before 2026-10-16 (legacy_space).
LEGACY_MODEL = "gemini-embedding-001"
LEGACY_DIM = 3072
# Both models return 3072 dimensions unless asked for fewer (output_dimensionality).
NATIVE_DIM = 3072
# Models whose task is a prompt prefix rather than a task_type.
PROMPTED_MODELS = frozenset({EMBEDDING_MODEL})

EMBEDDINGS_FILE = DATA_ROOT / "embeddings.npz"
# The npz member naming the space the vectors are in, beside "ids" and "vectors": a JSON
# object {model, dim, task, template}. Read from its own member, so no check loads a vector.
SPACE_MEMBER = "space"
# The sync_metadata key that keeps the same object in the store, for SQL to read.
EMBEDDING_SPACE_KEY = "embedding_space"
BATCH_SIZE = 100  # Google API batch limit
LOG_FILE = DATA_ROOT / "embeddings.log"

# A new index is saved in half precision: 768 float16 values are 1.5 KB a vector, and the
# cosine of two unit vectors moves by at most 4e-5 (measured on 228,000 random unit
# vectors). An index keeps the dtype it was saved in, so an index of float32 vectors stays
# float32 until it is rebuilt.
STORE_DTYPE = np.float16
# A float16 index is scanned this many rows at a time, each block widened to float32 for
# BLAS: 27 ms for 228,000 x 768 on the reference Mac against 17 ms in float32 and 238 ms
# for numpy's own half-precision product, in half the memory.
SCAN_BLOCK = 16_384

# Namespace offsets: emails (positive), attachments (negative), conversations (-2M and below)
CONVERSATION_ID_OFFSET = -2_000_000

# Teams thread embeddings live below conversations.
# Conversations occupy the -2_000_000 .. -1 range so we put teams below it.
TEAMS_THREAD_ID_OFFSET = -10_000_000

# WhatsApp session embeddings live below Teams. Teams owns
# (WHATSAPP_THREAD_ID_OFFSET, TEAMS_THREAD_ID_OFFSET], room for 990M threads.
WHATSAPP_THREAD_ID_OFFSET = -1_000_000_000

# At most this many chunk texts are made in a pass with a deadline (the hourly sync's):
# 30 embedding calls, more than its 60 s can send, so a backlog never builds them all.
CHUNK_TEXTS_PER_PASS = 3_000

# Chunk vectors live below WhatsApp, which owns (CHUNK_ID_OFFSET, WHATSAPP_THREAD_ID_OFFSET].
# A chunk's id is CHUNK_ID_OFFSET - (attachment_content.id * CHUNK_STRIDE + chunk_no), so the
# attachment a chunk belongs to is read off its id with no lookup, and a chunk's match folds
# onto its attachment's summary vector (-attachment_content.id) by arithmetic alone.
CHUNK_ID_OFFSET = -1_000_000_000_000
CHUNK_STRIDE = 1_000

# In-process cache of the embedding index, keyed by file mtime. query_semantic and
# the hybrid-recall candidate helper both hit this, so a long-lived process (the MCP
# server) loads and unit-normalizes the index ONCE instead of on every query.
# Reloads automatically when embeddings.npz changes (a sync job appended vectors).
_INDEX_CACHE: dict = {"path": None, "mtime": None, "ids": None, "unit": None, "space": None}
# The HTTP server runs each session's tool call in a worker thread, so callers
# overlap. Unlocked, every one that missed the cache loaded its own copy: 3.3 GB
# for two cold callers on the 1.6 GB index, 5 GB for two after a rewrite, against
# a 4 GB MemoryMax. Every read and write of _INDEX_CACHE holds this lock.
_CACHE_LOCK = threading.Lock()

# The space generate_embeddings and embed_query embed into when the caller names none: set
# by the writer or search that knows it (in_space), so the injection points keep their
# one-argument shape (`embed_fn(texts)`).
_ACTIVE_SPACE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "embedding_space", default=None
)


class IndexSpaceMismatch(RuntimeError):
    """The index holds vectors of another embedding space than the ones offered to it."""


class RebuildRunning(RuntimeError):
    """Another vector rebuild holds the rebuild lock."""


def embedding_space(dim: int | None = None) -> dict:
    """The space this code builds a new index in: its model, the vectors' dimension, the task
    and the version of the texts the vectors are made from. Vectors compare only within one
    space: a model's space is not another's (gemini-embedding-2's is not 001's), and a task,
    an output dimension or another text moves every vector."""
    return {
        "model": EMBEDDING_MODEL,
        "dim": int(EMBEDDING_DIM if dim is None else dim),
        "task": EMBEDDING_TASK,
        "template": EMBEDDING_TEMPLATE_VERSION,
    }


def legacy_space(dim: int = LEGACY_DIM) -> dict:
    """The space of every index saved before 2026-10-16, at `dim` (3072 as built, 768 once
    sliced by slice_index)."""
    return {"model": LEGACY_MODEL, "dim": int(dim), "task": None, "template": 1}


def writable(space: dict) -> bool:
    """Whether this code can add vectors in `space`: its model, task and template are this
    code's or the legacy space's, at any dimension."""
    families = {
        (EMBEDDING_MODEL, EMBEDDING_TASK, EMBEDDING_TEMPLATE_VERSION),
        (LEGACY_MODEL, None, 1),
    }
    return (space.get("model"), space.get("task"), space.get("template")) in families


def has_chunks(space: dict) -> bool:
    """Whether an index in `space` holds chunk vectors: template 2 and later."""
    return int(space.get("template") or 0) >= 2


@contextmanager
def in_space(space: dict | None) -> Iterator[None]:
    """Embed into `space` inside this block, wherever the embedder is called from."""
    token = _ACTIVE_SPACE.set(space)
    try:
        yield
    finally:
        _ACTIVE_SPACE.reset(token)


def _space_array(space: dict) -> np.ndarray:
    return np.array(json.dumps(space, sort_keys=True))


def _parse_space(array: np.ndarray) -> dict:
    return json.loads(str(array[()]))


def _archive_space(archive: zipfile.ZipFile) -> dict | None:
    """The space recorded in an open index archive, or None when it records none."""
    name = f"{SPACE_MEMBER}.npy"
    if name not in archive.namelist():
        return None
    with archive.open(name) as member:
        return _parse_space(np.lib.format.read_array(member, allow_pickle=False))


def _header(member) -> tuple[tuple[int, ...], np.dtype]:
    """The shape and dtype of an .npy member, from its header alone."""
    major, _minor = np.lib.format.read_magic(member)
    if major == 1:
        shape, _fortran, dtype = np.lib.format.read_array_header_1_0(member)
    else:
        shape, _fortran, dtype = np.lib.format.read_array_header_2_0(member)
    return shape, dtype


def _npz_space(data) -> dict | None:
    """The space recorded in an index opened with np.load, or None when it records none."""
    return _parse_space(data[SPACE_MEMBER]) if SPACE_MEMBER in data.files else None


def index_space(index_path=None) -> dict | None:
    """The space the index records, read without its vectors; None for an index saved before
    the space was recorded, and for no index at all."""
    path = EMBEDDINGS_FILE if index_path is None else Path(index_path)
    if not path.exists():
        return None
    with zipfile.ZipFile(path) as archive:
        return _archive_space(archive)


def file_space(index_path=None) -> dict | None:
    """The space the index's vectors are in, read without them: the recorded one, the legacy
    space at the vectors' width for an index saved before it was recorded, None for none."""
    path = EMBEDDINGS_FILE if index_path is None else Path(index_path)
    if not path.exists():
        return None
    with zipfile.ZipFile(path) as archive:
        space = _archive_space(archive)
        if space is not None:
            return space
        with archive.open("vectors.npy") as member:
            shape, _dtype = _header(member)
    return legacy_space(shape[1] if len(shape) == 2 else LEGACY_DIM)


def _width(vectors) -> int:
    return int(vectors.shape[1]) if vectors.ndim == 2 else 0


def _differences(held: dict, offered: dict) -> str:
    return ", ".join(
        f"{key} {held.get(key)!r} (offered {offered.get(key)!r})"
        for key in ("model", "dim", "task", "template")
        if held.get(key) != offered.get(key)
    )


_START_OVER = (
    "Rebuild it with `python -m src.cli maint vectors-rebuild` (every kind, resumable), or "
    "start it over with `python -m src.cli embed --force`; after the latter the next "
    "teams-sync and whatsapp-sync add their vectors back."
)


def _refuse_mix(held: dict, offered: dict) -> None:
    raise IndexSpaceMismatch(
        f"{EMBEDDINGS_FILE} holds vectors of another embedding space than the ones offered to "
        f"it: {_differences(held, offered)}. Refusing to mix two spaces in one index. "
        + _START_OVER
    )


def _refuse_unwritable(space: dict) -> None:
    raise IndexSpaceMismatch(
        f"{EMBEDDINGS_FILE} holds vectors of a space this code does not write: {space}. "
        "Refusing to add vectors of another space to it. " + _START_OVER
    )


def _merge_space(held: dict | None, width: int, offered: dict) -> None:
    """Refuse vectors of `offered` space a place in an index of another one.

    `held` is what the index records; an index saved before the space was recorded is in the
    legacy space at its own vectors' `width`, and gets the space written on this save.
    """
    if held is None:
        held = legacy_space(width)
    if held != offered:
        _refuse_mix(held, offered)


def _record_space(conn: sqlite3.Connection, space: dict) -> None:
    """Keep the space of the index just saved in sync_metadata too, where SQL can read it with
    no index at hand. Written and committed only when it changed, so an hourly save neither
    writes nor commits anything the caller left pending."""
    conn.execute("CREATE TABLE IF NOT EXISTS sync_metadata (key TEXT PRIMARY KEY, value TEXT)")
    value = json.dumps(space, sort_keys=True)
    row = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = ?", (EMBEDDING_SPACE_KEY,)
    ).fetchone()
    if row is None or row[0] != value:
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
            (EMBEDDING_SPACE_KEY, value),
        )
        conn.commit()


def _load_index(index_path=None):
    """Return (ids, unit_vectors) for the embedding index; see _load_index_and_space."""
    ids, unit, _space = _load_index_and_space(index_path)
    return ids, unit


def _normalise(vectors: np.ndarray) -> np.ndarray:
    """Scale every row to unit length IN PLACE, a block at a time, and return the array.

    `vectors` is a fresh array the caller owns, so dividing into it is safe, and it is the
    difference between one allocation and three: `vectors / norms` builds a second full array
    and `.astype()` copies again even when the dtype already matches. Every MCP server process
    pays this on its first semantic query, and there are routinely a dozen of them alive at
    once across sessions. The norms come from einsum, which sums the squares row by row:
    np.linalg.norm squared the whole array into a second one first, and the allocator kept
    it, so a process held 2.84 GB after its first recall, not 1.43. A float16 index is
    normalised in float32 blocks and written back in half precision.
    """
    for start in range(0, len(vectors), SCAN_BLOCK):
        block = vectors[start : start + SCAN_BLOCK]
        wide = block.astype(np.float32, copy=False)
        norms = np.sqrt(np.einsum("ij,ij->i", wide, wide))[:, None]
        norms[norms == 0] = 1
        if block.dtype == np.float32:
            block /= norms
        else:
            block[...] = wide / norms
    return vectors


def _load_index_and_space(index_path=None):
    """Return (ids, unit_vectors, space) for the embedding index, cached by file mtime.

    Vectors are L2-normalized once at load, so cosine similarity at query time is a
    single matrix-vector product (_similarities), in the dtype the file holds. `space` is what
    the index records, None for an index saved before it was recorded. Raises
    FileNotFoundError if no index exists. Concurrent callers share one load: the others wait
    for it, then read the cache.
    """
    path = EMBEDDINGS_FILE if index_path is None else Path(index_path)
    if not path.exists():
        raise FileNotFoundError("No embedding index found. Run 'python -m src.cli embed' first.")
    with _CACHE_LOCK:
        # The hit is checked under the lock as well: the cache is five fields, and
        # a check outside it could match the path and mtime, then read the None a
        # reload had just put in their place.
        mtime = path.stat().st_mtime
        if _INDEX_CACHE["path"] == str(path) and _INDEX_CACHE["mtime"] == mtime:
            return _INDEX_CACHE["ids"], _INDEX_CACHE["unit"], _INDEX_CACHE["space"]
        # Let go of the stale index before reading the new one, so a reload holds
        # one index, plus the old one only while a reader is still mid-call.
        _INDEX_CACHE.update({"path": None, "mtime": None, "ids": None, "unit": None, "space": None})
        data = np.load(path, allow_pickle=False)
        ids = data["ids"]
        space = _npz_space(data)
        vectors = data["vectors"]
        if vectors.dtype not in (np.float16, np.float32):
            vectors = vectors.astype(np.float32)
        unit = _normalise(np.ascontiguousarray(vectors))
        _INDEX_CACHE.update(
            {"path": str(path), "mtime": mtime, "ids": ids, "unit": unit, "space": space}
        )
        return ids, unit, space


def _similarities(unit: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Cosine similarity of the unit query vector with every row, as float32."""
    query = np.asarray(query, dtype=np.float32)
    if unit.dtype == np.float32:
        return unit @ query
    out = np.empty(len(unit), dtype=np.float32)
    for start in range(0, len(unit), SCAN_BLOCK):
        out[start : start + SCAN_BLOCK] = (
            unit[start : start + SCAN_BLOCK].astype(np.float32) @ query
        )
    return out


def _log(msg: str):
    """Log a message to file and stdout (nohup-safe — no print())."""
    from datetime import datetime

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    # Write to stderr which is less likely to block than stdout
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except OSError:
        pass


def _index_lock_path() -> Path:
    return EMBEDDINGS_FILE.with_name(EMBEDDINGS_FILE.name + ".lock")


@contextmanager
def _index_lock():
    """Serialise every read-modify-write of EMBEDDINGS_FILE across processes.

    build_index (hourly and daily syncs) and _append_to_index (teams-sync) run
    from different timers and each rewrites the whole file. Unlocked, an append
    that landed while build_index was embedding was overwritten by the stale copy
    build_index then saved: 45 Teams vectors were lost that way after 2026-09-09.
    Hold it only around load-merge-save, never around the network calls.
    """
    path = _index_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Read-only is enough for flock, so a lock file this user cannot write
    # (left by a manual sudo run, say) cannot block every later writer.
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def rebuild_lock_path() -> Path:
    """The lock a vector rebuild holds for as long as it runs (src/store/vector_rebuild.py)."""
    return EMBEDDINGS_FILE.with_name(EMBEDDINGS_FILE.name + ".rebuild.lock")


@contextmanager
def hold_rebuild_lock() -> Iterator[None]:
    """Hold the rebuild lock, or raise RebuildRunning when another process holds it. The
    kernel lets go of it when the process ends, however it ends, so a rebuild that dies never
    leaves the writers locked out."""
    path = rebuild_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o666)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RebuildRunning(f"another vector rebuild holds {path}") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def rebuild_running() -> bool:
    """Whether a vector rebuild holds its lock now. Every writer that adds vectors asks first
    and, while one runs, adds none: the rebuild embeds what they would have, before its swap,
    and the producer's memory holds one vector job at a time (docs/DEPLOY.md, section 12)."""
    path = rebuild_lock_path()
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _skip_for_rebuild(what: str) -> bool:
    if not rebuild_running():
        return False
    _log(f"A vector rebuild is running: {what} adds no vectors now; the rebuild embeds them")
    return True


def _index_state(index_path=None) -> tuple[set[int], dict]:
    """The ids in the index and the space a writer adds vectors in, read without the vectors.

    The space is the index's own, so the producer keeps writing the space it holds until a
    rebuild swaps in another; an index saved before the space was recorded is in the legacy
    space at its vectors' width; with no index it is this code's. Refuses, before anyone pays
    for embeddings, a misaligned file (the vector count comes from the .npy header inside the
    archive, not the data) and a file in a space this code does not write.
    """
    path = EMBEDDINGS_FILE if index_path is None else Path(index_path)
    if not path.exists():
        return set(), embedding_space()
    # One handle for every member: a writer's atomic replace between two opens
    # would pair the old ids with the new header and read as misalignment.
    with zipfile.ZipFile(path) as archive:
        space = _archive_space(archive)
        with archive.open("ids.npy") as member:
            raw_ids = np.lib.format.read_array(member, allow_pickle=False)
        with archive.open("vectors.npy") as member:
            shape, _dtype = _header(member)
    n_vectors = shape[0]
    if space is None:
        space = legacy_space(shape[1] if len(shape) == 2 else LEGACY_DIM)
    elif not writable(space):
        _refuse_unwritable(space)
    if len(raw_ids) != n_vectors:
        raise RuntimeError(
            f"{path} holds {len(raw_ids)} ids for {n_vectors} vectors; "
            "refusing to extend a misaligned index"
        )
    return {int(x) for x in raw_ids}, space


def _indexed_ids() -> set[int]:
    """The ids currently in EMBEDDINGS_FILE, read without loading the vectors (_index_state)."""
    return _index_state()[0]


def _atomic_savez(path, ids, vectors, space: dict | None = None) -> None:
    """Persist the id/vector arrays to ``path`` atomically, with the space they are in.

    ``np.savez`` streams the archive to its target incrementally, so a process killed
    mid-write — e.g. an OOM SIGKILL during a sync — leaves a truncated file that later fails
    to load with "File is not a zip file". Writing to a sibling temp file and
    ``replace``-ing it into place means ``path`` is only ever the previous good archive or
    the complete new one, never a half-written zip.

    ``space`` goes in the SPACE_MEMBER member; None means this code's space at the vectors'
    width. The vectors are saved in the dtype they come in: the merges keep the file's.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    vectors = np.asarray(vectors)
    if space is None:
        space = embedding_space(_width(vectors))
    # np.savez auto-appends ".npz" if missing — strip it from our tmp basename.
    tmp_base = path.with_suffix(".tmp")  # e.g. .../embeddings.tmp
    np.savez(
        tmp_base,
        ids=np.asarray(ids, dtype=np.int64),
        vectors=vectors,
        space=_space_array(space),  # the member SPACE_MEMBER names
    )
    tmp_path = tmp_base.with_suffix(".tmp.npz")  # actual file np.savez wrote
    tmp_path.replace(path)


def _merged(existing, new) -> np.ndarray:
    """`existing` and `new` stacked, in the existing file's dtype (STORE_DTYPE for none)."""
    if existing is None:
        return np.asarray(new).astype(STORE_DTYPE, copy=False)
    return np.concatenate([existing, np.asarray(new).astype(existing.dtype, copy=False)])


def _get_client():
    """Get a Google GenAI client: the Gemini API with GEMINI_API_KEY when
    BRAIN_EMBED_BACKEND=gemini, or when the key is set and the switch is not
    (src.config.embed_backend); Vertex AI (ADC auth) otherwise.

    The Gemini API serves both embedding models, so its vectors join an index of
    either. The switch exists because the Vertex project can refuse the model while
    it still serves Claude, and every host sets a Vertex project.
    """
    import os

    from google import genai

    backend = embed_backend()
    if backend not in ("vertex", "gemini"):
        raise ValueError(f"BRAIN_EMBED_BACKEND must be vertex or gemini; got {backend!r}")
    if backend == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("BRAIN_EMBED_BACKEND=gemini needs GEMINI_API_KEY")
        return genai.Client(api_key=api_key)

    project = os.environ.get("VERTEX_SDK_PROJECT") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
    # Embeddings run on Gemini via Vertex. Use the dedicated embed region —
    # NOT VERTEX_SDK_REGION, which now points at `eu` for Claude/Opus extraction.
    # gemini-embedding-001 is served from europe-west1.
    region = os.environ.get("VERTEX_REGION_EMBED") or "europe-west1"
    if project:
        return genai.Client(vertexai=True, project=project, location=region)
    # Fallback to API key if no Vertex project configured
    api_key = os.environ.get("GEMINI_API_KEY")
    return genai.Client(api_key=api_key) if api_key else genai.Client()


def _current_space(space: dict | None) -> dict:
    """The space to embed into: the one named, else the one the caller set (in_space), else
    this code's."""
    return space or _ACTIVE_SPACE.get() or embedding_space()


def _contents(texts: list[str]) -> list:
    """Each text as a Content of its own. gemini-embedding-2 makes ONE vector of a plain list
    of strings, aggregated over all of them (verified 2026-10-16: four texts, one vector);
    a Content each gives one vector each, from either model."""
    from google.genai import types

    return [types.Content(parts=[types.Part.from_text(text=text)]) for text in texts]


def embed_config(space: dict, timeout_ms: int | None = None):
    """The EmbedContentConfig for `space`, or None when it needs none: output_dimensionality
    below the model's 3072, task_type for a model that takes one, and a timeout."""
    from google.genai import types

    fields: dict = {}
    if int(space["dim"]) != NATIVE_DIM:
        fields["output_dimensionality"] = int(space["dim"])
    if space.get("task") and space["model"] not in PROMPTED_MODELS:
        fields["task_type"] = space["task"]
    if timeout_ms:
        fields["http_options"] = types.HttpOptions(timeout=timeout_ms)
    return types.EmbedContentConfig(**fields) if fields else None


def _rows_of(result, n: int, space: dict, where: str) -> np.ndarray:
    """The vectors of an embed_content result as float32 rows, one per input, refusing a
    short answer. gemini-embedding-001 returns a truncated vector unnormalised (norm about
    0.59 at 768), so its rows are scaled to unit length; gemini-embedding-2 normalises its
    own."""
    returned = list(result.embeddings)
    if len(returned) != n:
        raise RuntimeError(
            f"embed_content returned {len(returned)} embeddings for {n} inputs{where}; "
            "refusing to misalign the index"
        )
    rows = np.asarray([emb.values for emb in returned], dtype=np.float32)
    if space["model"] == LEGACY_MODEL and int(space["dim"]) < NATIVE_DIM and rows.size:
        norms = np.linalg.norm(rows, axis=1, keepdims=True)
        norms[norms == 0] = 1
        rows /= norms
    return rows


def generate_embeddings(texts: list[str], client=None, *, space: dict | None = None) -> np.ndarray:
    """Generate embeddings for a list of texts in `space` (_current_space).

    Args:
        texts: List of text strings to embed, as the space's template builds them
        client: Optional pre-configured GenAI client

    Returns:
        numpy array of shape (len(texts), the space's dim), float32
    """
    space = _current_space(space)
    if client is None:
        client = _get_client()
    config = embed_config(space)
    extra = {"config": config} if config is not None else {}

    # Write each batch straight into a preallocated float32 slab. The previous
    # version accumulated every vector in a Python list and converted once at the
    # end, which is not a style question at this scale: a 3072-float row costs
    # ~98 KB as Python floats against 12 KB as float32, so a full rebuild of
    # 112,686 items needed 11.1 GB and the producer has 7 GB. It was OOM-killed
    # at batch 850 of 1127 after 55 minutes, which means `embed --force` could
    # not repair a corrupt index on this host at all. Peak is now the slab plus
    # one batch.
    out = np.empty((len(texts), int(space["dim"])), dtype=np.float32)
    written = 0
    total_batches = (len(texts) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_num, i in enumerate(range(0, len(texts), BATCH_SIZE), 1):
        batch = texts[i : i + BATCH_SIZE]

        # Retry with exponential backoff on rate limits.
        #
        # Both the exhaustion path and the short-response path have to raise. The
        # caller pairs this array positionally with its `ids` list, so a batch
        # that quietly contributes 0 rows (5 failed 429s, then falling out of the
        # loop) or fewer rows than it was given does not lose 100 embeddings: it
        # shifts every id after it onto the wrong vector, which is invisible and
        # unrecoverable. Failing the run is cheap because the hourly job retries.
        for attempt in range(5):
            try:
                result = client.models.embed_content(
                    model=space["model"], contents=_contents(batch), **extra
                )
                rows = _rows_of(result, len(batch), space, f" in batch {batch_num}")
                if rows.shape[1] != out.shape[1]:
                    raise IndexSpaceMismatch(
                        f"embed_content returned vectors of dim {rows.shape[1]} for a space of "
                        f"dim {out.shape[1]}"
                    )
                out[written : written + len(rows)] = rows
                written += len(rows)
                break
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    wait = min(2**attempt * 5, 60)
                    _log(f"Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        else:
            raise RuntimeError(
                f"embed_content still rate-limited after 5 attempts on batch "
                f"{batch_num}/{total_batches}; aborting so the index stays aligned"
            )

        if batch_num % 50 == 0 or batch_num == total_batches:
            _log(
                f"Progress: {batch_num}/{total_batches} batches ({min(i + BATCH_SIZE, len(texts))}/{len(texts)} texts)"
            )

        if i + BATCH_SIZE < len(texts):
            time.sleep(1.5)  # Stay under 3000 req/min limit

    if written != len(texts):
        raise RuntimeError(
            f"wrote {written} embeddings for {len(texts)} inputs; refusing to "
            "return a short array that would misalign the index"
        )
    return out


# A search waits on its query's embedding, so that call gets a short timeout and
# at most one retry. generate_embeddings' backoff (5 + 10 + 20 + 40 + 60 s on a
# 429, on a client with no timeout) is for ingest, where waiting is cheap: on the
# query path it stalled a search for up to 135 s before the keyword-seeded
# fallback could run.
QUERY_EMBED_TIMEOUT_MS = 4_000
QUERY_EMBED_RETRY_PAUSE_S = 0.5


def _worth_a_retry(exc: Exception) -> bool:
    """False for a refusal a second try cannot change: a 4xx other than 429.

    google-genai's errors carry the HTTP status as `code`; a timeout or a dropped
    connection carries none and may well succeed on the second try.
    """
    code = getattr(exc, "code", None)
    return not (isinstance(code, int) and 400 <= code < 500 and code != 429)


# The last failed query embedding in this process, for `stats`: the exception's
# type and when, never its message, which can quote the query or a credential.
_LAST_EMBED_ERROR: dict | None = None


def last_embed_error() -> dict | None:
    """The last failed query embedding in this process, {"type", "at"}, or None."""
    return dict(_LAST_EMBED_ERROR) if _LAST_EMBED_ERROR else None


def query_text(space: dict, text: str) -> str:
    """A query as `space` embeds it: under a prompted model, "task: <task> | query: <text>";
    as typed otherwise."""
    if space["model"] in PROMPTED_MODELS and space.get("task"):
        return f"task: {space['task']} | query: {text}"
    return text


def _default_query_space() -> dict:
    """The space a query is embedded into when no search named one: the default index's,
    else this code's."""
    try:
        return file_space() or embedding_space()
    except (OSError, ValueError, zipfile.BadZipFile):
        return embedding_space()


def embed_query(texts: list[str], client=None, *, space: dict | None = None) -> np.ndarray:
    """Embed search queries: 4 s per call and at most one retry, then raise.

    Same contract as generate_embeddings (one row per text), so either serves as a search's
    `embed_fn`. The space is the one named, else the one the search set (in_space: the
    index it loaded), else the default index's. The caller falls back to the keyword-seeded
    vector. A failure is recorded for `stats` (last_embed_error) before it is raised.
    """
    global _LAST_EMBED_ERROR
    try:
        return _embed_query(texts, client, space or _ACTIVE_SPACE.get() or _default_query_space())
    except Exception as e:
        _LAST_EMBED_ERROR = {
            "type": type(e).__name__,
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        raise


def _embed_query(texts: list[str], client, space: dict) -> np.ndarray:
    if client is None:
        client = _get_client()
    config = embed_config(space, timeout_ms=QUERY_EMBED_TIMEOUT_MS)
    contents = _contents([query_text(space, t) for t in texts])
    for attempt in range(2):
        try:
            result = client.models.embed_content(
                model=space["model"], contents=contents, config=config
            )
            break
        except Exception as e:
            if attempt or not _worth_a_retry(e):
                raise
            time.sleep(QUERY_EMBED_RETRY_PAUSE_S)
    return _rows_of(result, len(texts), space, "")


# --- the texts the vectors are made from, by template --------------------------------------


def _email_embed_text(subject, sender_name, date_received, summary) -> str:
    """Metadata-enriched text for embedding an email (template 1).

    Prepends subject / sender / date (10-char ISO) to the LLM summary so the vector
    captures who/when/what-subject, not just the summary prose. This is a lightweight,
    no-extra-LLM variant of contextual retrieval — the "context" is structured
    metadata already on the row, so there is no extra generation cost, only a richer
    string to embed. Falls back gracefully when pieces are missing.
    """
    subject = (subject or "").strip()
    sender = (sender_name or "").strip()
    date = (date_received or "")[:10]
    head = " · ".join(b for b in (subject, sender, date) if b)
    summary = (summary or "").strip()
    if head and summary:
        return f"{head} — {summary}"
    return head or summary


def _document(title, text) -> str:
    """A document as gemini-embedding-2's retrieval prompt takes it."""
    return f"title: {(title or '').strip() or 'none'} | text: {(text or '').strip()}"


def email_text(space: dict, subject, sender_name, date_received, summary) -> str:
    """The text of an email's vector in `space`."""
    if space["template"] < 2:
        return _email_embed_text(subject, sender_name, date_received, summary)
    meta = ", ".join(b for b in ((sender_name or "").strip(), (date_received or "")[:10]) if b)
    summary = (summary or "").strip()
    return _document(subject, f"{meta}. {summary}" if meta and summary else meta or summary)


def attachment_text(space: dict, filename, summary) -> str:
    """The text of an attachment's summary vector in `space`."""
    if space["template"] < 2:
        return f"[Attachment: {filename}] {summary}"
    return _document(filename, summary)


def conversation_text(space: dict, project, summary) -> str:
    """The text of a conversation's vector in `space`."""
    project = project or "unknown"
    if space["template"] < 2:
        return f"[Conversation in {project}] {summary}"
    return _document(f"Conversation in {project}", summary)


def teams_text(space: dict, title, summary) -> str:
    """The text of a Teams thread's vector in `space`: the bare summary under template 1."""
    return summary if space["template"] < 2 else _document(title, summary)


def whatsapp_text(space: dict, chat_name, summary) -> str:
    """The text of a WhatsApp session's vector in `space`."""
    if space["template"] < 2:
        return f"[WhatsApp: {chat_name or 'chat'}] {summary}"
    return _document(chat_name or "WhatsApp chat", summary)


def chunk_vector_id(content_id: int, chunk_no: int) -> int:
    """The index id of chunk `chunk_no` of the attachment whose content row is `content_id`."""
    return CHUNK_ID_OFFSET - (int(content_id) * CHUNK_STRIDE + int(chunk_no))


def chunk_of(vector_id: int) -> tuple[int, int]:
    """(attachment_content.id, chunk_no) of a chunk vector's id."""
    return divmod(CHUNK_ID_OFFSET - int(vector_id), CHUNK_STRIDE)


def _parents(ids: np.ndarray) -> np.ndarray:
    """Each vector's item: a chunk's attachment summary id (-attachment_content.id), every
    other vector's own id. `ids` itself when the index holds no chunk."""
    if not len(ids) or ids.min() > CHUNK_ID_OFFSET:
        return ids
    chunk = ids <= CHUNK_ID_OFFSET
    return np.where(chunk, -((CHUNK_ID_OFFSET - ids) // CHUNK_STRIDE), ids)


# --- the items of each kind --------------------------------------------------------------


def _email_items(conn: sqlite3.Connection, space: dict, indexed: set[int] | None):
    """(id, text) of the summarised emails, those not in `indexed` when given."""
    rows = conn.execute("""
        SELECT id, summary, subject, sender_name, date_received FROM emails
        WHERE summary IS NOT NULL AND summary != ''
    """).fetchall()
    return [
        (
            r["id"],
            email_text(space, r["subject"], r["sender_name"], r["date_received"], r["summary"]),
        )
        for r in rows
        if indexed is None or r["id"] not in indexed
    ]


def _attachment_items(conn: sqlite3.Connection, space: dict, indexed: set[int] | None):
    """(-attachment_content.id, text) of the summarised attachments, those not in `indexed`.

    The ids come first, from the index that holds them (schema v32), and only the missing
    ones are fetched. A row keeps its stored text ahead of its summary, so fetching the
    summary of all 39k attachments to keep the few that are new read about 3 GB: a minute
    and more with a cold cache, on every pass, twice in each sync."""
    out: list[tuple[int, str]] = []
    try:
        att_ids = [r[0] for r in conn.execute(SUMMARISED_ATTACHMENT_IDS_SQL)]
        wanted = sorted(i for i in att_ids if indexed is None or -i not in indexed)
        for start in range(0, len(wanted), ATTACHMENT_FETCH_CHUNK):
            chunk = wanted[start : start + ATTACHMENT_FETCH_CHUNK]
            att_rows = conn.execute(
                f"""
                SELECT ac.id, ac.summary, a.filename
                FROM attachment_content ac
                JOIN attachments a ON a.id = ac.attachment_id
                WHERE ac.id IN ({",".join("?" * len(chunk))})
                  AND ac.llm_status = 'extracted'
                  AND ac.summary IS NOT NULL AND ac.summary != ''
                ORDER BY ac.id
                """,
                chunk,
            ).fetchall()
            out.extend(
                (-r["id"], attachment_text(space, r["filename"], r["summary"])) for r in att_rows
            )
    except sqlite3.OperationalError:
        pass  # attachment tables may not exist in older schemas
    return out


def _conversation_items(conn: sqlite3.Connection, space: dict, indexed: set[int] | None):
    """(CONVERSATION_ID_OFFSET - id, text) of the summarised conversations."""
    try:
        rows = conn.execute("""
            SELECT id, summary, project_name FROM conversations
            WHERE summary IS NOT NULL AND summary != ''
        """).fetchall()
    except sqlite3.OperationalError:
        return []  # conversation tables may not exist in older schemas
    out = []
    for r in rows:
        vid = CONVERSATION_ID_OFFSET - r["id"]
        if indexed is None or vid not in indexed:
            out.append((vid, conversation_text(space, r["project_name"], r["summary"])))
    return out


def summary_items(conn: sqlite3.Connection, space: dict) -> list[tuple[int, str]]:
    """(vector id, text) of every summary an index in `space` holds: emails, attachments,
    conversations, extracted Teams threads and WhatsApp sessions. For the rebuild."""
    items = _email_items(conn, space, None)
    items += _attachment_items(conn, space, None)
    items += _conversation_items(conn, space, None)
    items += [(TEAMS_THREAD_ID_OFFSET - tid, text) for tid, text in _teams_rows(conn, space)]
    items += [(WHATSAPP_THREAD_ID_OFFSET - tid, text) for tid, text in _whatsapp_rows(conn, space)]
    return items


def chunk_ids(plan) -> set[int]:
    """The ids of every chunk vector `plan` (chunks.ChunkPlan) says the index should hold."""
    return {
        chunk_vector_id(plan.content[aid], no)
        for aid, spans in plan.spans.items()
        for no, _start, _end in spans
    }


def chunk_items(conn: sqlite3.Connection, plan, aids, wanted=None) -> Iterator[tuple[int, str]]:
    """(vector id, text) of the chunks of the attachments `aids`, those in `wanted` when given;
    each attachment's text read once."""
    for aid in aids:
        spans = plan.spans.get(aid) or []
        cid = plan.content[aid]
        todo = [s for s in spans if wanted is None or chunk_vector_id(cid, s[0]) in wanted]
        if todo:
            for (no, _start, _end), text in zip(todo, chunk_texts(conn, aid, todo), strict=True):
                yield chunk_vector_id(cid, no), text


def _plan_chunks(conn: sqlite3.Connection, deadline: float | None):
    """chunks.plan_chunks, or None for a store without attachment_chunks (before v39)."""
    try:
        return plan_chunks(conn, deadline=deadline)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return None
        raise


def _embed_until(texts: list[str], client, deadline: float | None) -> np.ndarray:
    """generate_embeddings over `texts`, BATCH_SIZE at a time, until `deadline` (monotonic)
    passes; the vectors of the batches done, in order."""
    done = []
    for start in range(0, len(texts), BATCH_SIZE):
        if deadline is not None and time.monotonic() >= deadline:
            break
        done.append(generate_embeddings(texts[start : start + BATCH_SIZE], client))
    if not done:
        return np.empty((0, 0), dtype=np.float32)
    return np.vstack(done)


def build_index(
    conn: sqlite3.Connection, force: bool = False, *, chunk_deadline_s: float | None = None
) -> int:
    """Add the vectors the index lacks, in the index's space: emails, attachments,
    conversations and, in a space with chunks, the chunks of long documents.

    Skips items that already have a vector unless force=True, which starts the index over in
    this code's space. IDs use distinct namespaces: email IDs are positive, attachment IDs
    are negative (negated attachment_content.id), conversations, Teams, WhatsApp and chunks
    below. `chunk_deadline_s` bounds the chunk part (cutting and embedding) in wall-clock
    seconds: what does not fit waits for the next pass. Adds nothing while a vector rebuild
    runs. Saves the index once.

    Returns:
        Number of new embeddings generated
    """
    if not force and _skip_for_rebuild("the embedding pass"):
        return 0
    # Load existing index.
    #
    # `existing_id_order` is the file's own row order and is the ONLY thing that
    # may be concatenated with the new ids, because it has to stay positionally
    # paired with `existing_vectors`. The membership set is a separate object.
    # Until 2026-09-09 the merge below did `list(existing_ids) + ids` against
    # `np.vstack([existing_vectors, new_vectors])`, and set iteration order is
    # hash order, not insertion order. With namespaced negative ids in the index
    # the two diverge: 67,723 of 117,101 live vectors carried the wrong id, from
    # position 42,026 on, so every attachment, conversation and Teams vector and
    # every email filed after 2026-03-24 resolved to an unrelated row. Semantic
    # search returned confident, random results. Verified by embedding text that
    # is byte-identical across two rows and getting cosine 0.55 instead of 1.00.
    #
    # Selection reads only the ids. The vectors are loaded at save time, under
    # the index lock, so an append another process made while this one was
    # embedding is merged rather than overwritten (see _index_lock).
    if force:
        existing_ids: set[int] = set()
        space = embedding_space()
    else:
        existing_ids, space = _index_state()
    indexed = None if force else existing_ids

    # Emails embed a metadata-enriched string (subject / sender / date + summary), not the
    # summary alone, so the vector captures who/when/what-subject: a no-extra-LLM variant of
    # contextual retrieval.
    to_embed = _email_items(conn, space, indexed)
    to_embed += _attachment_items(conn, space, indexed)
    to_embed += _conversation_items(conn, space, indexed)

    # The chunks: cut what is new, then embed the chunks the index lacks, newest documents
    # first, until the deadline. A chunk vector no row stands for any more (a document cut
    # again, forgotten, or no longer eligible) leaves the index, and so does one of a
    # document cut again since it was embedded, which points at a passage that moved.
    chunk_pairs: list[tuple[int, str]] = []
    gone_chunks: set[int] = set()
    deadline = None
    if has_chunks(space):
        if chunk_deadline_s is not None:
            deadline = time.monotonic() + chunk_deadline_s
        plan = _plan_chunks(conn, deadline)
        if plan is not None:
            wanted = chunk_ids(plan)
            recut = {plan.content[a] for a in plan.changed}
            held = {v for v in existing_ids if v <= CHUNK_ID_OFFSET}
            gone_chunks = {v for v in held if v not in wanted or chunk_of(v)[0] in recut}
            missing = {v for v in wanted if v not in held or v in gone_chunks}
            aids = sorted(
                (
                    a
                    for a, spans in plan.spans.items()
                    if any(chunk_vector_id(plan.content[a], s[0]) in missing for s in spans)
                ),
                reverse=True,
            )
            for pair in chunk_items(conn, plan, aids, missing):
                chunk_pairs.append(pair)
                if deadline is not None and len(chunk_pairs) >= CHUNK_TEXTS_PER_PASS:
                    break
            if plan.deferred:
                _log(f"{plan.deferred} documents wait for the next pass to be cut into chunks")

    if not to_embed and not chunk_pairs and not gone_chunks:
        _log("All emails and attachments already have embeddings.")
        return 0

    _log(f"Generating embeddings for {len(to_embed)} items and {len(chunk_pairs)} chunks...")

    client = _get_client()
    ids = [t[0] for t in to_embed]
    with in_space(space):
        new_vectors = (
            generate_embeddings([t[1] for t in to_embed], client)
            if to_embed
            else np.empty((0, int(space["dim"])), dtype=np.float32)
        )
        if chunk_pairs:
            chunk_vectors = _embed_until([t[1] for t in chunk_pairs], client, deadline)
            if len(chunk_vectors) < len(chunk_pairs):
                _log(f"{len(chunk_pairs) - len(chunk_vectors)} chunks wait for the next pass")
            if len(chunk_vectors):
                ids += [p[0] for p in chunk_pairs[: len(chunk_vectors)]]
                new_vectors = (
                    np.vstack([new_vectors, chunk_vectors]) if len(new_vectors) else chunk_vectors
                )
    offered = {**space, "dim": _width(new_vectors) or int(space["dim"])}

    with _index_lock():
        # A force rebuild rewrites the file from what this function owns
        # (emails, attachments, conversations, chunks), in this code's space. Teams vectors
        # it drops come back on the next teams-sync, which selects by membership
        # in the file.
        if force or not EMBEDDINGS_FILE.exists():
            all_ids = ids
            all_vectors = _merged(None, new_vectors)
        else:
            # Merge into the file AS IT IS NOW, in file order (never set order;
            # see the comment above). Ids another writer added since the
            # snapshot are kept, and not duplicated. Its space is checked again
            # here: another writer may have rebuilt it since the snapshot.
            data = np.load(EMBEDDINGS_FILE, allow_pickle=False)
            existing_id_order = [int(x) for x in data["ids"]]
            existing_vectors = data["vectors"]
            if len(existing_id_order) != len(existing_vectors):
                raise RuntimeError(
                    f"{EMBEDDINGS_FILE} holds {len(existing_id_order)} ids for "
                    f"{len(existing_vectors)} vectors; refusing to extend a "
                    "misaligned index"
                )
            _merge_space(_npz_space(data), _width(existing_vectors), offered)
            present = set(existing_id_order) - gone_chunks
            fresh = [i for i, vid in enumerate(ids) if vid not in present]
            # Fresh ones too: a conversation loaded again while it was embedded.
            gone_conversations = _conversation_vectors_gone(
                conn, existing_id_order + [ids[i] for i in fresh]
            )
            fresh = [i for i in fresh if ids[i] not in gone_conversations]
            gone = gone_conversations | gone_chunks
            if gone:
                _log(f"Dropping {len(gone)} vectors of items no longer stored or cut again")
                kept = np.array([vid not in gone for vid in existing_id_order], dtype=bool)
                existing_id_order = [vid for vid in existing_id_order if vid not in gone]
                existing_vectors = existing_vectors[kept]
            all_ids = existing_id_order + [ids[i] for i in fresh]
            all_vectors = _merged(existing_vectors, new_vectors[fresh])
            del data, existing_vectors
            gc.collect()

        if len(all_ids) != len(all_vectors):
            raise RuntimeError(
                f"refusing to save a misaligned index: {len(all_ids)} ids for "
                f"{len(all_vectors)} vectors"
            )

        # Save to disk atomically so an interrupted write can never truncate the
        # shared index (see _atomic_savez).
        _atomic_savez(EMBEDDINGS_FILE, all_ids, all_vectors, offered)

    _log(f"Saved {len(all_ids)} embeddings to {EMBEDDINGS_FILE}")
    _record_space(conn, offered)
    return len(ids)


def _conversation_vectors_gone(conn: sqlite3.Connection, ids) -> set[int]:
    """The conversation vectors among `ids` whose conversation the store no longer
    holds. One loaded again gets a new id (load_single_conversation), and the old
    id's vector, matching no row, would take a search result's place beside the
    new one. build_index reads this under the index lock, and is the only writer
    of conversation vectors, so no vector whose row it has not seen can go."""
    try:
        held = {CONVERSATION_ID_OFFSET - r[0] for r in conn.execute("SELECT id FROM conversations")}
        # An attachment past the offset reads as a conversation; its vector stays.
        past = "SELECT id FROM attachment_content WHERE id >= ?"
        held |= {-r[0] for r in conn.execute(past, (-CONVERSATION_ID_OFFSET,))}
    except sqlite3.OperationalError:  # a schema without these tables
        return set()
    return {
        vid
        for vid in ids
        if TEAMS_THREAD_ID_OFFSET < vid <= CONVERSATION_ID_OFFSET and vid not in held
    }


def _kind_mask(ids, kinds) -> np.ndarray:
    """Which vectors are of the given kinds, read off their id namespaces.

    The boundaries are query_semantic's routing below: positive ids are emails,
    CHUNK_ID_OFFSET and below chunks, which count as their attachments,
    WHATSAPP_THREAD_ID_OFFSET and below WhatsApp sessions, TEAMS_THREAD_ID_OFFSET and below
    Teams threads, CONVERSATION_ID_OFFSET and below conversations, the rest of the
    negatives attachments.
    """
    mask = np.zeros(len(ids), dtype=bool)
    if "email" in kinds:
        mask |= ids > 0
    if "attachment" in kinds:
        mask |= ((ids < 0) & (ids > CONVERSATION_ID_OFFSET)) | (ids <= CHUNK_ID_OFFSET)
    if "conversation" in kinds:
        mask |= (ids <= CONVERSATION_ID_OFFSET) & (ids > TEAMS_THREAD_ID_OFFSET)
    if "teams_thread" in kinds:
        mask |= (ids <= TEAMS_THREAD_ID_OFFSET) & (ids > WHATSAPP_THREAD_ID_OFFSET)
    if "whatsapp_thread" in kinds:
        mask |= (ids <= WHATSAPP_THREAD_ID_OFFSET) & (ids > CHUNK_ID_OFFSET)
    return mask


def _first_of_each(order: np.ndarray, parents: np.ndarray, limit: int) -> np.ndarray:
    """The first `limit` entries of `order` (best first) whose item has not come before: a
    document's best chunk or its summary, whichever ranks higher, and only that one."""
    window = max(limit * 4, 64)
    while True:
        head = order[:window]
        _items, first = np.unique(parents[head], return_index=True)
        if len(first) >= limit or window >= len(order):
            return head[np.sort(first)][:limit]
        window *= 4


def _top_indices(
    similarities, ids, limit: int, kinds=None, allowed_ids=None, excluded_ids=None
) -> np.ndarray:
    """The `limit` most similar vectors, best first, among `kinds` (all when None)
    and, when given, among `allowed_ids` and not among `excluded_ids` (index ids,
    as stored in the npz), one per item: the chunks of a document and its summary fold
    onto the best of them.

    The kind is chosen before the top is taken. Taking it over every vector and
    filtering after left a kind with few vectors (conversations, about 1K of
    120K) mostly empty, and cost emails their slots to other kinds. The allowed
    and excluded ids are applied first for the same reason: a workspace filter
    applied after the top came back empty whenever the best matches sat in other
    projects. A chunk is left out with its attachment.
    """
    parents = _parents(ids)
    if kinds is None and allowed_ids is None and excluded_ids is None and parents is ids:
        return np.argsort(similarities)[::-1][:limit]
    mask = np.ones(len(ids), dtype=bool) if kinds is None else _kind_mask(ids, kinds)
    if allowed_ids is not None:
        mask &= np.isin(ids, np.fromiter(allowed_ids, dtype=np.int64))
    if excluded_ids is not None:
        mask &= ~np.isin(parents, excluded_ids)
    candidates = np.flatnonzero(mask)
    order = candidates[np.argsort(similarities[candidates])[::-1]]
    if parents is ids:
        return order[:limit]
    return _first_of_each(order, parents, limit)


def _hidden_vector_ids(
    conn: sqlite3.Connection,
    include_news: bool = False,
    include_automation: bool = False,
    classes: tuple[str, ...] | None = None,
) -> np.ndarray | None:
    """The index ids of the emails a read path leaves out (src/store/source_class.py) and of
    their attachments; None when it leaves out none. 16,320 News vectors stood among the
    semantic candidates, and the owner's automation mail beside them. `classes`, a search's
    `kinds`, leaves out the emails of the other classes too, but not their attachments,
    which are a kind of their own. An attachment's chunks leave with it (_top_indices)."""
    hidden = hidden_ids_sql(conn, include_news, include_automation)
    narrowed = (
        hidden
        if classes is None
        else hidden_ids_sql(conn, include_news, include_automation, classes)
    )
    if narrowed is None:
        return None
    emails = [row[0] for row in conn.execute(narrowed)]
    attachments = (
        [
            -row[0]
            for row in conn.execute(
                "SELECT ac.id FROM attachment_content ac JOIN attachments a"
                f" ON a.id = ac.attachment_id WHERE a.email_id IN ({hidden})"
            )
        ]
        if hidden is not None
        else []
    )
    return np.array(emails + attachments, dtype=np.int64)


# The index outlives the embedding model. Since 2026-10-06 an organisation
# policy on the Vertex project refuses gemini-embedding-001 while every stored
# vector is still good, so a query that cannot be embedded stands instead at
# the centroid of its best keyword matches' vectors.
KEYWORD_SEED_LIMIT = 10


class SemanticUnavailable(RuntimeError):
    """The query could not be embedded and no keyword match could stand in."""


def _keyword_seed_vector(conn: sqlite3.Connection, query: str, ids, unit) -> np.ndarray | None:
    """Centroid of the indexed vectors of the query's top keyword matches, or None."""
    from src.store.query import query_by_keyword

    hits = query_by_keyword(conn, query, limit=KEYWORD_SEED_LIMIT)
    wanted = [int(h["email_id"]) for h in hits if h.get("email_id") is not None]
    rows = np.flatnonzero(np.isin(ids, wanted))
    if rows.size == 0:
        return None
    return unit[rows].astype(np.float32).mean(axis=0)


def _require_query_space(space: dict | None) -> None:
    """Raise IndexSpaceMismatch unless this code can embed queries into the index's space.

    A query is embedded as typed, or under a prompted model with the space's task as its
    prompt, so the model and the task decide where it lands; the template is how the
    documents' texts were built and moves no query. Any model this code calls will do. An
    index saved before the space was recorded is in the legacy space.
    """
    if space is None:
        return
    model = space.get("model")
    if model not in (EMBEDDING_MODEL, LEGACY_MODEL):
        raise IndexSpaceMismatch(
            f"the index is in another embedding space than this code embeds queries into: "
            f"model {model!r} (this code {EMBEDDING_MODEL!r} or {LEGACY_MODEL!r}); "
            "rebuild it with `python -m src.cli maint vectors-rebuild`"
        )
    if model in PROMPTED_MODELS and space.get("task") != EMBEDDING_TASK:
        raise IndexSpaceMismatch(
            f"the index is in another embedding space than this code embeds queries into: "
            f"task {space.get('task')!r} (this code {EMBEDDING_TASK!r}); rebuild it with "
            "`python -m src.cli maint vectors-rebuild`"
        )


def _query_vector(
    conn: sqlite3.Connection, query: str, ids, unit, embed, space: dict | None = None
) -> tuple[np.ndarray, str | None]:
    """The query's unit vector, and how it was made: None, or 'keyword_seeded: <error type>'.

    The query is embedded into the index's space (`space`, what the index records; the
    legacy space at the vectors' width for an index that records none). When the model
    cannot embed it, or this code cannot embed into that space, the vector is the centroid
    of its best keyword matches, whose vectors are the index's own. Raises the error when no
    keyword match can stand in.
    """
    try:
        _require_query_space(space)
        with in_space(space or legacy_space(_width(unit))):
            vector = np.asarray(embed([query])[0], dtype=np.float32)
        if vector.shape != (_width(unit),):
            raise IndexSpaceMismatch(
                f"the query's vector has dim {vector.size} and the index's vectors "
                f"dim {_width(unit)}"
            )
        seeded = None
    except Exception as e:  # the model is unreachable, or in another space; the index is not
        if isinstance(e, IndexSpaceMismatch):
            print(
                f"semantic search: {e}. Ranking around the keyword matches' vectors instead.",
                file=sys.stderr,
            )
        seed = _keyword_seed_vector(conn, query, ids, unit)
        if seed is None:
            raise
        vector, seeded = seed, f"keyword_seeded: {type(e).__name__}"
    return vector / (np.linalg.norm(vector) or 1), seeded


class SemanticCandidates(list):
    """Ranked email ids, with `semantic` saying how the query vector was made:
    'ok', or 'keyword_seeded: <error type>' when the model could not embed it.

    `by_kind` ranks the other kinds by the same vector, best first, under recall's
    names for them: 'attachment' (attachments.id), 'teams' and 'whatsapp' (thread ids)
    and 'conversation' (conversations.id, the owner's sessions only). `chunks` says, for
    each attachment there whose best match was a passage rather than its summary, which
    one: {attachments.id: chunk_fields(...)}."""

    def __init__(
        self,
        ids=(),
        semantic: str = "ok",
        by_kind: dict[str, list[int]] | None = None,
        chunks: dict[int, dict] | None = None,
    ):
        super().__init__(ids)
        self.semantic = semantic
        self.by_kind = by_kind or {}
        self.chunks = chunks or {}


def _chunk_spans(conn: sqlite3.Connection, pairs) -> dict[tuple[int, int], tuple[int, int, int]]:
    """(content id, chunk_no) -> (attachment id, start, end) of the chunks `pairs` names."""
    pairs = list(pairs)
    if not pairs:
        return {}
    try:
        rows = conn.execute(
            "SELECT content_id, chunk_no, attachment_id, start_char, end_char "
            "FROM attachment_chunks WHERE (content_id, chunk_no) IN "
            "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
            "FROM json_each(?))",
            (json.dumps([list(p) for p in pairs]),),
        ).fetchall()
    except sqlite3.OperationalError:  # a store before v39
        return {}
    return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}


def chunk_fields(chunk_no: int, start: int, end: int) -> dict:
    """What a row a passage matched says: matched_in 'chunk', which chunk, and where it lies
    in the attachment's text (get_item(kind='attachment', ref, offset=chunk_offset))."""
    return {"matched_in": "chunk", "chunk_no": chunk_no, "chunk_offset": start, "chunk_end": end}


def query_semantic(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    kinds=None,
    embed_fn=None,
    index_path=None,
    allowed_ids=None,
    *,
    include_news: bool = False,
    include_automation: bool = False,
    classes: tuple[str, ...] | None = None,
) -> list[dict]:
    """Semantic search across email summaries using cosine similarity.

    Args:
        conn: Database connection
        query: Natural language search query
        limit: Maximum number of results
        kinds: Only these kinds ("email", "attachment", "conversation",
            "teams_thread", "whatsapp_thread"); every kind when None. An attachment's
            chunks are of its kind
        embed_fn, index_path: injection points for testing
        allowed_ids: Only these index ids (for a conversation,
            CONVERSATION_ID_OFFSET - conversations.id), ranked among
            themselves; every id when None
        include_news, include_automation: Include the vectors of news items and
            of the owner's automation mail, and of their attachments, left out by
            default (src/store/source_class.py)
        classes: Only the email vectors of these source classes (a search's
            `kinds`); attachments are left to `kinds`

    Returns:
        List of dicts with keys: email_id, date, subject, summary, similarity;
        email and attachment rows carry their email's source_class, and an
        attachment row its attachment_id. One row per attachment: when a passage of it
        ranked higher than its summary, the row says matched_in 'chunk' with chunk_no,
        chunk_offset and chunk_end
    """
    # Index load + unit-normalization is cached across calls (see _load_index_and_space),
    # so a long-lived process pays the read once, not per query.
    ids, normalized, space = _load_index_and_space(index_path)

    try:
        query_norm, seeded = _query_vector(
            conn, query, ids, normalized, embed_fn or embed_query, space
        )
    except Exception as e:
        raise SemanticUnavailable(
            "semantic search unavailable: the query could not be embedded "
            f"({type(e).__name__}: {str(e)[:300]}) and no keyword match could "
            "stand in for it; search_type='keyword' still works"
        ) from e

    similarities = _similarities(normalized, query_norm)

    # News and automation vectors, and their attachments', leave the ranking first.
    excluded = (
        _hidden_vector_ids(conn, include_news, include_automation, classes)
        if kinds is None or {"email", "attachment"} & set(kinds)
        else None
    )
    top_indices = _top_indices(similarities, ids, limit, kinds, allowed_ids, excluded)
    spans = _chunk_spans(
        conn, (chunk_of(int(ids[i])) for i in top_indices if int(ids[i]) <= CHUNK_ID_OFFSET)
    )

    # Fetch details (positive IDs = emails, negative IDs = attachments)
    results = []
    for idx in top_indices:
        item_id = int(ids[idx])
        similarity = float(similarities[idx])

        if item_id > 0:
            # Email
            row = conn.execute(
                """
                SELECT id as email_id, date_received as date, subject, summary
                FROM emails WHERE id = ?
            """,
                (item_id,),
            ).fetchone()

            if row:
                result = dict(row)
                result["type"] = "email"
                result["similarity"] = round(similarity, 4)
                results.append(result)
        elif item_id <= CHUNK_ID_OFFSET:
            # A passage of an attachment, shown as its attachment
            content_id, chunk_no = chunk_of(item_id)
            result = _attachment_result(conn, content_id, similarity)
            span = spans.get((content_id, chunk_no))
            if result is not None:
                if span is not None:
                    result.update(chunk_fields(chunk_no, span[1], span[2]))
                results.append(result)
        elif item_id <= WHATSAPP_THREAD_ID_OFFSET:
            # WhatsApp session (npz id encoding: WHATSAPP_THREAD_ID_OFFSET - thread.id)
            try:
                row = conn.execute(
                    """
                    SELECT t.id, t.title, t.summary, t.started_at as date,
                           t.message_count, c.name AS chat_name
                    FROM whatsapp_threads t JOIN whatsapp_chats c ON c.id = t.chat_id
                    WHERE t.id = ?
                    """,
                    (WHATSAPP_THREAD_ID_OFFSET - item_id,),
                ).fetchone()
                if row:
                    result = dict(row)
                    result["type"] = "whatsapp_thread"
                    result["similarity"] = round(similarity, 4)
                    results.append(result)
            except sqlite3.OperationalError:
                pass  # a store without the WhatsApp tables
        elif item_id <= TEAMS_THREAD_ID_OFFSET:
            # Teams thread (npz id encoding: TEAMS_THREAD_ID_OFFSET - thread.id)
            thread_id = TEAMS_THREAD_ID_OFFSET - item_id
            try:
                row = conn.execute(
                    """
                    SELECT t.id, t.title, t.summary, t.started_at as date,
                           t.message_count, c.team_name, c.topic AS chat_topic
                    FROM teams_threads t JOIN teams_chats c ON c.id = t.chat_id
                    WHERE t.id = ?
                    """,
                    (thread_id,),
                ).fetchone()
                if row:
                    result = dict(row)
                    result["type"] = "teams_thread"
                    result["similarity"] = round(similarity, 4)
                    results.append(result)
            except Exception:
                pass
        elif item_id <= CONVERSATION_ID_OFFSET:
            # Conversation (offset namespace)
            conv_id = -(item_id - CONVERSATION_ID_OFFSET)
            try:
                row = conn.execute(
                    """
                    SELECT id, session_id, started_at as date,
                           project_name, summary, turn_count
                    FROM conversations WHERE id = ?
                """,
                    (conv_id,),
                ).fetchone()

                if row:
                    result = dict(row)
                    result["type"] = "conversation"
                    result["similarity"] = round(similarity, 4)
                    results.append(result)
            except Exception:
                pass
        else:
            # Attachment (negative ID = attachment_content.id)
            result = _attachment_result(conn, -item_id, similarity)
            if result is not None:
                results.append(result)

    if seeded:
        for result in results:
            result["semantic"] = seeded
    with_source_class(conn, [r for r in results if r["type"] in ("email", "attachment")])
    return results


def _attachment_result(conn: sqlite3.Connection, content_id: int, similarity: float):
    """An attachment as a semantic row, by its attachment_content id; None when it is gone."""
    row = conn.execute(
        """
        SELECT ac.id as attachment_content_id, a.id as attachment_id,
               a.filename, ac.summary, a.email_id, e.subject as email_subject,
               e.date_received as date
        FROM attachment_content ac
        JOIN attachments a ON a.id = ac.attachment_id
        LEFT JOIN emails e ON e.id = a.email_id
        WHERE ac.id = ?
    """,
        (content_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["type"] = "attachment"
    result["similarity"] = round(similarity, 4)
    return result


def _vector_id_to_email_id(conn: sqlite3.Connection, item_id: int) -> int | None:
    """Map a namespaced embedding id to a parent email id (or None if not email-backed).

    Positive ids are email ids; the attachment namespace (-1 .. CONVERSATION_ID_OFFSET+1)
    and a chunk (below CHUNK_ID_OFFSET) map via attachment_content -> attachments.email_id;
    conversation, Teams and WhatsApp namespaces are not emails and return None.
    """
    if item_id > 0:
        return item_id
    if item_id <= CHUNK_ID_OFFSET:
        item_id = -chunk_of(item_id)[0]
    elif item_id <= CONVERSATION_ID_OFFSET:
        return None  # conversation or teams thread — not an email
    row = conn.execute(
        """
        SELECT a.email_id
        FROM attachment_content ac
        JOIN attachments a ON a.id = ac.attachment_id
        WHERE ac.id = ?
        """,
        (-item_id,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def semantic_email_candidates(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    embed_fn=None,
    index_path=None,
    *,
    include_news: bool = False,
    include_automation: bool = False,
    classes: tuple[str, ...] | None = None,
) -> list[int]:
    """Ranked, de-duplicated email ids for `query` from the vector index, best-first.

    Attachment vectors, and their chunks', are folded onto their parent email so the
    semantic signal fuses with keyword search at email granularity. Used by recall()'s
    hybrid path; `embed_fn` / `index_path` are injection points for testing. The ids come
    back as SemanticCandidates, whose `semantic` says whether the query was embedded or
    stood in for by its keyword matches. News items and the owner's automation mail,
    and their attachments, are left out unless included (src/store/source_class.py),
    and with `classes` (recall's `kinds`) the emails of the other classes; recall drops
    a candidate an attachment folds onto one of those.
    """
    ids, normalized, space = _load_index_and_space(index_path)
    query_norm, seeded = _query_vector(conn, query, ids, normalized, embed_fn or embed_query, space)
    similarities = _similarities(normalized, query_norm)
    hidden = _hidden_vector_ids(conn, include_news, include_automation, classes)
    # Over-fetch: several attachment vectors can collapse onto one email, so we
    # need headroom to fill `limit` emails. Other kinds, and the classes a read
    # leaves out, are left out first.
    order = _top_indices(
        similarities, ids, max(limit * 4, limit), {"email", "attachment"}, excluded_ids=hidden
    )
    out: list[int] = []
    seen: set[int] = set()
    for idx in order:
        email_id = _vector_id_to_email_id(conn, int(ids[idx]))
        if email_id is None or email_id in seen:
            continue
        seen.add(email_id)
        out.append(email_id)
        if len(out) >= limit:
            break
    by_kind, chunks = _other_kinds(conn, similarities, ids, limit, hidden)
    return SemanticCandidates(out, semantic=seeded or "ok", by_kind=by_kind, chunks=chunks)


def _other_kinds(
    conn: sqlite3.Connection, similarities, ids, limit: int, hidden
) -> tuple[dict[str, list[int]], dict[int, dict]]:
    """The `limit` best attachments, Teams threads, WhatsApp sessions and owner
    conversations by the similarities already computed, as their tables' ids; and for each
    attachment whose best match was a passage, which one (SemanticCandidates.chunks).

    recall's other buckets fuse these with their keyword hits: the vectors were in the
    index all along, and only the email bucket used them. The attachments of hidden
    email (news, automation) leave as `hidden` says; a conversation must be one of the
    owner's sessions, as recall's keyword bucket reads them (conversation_query)."""
    found: dict[str, list[int]] = {}
    chunks: dict[int, dict] = {}
    best = [
        int(ids[i])
        for i in _top_indices(similarities, ids, limit, {"attachment"}, excluded_ids=hidden)
    ]
    texts = [chunk_of(v)[0] if v <= CHUNK_ID_OFFSET else -v for v in best]
    if texts:
        attachment_of = dict(
            conn.execute(
                "SELECT id, attachment_id FROM attachment_content"
                " WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(texts),),
            ).fetchall()
        )
        found["attachment"] = list(
            dict.fromkeys(attachment_of[t] for t in texts if t in attachment_of)
        )
        spans = _chunk_spans(conn, (chunk_of(v) for v in best if v <= CHUNK_ID_OFFSET))
        for (_content_id, chunk_no), (attachment_id, start, end) in spans.items():
            chunks[attachment_id] = chunk_fields(chunk_no, start, end)
    for kind, vector_kind, offset in (
        ("teams", "teams_thread", TEAMS_THREAD_ID_OFFSET),
        ("whatsapp", "whatsapp_thread", WHATSAPP_THREAD_ID_OFFSET),
    ):
        found[kind] = [
            offset - int(ids[i]) for i in _top_indices(similarities, ids, limit, {vector_kind})
        ]
    try:
        from src.store.conversation_query import conversation_ids_in_workspace

        owner = conversation_ids_in_workspace(conn, "")
    except sqlite3.OperationalError:  # a store without conversations
        owner = set()
    if owner:
        allowed = {CONVERSATION_ID_OFFSET - cid for cid in owner}
        found["conversation"] = [
            CONVERSATION_ID_OFFSET - int(ids[i])
            for i in _top_indices(similarities, ids, limit, {"conversation"}, allowed)
        ]
    return found, chunks


def _append_to_index(ids: list[int], vectors, space: dict | None = None) -> dict:
    """Append (id, vector) rows to EMBEDDINGS_FILE; returns the space saved.

    EMBEDDINGS_FILE format: npz with arrays 'ids' (int64) and 'vectors' (float16 or float32),
    and the space they are in (SPACE_MEMBER). Re-saves the merged arrays atomically, in the
    file's dtype. Existing ids are replaced (so re-embedding the same teams_thread overwrites
    its prior vector instead of duplicating it). Vectors of another space than the file's are
    refused. Load, merge and save all happen under the index lock (see _index_lock).
    `space` is the space the vectors were made in (the index's own when None), at their width.
    """
    vectors = np.asarray(vectors)
    space = {**(space or _index_state()[1]), "dim": _width(vectors)}
    with _index_lock():
        if EMBEDDINGS_FILE.exists():
            existing = np.load(EMBEDDINGS_FILE, allow_pickle=False)
            old_ids = existing["ids"]
            old_vecs = existing["vectors"]
            _merge_space(_npz_space(existing), _width(old_vecs), space)
            # Drop any rows whose id matches one we're inserting (replace semantics).
            keep = ~np.isin(old_ids, np.array(ids, dtype=np.int64))
            merged_ids = np.concatenate([old_ids[keep], np.array(ids, dtype=np.int64)])
            merged_vecs = _merged(old_vecs[keep], vectors)
            # Release the NpzFile-backed arrays before we allocate the tmp save buffer.
            del existing, old_ids, old_vecs
            gc.collect()
        else:
            merged_ids = np.array(ids, dtype=np.int64)
            merged_vecs = _merged(None, vectors)
        _atomic_savez(EMBEDDINGS_FILE, merged_ids, merged_vecs, space)
    return space


def _teams_rows(conn: sqlite3.Connection, space: dict) -> list[tuple[int, str]]:
    """(teams_threads.id, text) of every extracted thread with a summary, in `space`."""
    rows = conn.execute(
        """
        SELECT t.id, t.summary, COALESCE(t.title, c.topic, c.team_name)
        FROM teams_threads t LEFT JOIN teams_chats c ON c.id = t.chat_id
        WHERE t.extraction_status = 'extracted' AND COALESCE(t.summary, '') != ''
        ORDER BY t.id
        """
    ).fetchall()
    return [(int(r[0]), teams_text(space, r[2], r[1])) for r in rows]


def _teams_threads_to_embed(
    conn, indexed: set[int] | None = None, space: dict | None = None
) -> list[tuple[int, str]]:
    """Return [(teams_threads.id, text), ...] for threads needing fresh embeddings.

    A thread needs (re-)embedding when extraction_status='extracted' AND either
    embedding_at is missing or older than extracted_at, OR its vector is not in
    the index. The second test is what emails already had: embedding_at alone
    said "done" for 5,336 threads whose vectors a force rebuild and a lost update
    had removed, so they were never retried. ``indexed`` defaults to the ids in
    EMBEDDINGS_FILE, ``space`` to the space a writer adds vectors in (_index_state).
    """
    if indexed is None or space is None:
        held, writer = _index_state()
        indexed = held if indexed is None else indexed
        space = writer if space is None else space
    texts = dict(_teams_rows(conn, space))
    rows = conn.execute(
        """
        SELECT id, embedding_at, extracted_at
        FROM teams_threads
        WHERE extraction_status = 'extracted'
          AND COALESCE(summary, '') != ''
        ORDER BY id
        """
    ).fetchall()
    stale_rows = []
    repair_rows = []
    for rid, embedding_at, extracted_at in rows:
        stale = embedding_at is None or (extracted_at is not None and embedding_at < extracted_at)
        if stale:
            stale_rows.append((int(rid), texts[int(rid)]))
        elif (TEAMS_THREAD_ID_OFFSET - int(rid)) not in indexed:
            repair_rows.append((int(rid), texts[int(rid)]))
    # New and re-extracted threads first, then the repair backlog newest first,
    # so a per-run cap never parks this week's threads behind thousands of old
    # ones whose vectors went missing.
    return sorted(stale_rows, key=lambda p: -p[0]) + sorted(repair_rows, key=lambda p: -p[0])


def build_teams_index(conn, force: bool = False, limit: int = 0) -> int:
    """Embed the teams_threads that need it, at most ``limit`` (0 = all), in the index's space.

    Returns count generated. Stores into the same EMBEDDINGS_FILE keyed by
    TEAMS_THREAD_ID_OFFSET-id (mirrors the conversation encoding shape), so all
    teams npz keys are strictly less than TEAMS_THREAD_ID_OFFSET (-10M). Lets
    query_semantic distinguish teams from conversations purely by id sign+threshold.
    Adds nothing while a vector rebuild runs.
    """
    if _skip_for_rebuild("teams-sync"):
        return 0
    indexed, space = _index_state()
    if force:
        # Force rebuild = clear embedding_at so the query re-selects everything.
        conn.execute("UPDATE teams_threads SET embedding_at = NULL")
        conn.commit()

    pairs = _teams_threads_to_embed(conn, indexed, space)
    if limit and limit > 0:
        pairs = pairs[:limit]
    if not pairs:
        return 0

    ids = [p[0] for p in pairs]
    texts = [p[1] for p in pairs]
    with in_space(space):
        vecs = generate_embeddings(texts)

    # Persist to embeddings.npz alongside email/conversation entries.
    # Encoding mirrors CONVERSATION_ID_OFFSET - id (decreasing-with-id) so the
    # query_semantic guard `item_id <= TEAMS_THREAD_ID_OFFSET` actually fires.
    # The earlier i+OFFSET form silently fell into the conversation routing
    # branch and degraded semantic search. Validation gate caught it.
    saved = _append_to_index(
        ids=[TEAMS_THREAD_ID_OFFSET - i for i in ids],
        vectors=vecs,
        space=space,
    )

    now = datetime.now(UTC).isoformat()
    for tid in ids:
        conn.execute(
            "UPDATE teams_threads SET embedding_at = ? WHERE id = ?",
            (now, tid),
        )
    conn.commit()
    _record_space(conn, saved)
    return len(ids)


def _whatsapp_rows(conn: sqlite3.Connection, space: dict) -> list[tuple[int, str]]:
    """(whatsapp_threads.id, text) of every extracted session with a summary, in `space`."""
    try:
        rows = conn.execute(
            """
            SELECT t.id, t.summary, c.name
            FROM whatsapp_threads t JOIN whatsapp_chats c ON c.id = t.chat_id
            WHERE t.extraction_status = 'extracted' AND COALESCE(t.summary, '') != ''
            ORDER BY t.id
            """
        ).fetchall()
    except sqlite3.OperationalError:  # a store without the WhatsApp tables
        return []
    return [(int(r[0]), whatsapp_text(space, r[2], r[1])) for r in rows]


def _whatsapp_threads_to_embed(
    conn, indexed: set[int] | None = None, space: dict | None = None
) -> list[tuple[int, str]]:
    """[(whatsapp_threads.id, text)] for sessions needing a (fresh) vector.

    The Teams rule (see _teams_threads_to_embed): extracted, and either
    re-extracted since it was embedded or missing from the index. Membership is
    what brings a session back after a force rebuild dropped it.
    """
    if indexed is None or space is None:
        held, writer = _index_state()
        indexed = held if indexed is None else indexed
        space = writer if space is None else space
    texts = dict(_whatsapp_rows(conn, space))
    try:
        rows = conn.execute(
            """
            SELECT id, embedding_at, extracted_at FROM whatsapp_threads
            WHERE extraction_status = 'extracted' AND COALESCE(summary, '') != ''
            ORDER BY id
            """
        ).fetchall()
    except sqlite3.OperationalError:  # a store without the WhatsApp tables
        return []
    stale, repair = [], []
    for rid, embedding_at, extracted_at in rows:
        text = texts.get(int(rid))
        if text is None:
            continue  # a session whose chat row is gone
        if embedding_at is None or (extracted_at is not None and embedding_at < extracted_at):
            stale.append((int(rid), text))
        elif (WHATSAPP_THREAD_ID_OFFSET - int(rid)) not in indexed:
            repair.append((int(rid), text))
    return sorted(stale, key=lambda p: -p[0]) + sorted(repair, key=lambda p: -p[0])


def build_whatsapp_index(conn, force: bool = False, limit: int = 0) -> int:
    """Embed the WhatsApp sessions that need it, at most ``limit`` (0 = all), in the index's
    space.

    Stored in EMBEDDINGS_FILE at WHATSAPP_THREAD_ID_OFFSET - id, so every
    WhatsApp key is at or below -1,000,000,001 and query_semantic routes it by
    threshold alone. Returns the number embedded. Adds nothing while a vector rebuild runs.
    """
    if _skip_for_rebuild("whatsapp-sync"):
        return 0
    indexed, space = _index_state()
    if force:
        conn.execute("UPDATE whatsapp_threads SET embedding_at = NULL")
        conn.commit()
    pairs = _whatsapp_threads_to_embed(conn, indexed, space)
    if limit and limit > 0:
        pairs = pairs[:limit]
    if not pairs:
        return 0
    with in_space(space):
        vecs = generate_embeddings([p[1] for p in pairs])
    saved = _append_to_index(
        ids=[WHATSAPP_THREAD_ID_OFFSET - p[0] for p in pairs], vectors=vecs, space=space
    )
    now = datetime.now(UTC).isoformat()
    for tid, _text in pairs:
        conn.execute("UPDATE whatsapp_threads SET embedding_at = ? WHERE id = ?", (now, tid))
    conn.commit()
    _record_space(conn, saved)
    return len(pairs)


def remove_vectors(ids) -> int:
    """Drop these vector ids from the index, atomically, under the index lock, and with an
    attachment's summary vector its chunks'.

    For rows the store has forgotten (src/store/forget.py), and summaries cleared to be made
    again (reextract, ocr-regarble). build_index only ever adds email and attachment
    vectors, so a forgotten document's vector would otherwise keep answering searches for a
    row that no longer exists. Returns how many were removed. A missing index is not an
    error.
    """
    drop = {int(i) for i in ids}
    if not drop:
        return 0
    removed = 0
    with _index_lock():
        if not EMBEDDINGS_FILE.exists():
            return 0
        data = np.load(EMBEDDINGS_FILE, allow_pickle=False)
        stored = data["ids"]
        gone = np.isin(stored, np.fromiter(drop, dtype=np.int64)) | (
            (stored <= CHUNK_ID_OFFSET)
            & np.isin(_parents(stored), np.fromiter(drop, dtype=np.int64))
        )
        if not gone.any():
            # Nothing of these is indexed (a forgotten row that never had a summary): the
            # vectors, 1.78 GB on the producer, are never read.
            return 0
        vectors = data["vectors"]
        if len(stored) != len(vectors):
            raise RuntimeError(
                f"{EMBEDDINGS_FILE} holds {len(stored)} ids for {len(vectors)} vectors; "
                "refusing to rewrite a misaligned index"
            )
        removed = int(gone.sum())
        # Removing adds no vector, so the file keeps the space it records, whatever it is;
        # one saved before the space was recorded is the legacy space at its width.
        space = _npz_space(data) or legacy_space(_width(vectors))
        _atomic_savez(EMBEDDINGS_FILE, stored[~gone], vectors[~gone], space)
        del data, vectors
        gc.collect()
    if removed:
        _log(f"Removed {removed} vectors of forgotten rows")
    return removed


# --- installing a whole index: the rebuild's swap and the 768-dimension slice ------------


def backup_path() -> Path:
    """Where install_index keeps the index it replaced, for a rollback."""
    return EMBEDDINGS_FILE.with_name(EMBEDDINGS_FILE.name + ".pre-rebuild")


def install_index(conn: sqlite3.Connection, ids, vectors, space: dict) -> Path | None:
    """Replace the index with these vectors in `space`, atomically, under the index lock.

    The index it replaces is kept beside it (backup_path), hard-linked when the file system
    allows, so a reader never meets a missing file and the old index is one rename away.
    Returns the backup's path, None when there was no index. The space is recorded in the
    store too.
    """
    vectors = np.asarray(vectors)
    if len(ids) != len(vectors):
        raise RuntimeError(f"refusing to install {len(ids)} ids for {len(vectors)} vectors")
    backup = None
    with _index_lock():
        if EMBEDDINGS_FILE.exists():
            backup = backup_path()
            backup.unlink(missing_ok=True)
            try:
                os.link(EMBEDDINGS_FILE, backup)
            except OSError:
                shutil.copy2(EMBEDDINGS_FILE, backup)
        _atomic_savez(EMBEDDINGS_FILE, ids, vectors, space)
    _record_space(conn, space)
    _log(f"Installed {len(ids)} vectors in {space} at {EMBEDDINGS_FILE}")
    return backup


def _vector_blocks(path: Path, rows: int) -> Iterator[np.ndarray]:
    """The index's vectors, `rows` at a time, streamed from the archive: slicing a 1.78 GB
    index holds one block of it, not the whole."""
    with zipfile.ZipFile(path) as archive, archive.open("vectors.npy") as member:
        shape, dtype = _header(member)
        width = shape[1]
        left = shape[0]
        while left:
            take = min(rows, left)
            want = take * width * dtype.itemsize
            raw = bytearray()
            while len(raw) < want:
                piece = member.read(want - len(raw))
                if not piece:
                    raise RuntimeError(f"{path} ends {left} vectors early")
                raw += piece
            yield np.frombuffer(bytes(raw), dtype=dtype).reshape(take, width)
            left -= take


def slice_index(conn: sqlite3.Connection, dim: int = EMBEDDING_DIM) -> dict:
    """Cut the index's vectors to their first `dim` dimensions, scale each to unit length and
    save them in half precision: no embedding call and no spend.

    Matryoshka training makes the first dimensions of a 3072-wide vector a vector of their
    own. Measured 2026-10-16 on 20 real texts: gemini-embedding-001 asked for 768
    dimensions returns, scaled to unit length, the first 768 of its 3072 at cosine at least
    0.9999999, for fresh vectors and for the stored ones. So the legacy index sliced to 768
    takes queries embedded at 768 as if it had been built so. Returns the new space.
    """
    space = file_space()
    if space is None:
        raise FileNotFoundError(f"no index at {EMBEDDINGS_FILE} to slice")
    if space["model"] not in (EMBEDDING_MODEL, LEGACY_MODEL) or int(space["dim"]) <= dim:
        raise ValueError(f"cannot slice an index in {space} to {dim} dimensions")
    with zipfile.ZipFile(EMBEDDINGS_FILE) as archive, archive.open("ids.npy") as member:
        ids = np.lib.format.read_array(member, allow_pickle=False)
    out = np.empty((len(ids), dim), dtype=STORE_DTYPE)
    done = 0
    for block in _vector_blocks(EMBEDDINGS_FILE, SCAN_BLOCK):
        part = block[:, :dim].astype(np.float32)
        norms = np.linalg.norm(part, axis=1, keepdims=True)
        norms[norms == 0] = 1
        out[done : done + len(part)] = part / norms
        done += len(part)
    if done != len(ids):
        raise RuntimeError(f"{EMBEDDINGS_FILE} holds {len(ids)} ids for {done} vectors")
    sliced = {**space, "dim": int(dim)}
    install_index(conn, ids, out, sliced)
    return sliced
