"""Semantic search via embeddings for the second-brain knowledge store.

Generates embeddings for email summaries using Google's text-embedding model
and provides cosine similarity search.
"""

import gc
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from src.config import REPO_ROOT

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 3072
EMBEDDINGS_FILE = REPO_ROOT / "data" / "embeddings.npz"
BATCH_SIZE = 100  # Google API batch limit
LOG_FILE = REPO_ROOT / "data" / "embeddings.log"

# Namespace offsets: emails (positive), attachments (negative), conversations (-2M and below)
CONVERSATION_ID_OFFSET = -2_000_000

# Teams thread embeddings live below conversations.
# Conversations occupy the -2_000_000 .. -1 range so we put teams below it.
TEAMS_THREAD_ID_OFFSET = -10_000_000

# In-process cache of the embedding index, keyed by file mtime. query_semantic and
# the hybrid-recall candidate helper both hit this, so a long-lived process (the MCP
# server) loads and unit-normalizes the ~1 GB index ONCE instead of on every query.
# Reloads automatically when embeddings.npz changes (a sync job appended vectors).
_INDEX_CACHE: dict = {"path": None, "mtime": None, "ids": None, "unit": None}


def _load_index(index_path=None):
    """Return (ids, unit_vectors) for the embedding index, cached by file mtime.

    Vectors are L2-normalized once at load, so cosine similarity at query time is a
    single matrix-vector product. Raises FileNotFoundError if no index exists.
    """
    path = EMBEDDINGS_FILE if index_path is None else Path(index_path)
    if not path.exists():
        raise FileNotFoundError("No embedding index found. Run 'python -m src.cli embed' first.")
    mtime = path.stat().st_mtime
    if _INDEX_CACHE["path"] == str(path) and _INDEX_CACHE["mtime"] == mtime:
        return _INDEX_CACHE["ids"], _INDEX_CACHE["unit"]
    data = np.load(path, allow_pickle=False)
    ids = data["ids"]
    vectors = data["vectors"]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    unit = (vectors / norms).astype(np.float32)
    _INDEX_CACHE.update({"path": str(path), "mtime": mtime, "ids": ids, "unit": unit})
    return ids, unit


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


def _atomic_savez(path, ids, vectors) -> None:
    """Persist the id/vector arrays to ``path`` atomically.

    ``np.savez`` streams the (~1 GB) archive to its target incrementally, so a
    process killed mid-write — e.g. an OOM SIGKILL during a sync — leaves a
    truncated file that later fails to load with "File is not a zip file".
    Writing to a sibling temp file and ``replace``-ing it into place means
    ``path`` is only ever the previous good archive or the complete new one,
    never a half-written zip.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # np.savez auto-appends ".npz" if missing — strip it from our tmp basename.
    tmp_base = path.with_suffix(".tmp")  # e.g. .../embeddings.tmp
    np.savez(tmp_base, ids=np.asarray(ids, dtype=np.int64), vectors=vectors)
    tmp_path = tmp_base.with_suffix(".tmp.npz")  # actual file np.savez wrote
    tmp_path.replace(path)


def _get_client():
    """Get a Google GenAI client via Vertex AI (ADC auth)."""
    import os

    from google import genai

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


def generate_embeddings(texts: list[str], client=None) -> np.ndarray:
    """Generate embeddings for a list of texts using Google's embedding model.

    Args:
        texts: List of text strings to embed
        client: Optional pre-configured GenAI client

    Returns:
        numpy array of shape (len(texts), EMBEDDING_DIM)
    """
    if client is None:
        client = _get_client()

    all_embeddings = []
    total_batches = (len(texts) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_num, i in enumerate(range(0, len(texts), BATCH_SIZE), 1):
        batch = texts[i : i + BATCH_SIZE]

        # Retry with exponential backoff on rate limits
        for attempt in range(5):
            try:
                result = client.models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=batch,
                )
                for emb in result.embeddings:
                    all_embeddings.append(emb.values)
                break
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    wait = min(2**attempt * 5, 60)
                    _log(f"Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        if batch_num % 50 == 0 or batch_num == total_batches:
            _log(
                f"Progress: {batch_num}/{total_batches} batches ({min(i + BATCH_SIZE, len(texts))}/{len(texts)} texts)"
            )

        if i + BATCH_SIZE < len(texts):
            time.sleep(1.5)  # Stay under 3000 req/min limit

    return np.array(all_embeddings, dtype=np.float32)


def _email_embed_text(subject, sender_name, date_received, summary) -> str:
    """Metadata-enriched text for embedding an email.

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


def build_index(conn: sqlite3.Connection, force: bool = False) -> int:
    """Build embedding index for emails and attachment summaries.

    Generates embeddings for email summaries and attachment summaries,
    saves to disk. Skips items that already have embeddings unless force=True.

    IDs use distinct namespaces: email IDs are positive, attachment IDs are
    negative (negated attachment_content.id) to avoid collisions.

    Args:
        conn: Database connection
        force: If True, rebuild all embeddings from scratch

    Returns:
        Number of new embeddings generated
    """
    # Load existing index
    existing_ids = set()
    existing_vectors = None
    if not force and EMBEDDINGS_FILE.exists():
        data = np.load(EMBEDDINGS_FILE, allow_pickle=False)
        existing_ids = {int(x) for x in data["ids"]}
        existing_vectors = data["vectors"]

    # Get emails needing embeddings. Embed a metadata-enriched string (subject /
    # sender / date + summary), not the summary alone, so the vector captures
    # who/when/what-subject — a no-extra-LLM variant of contextual retrieval.
    email_rows = conn.execute("""
        SELECT id, summary, subject, sender_name, date_received FROM emails
        WHERE summary IS NOT NULL AND summary != ''
    """).fetchall()

    def _email_text(r):
        return _email_embed_text(r["subject"], r["sender_name"], r["date_received"], r["summary"])

    if force:
        to_embed = [(r["id"], _email_text(r)) for r in email_rows]
    else:
        to_embed = [(r["id"], _email_text(r)) for r in email_rows if r["id"] not in existing_ids]

    # Get attachment summaries needing embeddings (negative IDs to avoid collision)
    try:
        att_rows = conn.execute("""
            SELECT ac.id, ac.summary, a.filename
            FROM attachment_content ac
            JOIN attachments a ON a.id = ac.attachment_id
            WHERE ac.llm_status = 'extracted'
              AND ac.summary IS NOT NULL AND ac.summary != ''
        """).fetchall()

        for r in att_rows:
            att_id = -r["id"]  # negative namespace
            if force or att_id not in existing_ids:
                # Prefix with filename for better context
                text = f"[Attachment: {r['filename']}] {r['summary']}"
                to_embed.append((att_id, text))
    except Exception:
        # attachment tables may not exist in older schemas
        pass

    # Get conversation summaries needing embeddings (CONVERSATION_ID_OFFSET namespace)
    try:
        conv_rows = conn.execute("""
            SELECT id, summary, project_name
            FROM conversations
            WHERE summary IS NOT NULL AND summary != ''
        """).fetchall()

        for r in conv_rows:
            conv_emb_id = CONVERSATION_ID_OFFSET - r["id"]
            if force or conv_emb_id not in existing_ids:
                project = r["project_name"] or "unknown"
                text = f"[Conversation in {project}] {r['summary']}"
                to_embed.append((conv_emb_id, text))
    except Exception:
        # conversation tables may not exist in older schemas
        pass

    if not to_embed:
        _log("All emails and attachments already have embeddings.")
        return 0

    _log(f"Generating embeddings for {len(to_embed)} items...")

    client = _get_client()
    ids = [t[0] for t in to_embed]
    texts = [t[1] for t in to_embed]

    new_vectors = generate_embeddings(texts, client)

    # Merge with existing
    if existing_vectors is not None and not force:
        all_ids = list(existing_ids) + ids
        all_vectors = np.vstack([existing_vectors, new_vectors])
    else:
        all_ids = ids
        all_vectors = new_vectors

    # Save to disk atomically so an interrupted write can never truncate the
    # shared index (see _atomic_savez).
    _atomic_savez(EMBEDDINGS_FILE, all_ids, all_vectors)

    _log(f"Saved {len(all_ids)} embeddings to {EMBEDDINGS_FILE}")
    return len(to_embed)


def query_semantic(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Semantic search across email summaries using cosine similarity.

    Args:
        conn: Database connection
        query: Natural language search query
        limit: Maximum number of results

    Returns:
        List of dicts with keys: email_id, date, subject, summary, similarity
    """
    # Index load + unit-normalization is cached across calls (see _load_index),
    # so a long-lived process pays the ~1 GB read once, not per query.
    ids, normalized = _load_index()

    # Generate query embedding
    query_vec = np.asarray(generate_embeddings([query])[0], dtype=np.float32)
    query_norm = query_vec / (np.linalg.norm(query_vec) or 1)

    similarities = normalized @ query_norm

    # Get top-k indices
    top_indices = np.argsort(similarities)[::-1][:limit]

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
            row = conn.execute(
                """
                SELECT ac.id as attachment_content_id, a.filename,
                       ac.summary, e.subject as email_subject,
                       e.date_received as date
                FROM attachment_content ac
                JOIN attachments a ON a.id = ac.attachment_id
                LEFT JOIN emails e ON e.id = a.email_id
                WHERE ac.id = ?
            """,
                (-item_id,),
            ).fetchone()

            if row:
                result = dict(row)
                result["type"] = "attachment"
                result["similarity"] = round(similarity, 4)
                results.append(result)

    return results


def _vector_id_to_email_id(conn: sqlite3.Connection, item_id: int) -> int | None:
    """Map a namespaced embedding id to a parent email id (or None if not email-backed).

    Positive ids are email ids; the attachment namespace (-1 .. CONVERSATION_ID_OFFSET+1)
    maps via attachment_content -> attachments.email_id; conversation/teams namespaces
    are not emails and return None.
    """
    if item_id > 0:
        return item_id
    if item_id <= CONVERSATION_ID_OFFSET:
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
) -> list[int]:
    """Ranked, de-duplicated email ids for `query` from the vector index, best-first.

    Attachment vectors are folded onto their parent email so the semantic signal
    fuses with keyword search at email granularity. Used by recall()'s hybrid path;
    `embed_fn` / `index_path` are injection points for testing.
    """
    embed = embed_fn or generate_embeddings
    ids, normalized = _load_index(index_path)
    query_vec = np.asarray(embed([query])[0], dtype=np.float32)
    query_norm = query_vec / (np.linalg.norm(query_vec) or 1)
    similarities = normalized @ query_norm
    # Over-fetch: many top vectors collapse onto the same email or onto non-email
    # namespaces (conversations/teams), so we need headroom to fill `limit` emails.
    order = np.argsort(similarities)[::-1][: max(limit * 4, limit)]
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
    return out


def _append_to_index(ids: list[int], vectors) -> None:
    """Append (id, vector) rows to EMBEDDINGS_FILE.

    EMBEDDINGS_FILE format: npz with arrays 'ids' (int64) and 'vectors' (float32).
    Re-saves the merged arrays atomically. Existing ids are replaced (so re-embedding
    the same teams_thread overwrites its prior vector instead of duplicating it).
    """
    if EMBEDDINGS_FILE.exists():
        existing = np.load(EMBEDDINGS_FILE, allow_pickle=False)
        old_ids = existing["ids"]
        old_vecs = existing["vectors"]
        # Drop any rows whose id matches one we're inserting (replace semantics).
        keep = ~np.isin(old_ids, np.array(ids, dtype=np.int64))
        merged_ids = np.concatenate([old_ids[keep], np.array(ids, dtype=np.int64)])
        merged_vecs = np.concatenate([old_vecs[keep], vectors])
        # Release the NpzFile-backed arrays before we allocate the tmp save buffer.
        del existing, old_ids, old_vecs
        gc.collect()
    else:
        merged_ids = np.array(ids, dtype=np.int64)
        merged_vecs = vectors
    _atomic_savez(EMBEDDINGS_FILE, merged_ids, merged_vecs)


def _teams_threads_to_embed(conn) -> list[tuple[int, str]]:
    """Return [(teams_threads.id, summary), ...] for threads needing fresh embeddings.

    A thread needs (re-)embedding when extraction_status='extracted' AND
    (embedding_at IS NULL OR embedding_at < extracted_at).
    """
    rows = conn.execute(
        """
        SELECT id, summary
        FROM teams_threads
        WHERE extraction_status = 'extracted'
          AND COALESCE(summary, '') != ''
          AND (embedding_at IS NULL OR embedding_at < extracted_at)
        ORDER BY id
        """
    ).fetchall()
    out = []
    for r in rows:
        # Support both Row and tuple access patterns.
        rid = r["id"] if hasattr(r, "keys") else r[0]
        summary = r["summary"] if hasattr(r, "keys") else r[1]
        out.append((int(rid), summary))
    return out


def build_teams_index(conn, force: bool = False) -> int:
    """Embed every teams_thread that needs it. Returns count generated.

    Stores into the same EMBEDDINGS_FILE keyed by TEAMS_THREAD_ID_OFFSET-id
    (mirrors the conversation encoding shape), so all teams npz keys are
    strictly less than TEAMS_THREAD_ID_OFFSET (-10M). Lets query_semantic
    distinguish teams from conversations purely by id sign+threshold.
    """
    if force:
        # Force rebuild = clear embedding_at so the query re-selects everything.
        conn.execute("UPDATE teams_threads SET embedding_at = NULL")
        conn.commit()

    pairs = _teams_threads_to_embed(conn)
    if not pairs:
        return 0

    ids = [p[0] for p in pairs]
    texts = [p[1] for p in pairs]
    vecs = generate_embeddings(texts)

    # Persist to embeddings.npz alongside email/conversation entries.
    # Encoding mirrors CONVERSATION_ID_OFFSET - id (decreasing-with-id) so the
    # query_semantic guard `item_id <= TEAMS_THREAD_ID_OFFSET` actually fires.
    # The earlier i+OFFSET form silently fell into the conversation routing
    # branch and degraded semantic search. Validation gate caught it.
    _append_to_index(
        ids=[TEAMS_THREAD_ID_OFFSET - i for i in ids],
        vectors=vecs,
    )

    now = datetime.now(UTC).isoformat()
    for tid in ids:
        conn.execute(
            "UPDATE teams_threads SET embedding_at = ? WHERE id = ?",
            (now, tid),
        )
    conn.commit()
    return len(ids)
