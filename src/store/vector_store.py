"""The vectors table (issue 171): the gated migration `vectors`.

Before the step, the vector index is embeddings.npz beside the store: every writer loads it
whole, merges and saves it again (1.7 GB at float32, 380 MB at float16), and every process
that searches loads its own copy. The step moves it into brain.db, one row per vector:

- `id` is the index id, in the namespaces embeddings.py gives them (emails positive,
  attachments negative, conversations, Teams, WhatsApp and chunks below their offsets);
- `kind` and `parent_id` say what the vector stands for: a chunk's parent is its
  attachment's summary vector, every other vector is its own parent (`email_chunk`, a
  passage of an email's body, is the kind the next part of issue 171 writes);
- `vec` is the vector in int8, one byte a dimension, scaled to its own largest component
  (quantise): the cosine of a vector with its int8 copy is above 0.9997 (all 247,833 of
  the owner's, measured 2026-11-04);
- `dim` and `template` are the dimension and the version of the text it was made from, so a
  later re-embed (MRL 1536 for summaries, contextual chunk headers) can add rows of its own
  beside the old ones and replace them a batch at a time.

The model and task are the store's, recorded as before in sync_metadata (embedding_space).
Every write bumps `vectors_version` in sync_metadata in the same transaction, so a process
that holds the table in memory reloads it when another one wrote (load).

The step copies the npz in and leaves the file as it was, so going back is dropping the
table and the gated_migrations row. A store without the step keeps the npz, read and written
exactly as before. create_database does not take this step on a new store: unlike the other
gated steps it moves data that lives outside brain.db, so it is the owner's alone.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from src.store.schema import applied_gated

NAME = "vectors"
VERSION_KEY = "vectors_version"
# Rows written per executemany, and read per block when the table is loaded.
WRITE_BLOCK = 5_000

TABLE = """CREATE TABLE IF NOT EXISTS vectors (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('email', 'attachment', 'chunk', 'email_chunk',
        'conversation', 'teams_thread', 'whatsapp_thread')),
    parent_id INTEGER NOT NULL,
    dim INTEGER NOT NULL,
    template INTEGER NOT NULL,
    vec BLOB NOT NULL,
    written_at INTEGER NOT NULL
)"""
INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_vectors_parent ON vectors(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_vectors_kind ON vectors(kind)",
)


def enabled(conn: sqlite3.Connection) -> bool:
    """Whether this store has taken the step: its vectors live in the table."""
    return NAME in applied_gated(conn)


def _now() -> int:
    """Unix seconds: an integer keeps a row at five to a 4 KB page, where an ISO stamp made it
    four (measured on the owner's 247,833 vectors)."""
    return int(datetime.now(UTC).timestamp())


def kind_of(vector_id: int) -> str:
    """The kind a vector id's namespace names (embeddings._kind_mask)."""
    from src.store import embeddings as emb

    v = int(vector_id)
    if v > 0:
        return "email"
    if v <= emb.EMAIL_CHUNK_ID_OFFSET:
        return "email_chunk"
    if v <= emb.CHUNK_ID_OFFSET:
        return "chunk"
    if v <= emb.WHATSAPP_THREAD_ID_OFFSET:
        return "whatsapp_thread"
    if v <= emb.TEAMS_THREAD_ID_OFFSET:
        return "teams_thread"
    if v <= emb.CONVERSATION_ID_OFFSET:
        return "conversation"
    return "attachment"


def parent_of(vector_id: int) -> int:
    """A chunk's attachment summary id; any other vector's own id."""
    from src.store import embeddings as emb

    v = int(vector_id)
    if v <= emb.EMAIL_CHUNK_ID_OFFSET:
        return emb.email_chunk_of(v)[0]
    if v <= emb.CHUNK_ID_OFFSET:
        return -emb.chunk_of(v)[0]
    return v


def quantise(vectors) -> np.ndarray:
    """Each row scaled to unit length, then to its largest component as 127, rounded to int8.

    A cosine needs the direction alone, so no scale is kept: the reader divides by each
    row's own norm (load)."""
    rows = np.asarray(vectors, dtype=np.float32)
    if rows.ndim != 2:
        raise ValueError(f"vectors must be 2-D, got shape {rows.shape}")
    peak = np.abs(rows).max(axis=1, keepdims=True)
    peak[peak == 0] = 1
    return np.clip(np.rint(rows / peak * 127.0), -127, 127).astype(np.int8)


def _bump(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS sync_metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE"
        " SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
        (VERSION_KEY,),
    )


def write(conn: sqlite3.Connection, ids, vectors, template: int) -> int:
    """Insert or replace these (id, vector) rows; the number written. Not committed."""
    if not len(ids):
        return 0
    return _write_int8(conn, ids, quantise(vectors), template)


def _write_int8(conn: sqlite3.Connection, ids, q: np.ndarray, template: int) -> int:
    """Insert or replace rows of vectors already in int8, in id order: rows that arrive in
    rowid order fill each page, where the npz's order split pages and left them a third
    empty (2.8 rows a page against 5)."""
    order = np.argsort(np.asarray(ids, dtype=np.int64), kind="stable")
    ids = [int(ids[i]) for i in order]
    q = q[order]
    if len(q) != len(ids):
        raise RuntimeError(f"refusing to write {len(ids)} ids for {len(q)} vectors")
    now = _now()
    dim = int(q.shape[1])
    for start in range(0, len(ids), WRITE_BLOCK):
        conn.executemany(
            "INSERT OR REPLACE INTO vectors (id, kind, parent_id, dim, template, vec, written_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (vid, kind_of(vid), parent_of(vid), dim, int(template), q[n].tobytes(), now)
                for n, vid in enumerate(ids[start : start + WRITE_BLOCK], start)
            ),
        )
    _bump(conn)
    return len(ids)


def delete(conn: sqlite3.Connection, ids: Iterable[int], *, with_chunks: bool = True) -> int:
    """Delete these vector ids and, with `with_chunks`, the chunks whose parent is one of them;
    the number deleted. Not committed."""
    drop = json.dumps(sorted({int(i) for i in ids}))
    if drop == "[]":
        return 0
    where = "id IN (SELECT value FROM json_each(?))"
    args: tuple = (drop,)
    if with_chunks:
        where += (
            " OR (kind IN ('chunk', 'email_chunk')"
            " AND parent_id IN (SELECT value FROM json_each(?)))"
        )
        args = (drop, drop)
    removed = conn.execute(f"DELETE FROM vectors WHERE {where}", args).rowcount
    if removed:
        _bump(conn)
    return int(removed)


def replace_all(conn: sqlite3.Connection, ids, vectors, template: int) -> int:
    """The table becomes exactly these rows (a rebuild's swap). Not committed."""
    conn.execute("DELETE FROM vectors")
    _bump(conn)
    return write(conn, ids, vectors, template)


def held_ids(conn: sqlite3.Connection) -> set[int]:
    return {int(r[0]) for r in conn.execute("SELECT id FROM vectors")}


def held_ids_of_kind(conn: sqlite3.Connection, kind: str) -> set[int]:
    return {int(r[0]) for r in conn.execute("SELECT id FROM vectors WHERE kind = ?", (kind,))}


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """The rows the table holds, in all and by kind; empty without the table (the dry run's
    diff)."""
    if "vectors" not in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }:
        return {}
    out = {"vectors rows": conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]}
    for kind, n in conn.execute("SELECT kind, COUNT(*) FROM vectors GROUP BY kind ORDER BY kind"):
        out[f"vectors {kind}"] = n
    return out


def migrate(conn: sqlite3.Connection, index_path: Path | None = None) -> int:
    """The step: create the table and copy the npz index into it, a block at a time; the
    number of vectors copied. The npz is only read. Its space is recorded in sync_metadata,
    as the writers did."""
    from src.store import embeddings as emb

    conn.execute(TABLE)
    for ddl in INDEXES:
        conn.execute(ddl)
    path = Path(index_path) if index_path is not None else emb.EMBEDDINGS_FILE
    if not path.exists():
        _bump(conn)
        return 0
    import zipfile

    space = emb.file_space(path)
    with zipfile.ZipFile(path) as archive, archive.open("ids.npy") as member:
        ids = np.lib.format.read_array(member, allow_pickle=False)
    # Quantised a block at a time into one int8 array (a byte a dimension: 190 MB for the
    # owner's 247,833 x 768), then written in id order.
    q: np.ndarray | None = None
    copied = 0
    for block in emb._vector_blocks(path, emb.SCAN_BLOCK):
        if q is None:
            q = np.empty((len(ids), block.shape[1]), dtype=np.int8)
        if copied + len(block) > len(ids):
            raise RuntimeError(f"{path} holds more vectors than its {len(ids)} ids; nothing copied")
        q[copied : copied + len(block)] = quantise(block)
        copied += len(block)
    if copied != len(ids):
        raise RuntimeError(f"{path} holds {len(ids)} ids for {copied} vectors; nothing copied")
    if q is not None:
        _write_int8(conn, ids, q, int(space["template"]) if space else 1)
    if space is not None:
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
            (emb.EMBEDDING_SPACE_KEY, json.dumps(space, sort_keys=True)),
        )
    return copied


def stored_space(conn: sqlite3.Connection) -> dict | None:
    """The space the store records for its vectors, None when it records none."""
    from src.store import embeddings as emb

    try:
        row = conn.execute(
            "SELECT value FROM sync_metadata WHERE key = ?", (emb.EMBEDDING_SPACE_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(row[0]) if row and row[0] else None


# --- reading ---------------------------------------------------------------------------------


_CACHE: dict = {"key": None, "value": None}
_CACHE_LOCK = threading.Lock()


def _db_file(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(row[2] or "")
    return ""


def _cache_key(conn: sqlite3.Connection) -> tuple:
    """What a loaded table is good for: the store's file (its path and inode: a pull renames a
    new copy in, a restore writes a new file) and the write counter every writer bumps in its
    own transaction. No scan of the table: a COUNT(*) here cost every search 20 ms."""
    import os

    path = _db_file(conn)
    try:
        inode = os.stat(path).st_ino if path else None
    except OSError:
        inode = None
    version = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = ?", (VERSION_KEY,)
    ).fetchone()
    return (path, inode, version[0] if version else None)


def load(conn: sqlite3.Connection) -> tuple[np.ndarray, np.ndarray, dict | None]:
    """(ids, unit vectors, space) of the table, cached in the process until a write changes it.

    The rows are int8 on disk and widened here, once, to unit vectors in float16, the npz
    index's own dtype in memory: numpy casts int8 to float32 at about 1.2 GB/s, so scanning
    the int8 rows took 150 ms a query on the owner's 247,833 vectors against 45 ms for
    float16 (measured 2026-11-04). The table halves the bytes on disk and in the pull, and no
    writer rewrites a file; a process holds what it held before.

    Raises FileNotFoundError when the table holds no vector, as the npz reader does for no
    file, so the callers' fallbacks are the same. Rows of several widths are read on the
    narrowest one's prefix.
    """
    from src.store import embeddings as emb

    key = _cache_key(conn)
    with _CACHE_LOCK:
        # An in-memory store has no file to key on: it is read each time.
        if key[0] and _CACHE["key"] == key:
            return _CACHE["value"]
        _CACHE.update({"key": None, "value": None})
        n = int(conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        if not n:
            raise FileNotFoundError(
                "The vectors table holds no vector. Run 'python -m src.cli embed'."
            )
        # Rows of two widths (summaries at 1536 beside the older 768, issue 171) are read on
        # their shared prefix: a Matryoshka vector's first dimensions, scaled to unit length,
        # are the narrower vector itself.
        dim = int(conn.execute("SELECT MIN(dim) FROM vectors").fetchone()[0])
        mixed = conn.execute("SELECT 1 FROM vectors WHERE dim > ? LIMIT 1", (dim,)).fetchone()
        # Each block of rows is widened straight into the float16 array: no int8 copy of the
        # whole table is held beside it.
        ids = np.empty(n, dtype=np.int64)
        unit = np.empty((n, dim), dtype=emb.STORE_DTYPE)
        done = 0
        cursor = conn.execute("SELECT id, vec FROM vectors ORDER BY id")
        while done < n:
            rows = cursor.fetchmany(emb.SCAN_BLOCK)[: n - done]
            if not rows:
                break
            take = len(rows)
            ids[done : done + take] = [r[0] for r in rows]
            blobs = (r[1][:dim] for r in rows) if mixed else (r[1] for r in rows)
            part = (
                np.frombuffer(b"".join(blobs), dtype=np.int8).reshape(take, dim).astype(np.float32)
            )
            norms = np.sqrt(np.einsum("ij,ij->i", part, part))[:, None]
            norms[norms == 0] = 1
            unit[done : done + take] = part / norms
            done += take
        if done != n:
            ids, unit = ids[:done], unit[:done]
        space = stored_space(conn)
        if space is not None:
            space = {**space, "dim": dim}
        value = (ids, unit, space)
        _CACHE.update({"key": key, "value": value})
        return value


def export_npz(conn: sqlite3.Connection, path: Path) -> int:
    """Write the table's rows to an npz in the index's own format (float16, unit rows): the
    copy install_index keeps for a rollback. The number written."""
    from src.store import embeddings as emb

    ids, unit, space = load(conn)
    emb._atomic_savez(path, ids, unit, space)
    return len(ids)
