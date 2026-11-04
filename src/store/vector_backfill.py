"""`brain maint vectors-backfill` (issue 171): the history the hourly pass leaves alone, in
batches the owner schedules (nightly on the producer), on a store that took the gated step
`vectors`. A dry run unless --apply; refused on a replica.

- Email chunks: the bodies of the mail below the watermark (embeddings.EMAIL_CHUNKS_UPTO_KEY)
  with no chunk vector yet, newest first, at most --limit emails a run.
- Copy chunks: the chunk vectors of attachments whose bytes (sha256) an earlier attachment
  already holds, which the npz index made for every copy; deleted, no model call.
- With --summaries: the summary vectors still at 768 embedded again at
  embeddings.TABLE_SUMMARY_DIM, newest first, at most --limit a run.

    brain maint vectors-backfill                          # counts, writes nothing
    brain maint vectors-backfill --apply --limit 2000     # a night's batch
    brain maint vectors-backfill --apply --summaries --limit 5000
"""

import sqlite3
import sys

from src.store import embeddings as emb
from src.store import vector_store

SUMMARY_KINDS = ("email", "attachment", "conversation", "teams_thread", "whatsapp_thread")


def _pending_emails(conn: sqlite3.Connection) -> list:
    """The email rows the backfill would chunk, newest first: below the watermark (all, with
    none), with no chunk vector, long enough."""
    row = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = ?", (emb.EMAIL_CHUNKS_UPTO_KEY,)
    ).fetchone()
    where, args = ("id <= ?", (int(row[0]),)) if row else ("1", ())
    held = {emb.email_chunk_of(v)[0] for v in vector_store.held_ids_of_kind(conn, "email_chunk")}
    out = []
    for r in emb._email_chunk_rows(conn, where, args, "DESC"):
        if r["id"] in held:
            continue
        pairs = emb.email_chunk_pairs(r)
        if pairs:
            out.append((int(r["id"]), pairs))
    return out


def _copy_chunks(conn: sqlite3.Connection) -> set[int]:
    """The chunk vectors held for copies of a file an earlier attachment holds."""
    from src.store.chunks import copy_attachments, eligible

    found = eligible(conn)
    content = {aid: cid for aid, cid, *_ in found}
    copies = {-content[a] for a in copy_attachments(conn, content)}
    return {
        int(r[0])
        for r in conn.execute("SELECT id, parent_id FROM vectors WHERE kind = 'chunk'")
        if int(r[1]) in copies
    }


def _narrow_summaries(conn: sqlite3.Connection, space: dict, limit: int) -> list:
    """(vector id, text) of the summary rows still narrower than the summary width, newest
    first (highest id within each kind), at most `limit`."""
    width = int(emb.summary_space(space, True)["dim"])
    narrow = {
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM vectors WHERE dim < ? AND kind IN"
            f" ({', '.join('?' * len(SUMMARY_KINDS))})",
            (width, *SUMMARY_KINDS),
        )
    }
    items = [(v, t) for v, t in emb.summary_items(conn, space) if v in narrow]
    items.sort(key=lambda p: (p[0] < 0, -abs(p[0])))
    return items[:limit]


def run(conn: sqlite3.Connection, *, apply: bool, limit: int, summaries: bool) -> dict:
    space = vector_store.stored_space(conn) or emb.embedding_space()
    out: dict = {}
    pending = _pending_emails(conn)
    out["email_chunks_emails"] = len(pending)
    out["email_chunks_texts"] = sum(len(p) for _e, p in pending)
    copies = _copy_chunks(conn)
    out["copy_chunks"] = len(copies)
    narrow = _narrow_summaries(conn, space, limit) if summaries else []
    out["summaries"] = len(narrow)
    if not apply:
        return out
    if copies:
        vector_store.delete(conn, copies, with_chunks=False)
        conn.commit()
    batch = [pair for _e, pairs in pending[:limit] for pair in pairs]
    client = emb._get_client()
    with emb.in_space(space):
        if batch:
            vectors = emb.generate_embeddings([t for _v, t in batch], client, space=space)
            vector_store.write(conn, [v for v, _t in batch], vectors, int(space["template"]))
            conn.commit()
        if narrow:
            vectors = emb.generate_embeddings(
                [t for _v, t in narrow], client, space=emb.summary_space(space, True)
            )
            vector_store.write(conn, [v for v, _t in narrow], vectors, int(space["template"]))
            conn.commit()
    out["embedded_email_chunks"] = len(batch)
    out["embedded_summaries"] = len(narrow)
    return out


def cmd_vectors_backfill(args) -> int:
    from src.config import is_replica, replica_refusal
    from src.store.schema import get_connection

    if is_replica():
        print(replica_refusal("vectors-backfill"), file=sys.stderr)
        return 2
    conn = get_connection(str(args.db))
    try:
        if not vector_store.enabled(conn):
            print(
                "No vectors table: run `brain maint migrate --apply-gated` (step `vectors`) first.",
                file=sys.stderr,
            )
            return 2
        result = run(conn, apply=args.apply, limit=args.limit, summaries=args.summaries)
    finally:
        conn.close()
    mode = "applied" if args.apply else "dry run, nothing written; --apply writes"
    print(f"vectors-backfill ({mode}):")
    print(
        f"  email chunks: {result['email_chunks_emails']} emails,"
        f" {result['email_chunks_texts']} chunks waiting"
        + (f"; embedded {result['embedded_email_chunks']}" if args.apply else "")
    )
    verb = "dropped" if args.apply else "to drop"
    print(f"  copy chunks {verb}: {result['copy_chunks']}")
    if args.summaries:
        print(
            f"  summaries at {emb.TABLE_SUMMARY_DIM}: {result['summaries']} this run"
            + (f"; embedded {result['embedded_summaries']}" if args.apply else "")
        )
    return 0


def add_maint_parser(maint_subparsers) -> None:
    parser = maint_subparsers.add_parser(
        "vectors-backfill",
        help="Chunk the history's email bodies, drop copy chunks, lift summaries to 1536 "
        "(after the gated vectors step; dry run unless --apply)",
        description=__doc__,
    )
    parser.add_argument("--apply", action="store_true", help="Write and embed")
    parser.add_argument(
        "--limit", type=int, default=500, help="Emails (and summaries) a run, newest first"
    )
    parser.add_argument(
        "--summaries", action="store_true", help=f"Embed 768 summaries at {emb.TABLE_SUMMARY_DIM}"
    )
    parser.set_defaults(func=cmd_vectors_backfill)
