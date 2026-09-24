#!/usr/bin/env python3
"""A known-item baseline for keyword search: can an email be found by its subject?

A regression guard, run before and after changing how text is tokenised
(stemming, prefix matching): a change that loses emails shows up as a drop. It
cannot show a gain. The queries are the subject's own words, which the subject
index matches exactly already, so a looser match can only add competition.

`build` samples emails with at least two search words in their subject, leaving
news out, and keeps two of those words as the query for each; the email is the
answer. The set holds subjects, so it is written outside the repository
(default ~/.second-brain/retrieval-eval.json). `run` searches each query the way
search_emails does and reports how often the answer comes back first, in the top
5 and in the top 10, and the mean reciprocal rank, twice: for the email itself,
and for its thread (the email or any other in its conversation). Replies share
the subject, so which of them ranks first is a tie-break, and keyword search
returns one email per thread for a subject match; the thread figures are the
ones to compare.

    python scripts/retrieval_eval.py build [--size 20] [--seed 7]
    python scripts/retrieval_eval.py run

Both open the database read-only, so they are safe on a replica.
"""

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DEFAULT_DB  # noqa: E402
from src.store.greek import search_words  # noqa: E402

DEFAULT_SET = Path.home() / ".second-brain" / "retrieval-eval.json"
QUERY_WORDS = 2


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_set(conn: sqlite3.Connection, size: int, seed: int) -> list[dict]:
    """`size` emails, each with a query of QUERY_WORDS words from its own subject.

    The longest words are kept: they are the ones someone remembers an email by,
    and the least likely to match half the mailbox.
    """
    rows = conn.execute(
        "SELECT id, subject FROM emails "
        "WHERE COALESCE(mailbox_name, '') <> 'News' AND COALESCE(subject, '') <> '' "
        "ORDER BY id"
    ).fetchall()
    usable = [r for r in rows if len(search_words(r["subject"])) >= QUERY_WORDS]
    picked = random.Random(seed).sample(usable, min(size, len(usable)))
    out = []
    for row in picked:
        words = sorted(search_words(row["subject"]), key=lambda w: (-len(w), w))
        out.append({"email_id": row["id"], "query": " ".join(words[:QUERY_WORDS])})
    return out


def rank_of(results: list[dict], email_ids: set[int]) -> int | None:
    """1-based position of the first of `email_ids` in `results`, or None."""
    for n, row in enumerate(results, start=1):
        if row.get("email_id") in email_ids:
            return n
    return None


def thread_of(conn: sqlite3.Connection, email_id: int) -> set[int]:
    """The ids of every email in `email_id`'s conversation, itself included."""
    row = conn.execute("SELECT conversation_id FROM emails WHERE id = ?", (email_id,)).fetchone()
    if not row or not row[0]:
        return {email_id}
    ids = conn.execute("SELECT id FROM emails WHERE conversation_id = ?", (row[0],))
    return {r[0] for r in ids} | {email_id}


def score(ranks: list[int | None]) -> dict:
    """hit@1, hit@5, hit@10 and mean reciprocal rank over the ranks found."""
    total = len(ranks) or 1
    return {
        "queries": len(ranks),
        "hit@1": sum(1 for r in ranks if r == 1) / total,
        "hit@5": sum(1 for r in ranks if r is not None and r <= 5) / total,
        "hit@10": sum(1 for r in ranks if r is not None and r <= 10) / total,
        "mrr": sum(1 / r for r in ranks if r) / total,
    }


def run_set(conn: sqlite3.Connection, items: list[dict]) -> tuple[dict, list[dict]]:
    """Search every query as search_emails does; return the score and the misses."""
    from src.store.greek import register_sql_functions
    from src.store.query import query_by_keyword

    register_sql_functions(conn)
    ranks, thread_ranks, misses = [], [], []
    for item in items:
        results = query_by_keyword(conn, item["query"], limit=10)
        rank = rank_of(results, {item["email_id"]})
        thread_rank = rank_of(results, thread_of(conn, item["email_id"]))
        ranks.append(rank)
        thread_ranks.append(thread_rank)
        if thread_rank != 1:
            misses.append({**item, "rank": thread_rank})
    return {"email": score(ranks), "thread": score(thread_ranks)}, misses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("command", choices=["build", "run"])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--file", type=Path, default=DEFAULT_SET)
    parser.add_argument("--size", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    conn = _open(args.db)
    try:
        if args.command == "build":
            items = build_set(conn, args.size, args.seed)
            args.file.parent.mkdir(parents=True, exist_ok=True)
            args.file.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{len(items)} queries written to {args.file}")
            return 0
        items = json.loads(args.file.read_text(encoding="utf-8"))
        result, misses = run_set(conn, items)
    finally:
        conn.close()
    print(json.dumps(result, indent=2))
    for miss in misses:
        print(f"  thread rank {miss['rank']}: email {miss['email_id']} for {miss['query']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
