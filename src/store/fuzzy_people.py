"""Review-gated fuzzy people matching — the long tail phase 7 can't catch.

Deterministic phase 7 (transliterate.canonical_name) already merges order-reversed
and Greek<->Latin variants. The residue needs judgment: nicknames, anglicizations
(Ιωάννης / Yiannis / John), typos, first-name-only vs full-name. This module surfaces
*candidate* pairs for HUMAN review — it NEVER auto-merges. Workflow:

    python -m src.store.fuzzy_people --generate --out ~/Downloads/people-review.jsonl
    #   review the file; set "decision": "merge" only on pairs you confirm
    python -m src.store.fuzzy_people --apply ~/Downloads/people-review.jsonl

Candidates are found deterministically (surname blocking + string similarity). An
optional `adjudicator` callable (e.g. an LLM) can PRE-FILL a suggested decision, but
apply acts only on the decisions present in the reviewed file — so a wrong LLM guess
can never merge two real people without a human keeping "decision": "merge".
"""

import difflib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from src.config import DEFAULT_DB
from src.store.dedup_people import merge_person
from src.store.schema import get_connection
from src.store.transliterate import canonical_name

DB_PATH = DEFAULT_DB

# Skip pathologically large surname blocks — too ambiguous, O(n^2) blowup.
MAX_BLOCK = 60


def _surname_key(name: str) -> str:
    """Blocking key: the longest canonical token (usually the surname)."""
    toks = canonical_name(name).split()
    return max(toks, key=len) if toks else ""


def similarity(name_a: str, name_b: str) -> float:
    """0..1 similarity of two names on their canonical forms.

    A subset relationship (first-name-only vs full name, same tokens) scores high;
    otherwise fall back to a character-level ratio.
    """
    ca, cb = canonical_name(name_a), canonical_name(name_b)
    if not ca or not cb:
        return 0.0
    ta, tb = set(ca.split()), set(cb.split())
    if ta and tb and (ta <= tb or tb <= ta):
        return 0.9
    return difflib.SequenceMatcher(None, ca, cb).ratio()


def find_fuzzy_candidates(
    conn: sqlite3.Connection, threshold: float = 0.82, max_pairs: int = 1000
) -> list[dict]:
    """Return likely same-person pairs phase 7 missed, ranked by similarity.

    Blocks by surname token; within a block, pairs names that are NOT already
    canonical-equal (phase 7's job) and don't have two different non-empty emails
    (different emails => different people). Emailless fragments are the target.
    """
    people = conn.execute("SELECT id, name, email FROM people").fetchall()
    blocks: dict[str, list] = defaultdict(list)
    for p in people:
        key = _surname_key(p["name"])
        if key:
            blocks[key].append(p)

    candidates: list[dict] = []
    for members in blocks.values():
        if len(members) < 2 or len(members) > MAX_BLOCK:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if canonical_name(a["name"]) == canonical_name(b["name"]):
                    continue  # phase 7 territory
                ea = (a["email"] or "").strip().lower()
                eb = (b["email"] or "").strip().lower()
                if ea and eb and ea != eb:
                    continue  # two distinct emails => distinct people
                score = similarity(a["name"], b["name"])
                if score >= threshold:
                    candidates.append(
                        {
                            "id_a": a["id"],
                            "name_a": a["name"],
                            "email_a": a["email"],
                            "id_b": b["id"],
                            "name_b": b["name"],
                            "email_b": b["email"],
                            "score": round(score, 3),
                            "decision": "",  # human sets "merge" to confirm
                        }
                    )
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:max_pairs]


def write_review_file(candidates: list[dict], path: str) -> int:
    """Write candidate pairs as JSONL for human review. Returns count written."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for c in candidates:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return len(candidates)


def read_review_file(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def apply_reviewed_merges(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Merge only rows a human marked decision == 'merge'. Survivor = the
    email-bearing record (else lowest id). Returns merges applied."""
    merged = 0
    for r in rows:
        if str(r.get("decision", "")).strip().lower() != "merge":
            continue
        a_id, b_id = r["id_a"], r["id_b"]
        # Survivor prefers the email-bearing record.
        if r.get("email_a") and not r.get("email_b"):
            keep, remove = a_id, b_id
        elif r.get("email_b") and not r.get("email_a"):
            keep, remove = b_id, a_id
        else:
            keep, remove = min(a_id, b_id), max(a_id, b_id)
        # Both rows must still exist (a prior merge may have removed one).
        if conn.execute("SELECT 1 FROM people WHERE id = ?", (keep,)).fetchone() is None:
            continue
        if conn.execute("SELECT 1 FROM people WHERE id = ?", (remove,)).fetchone() is None:
            continue
        merge_person(conn, keep, remove)
        merged += 1
    conn.commit()
    return merged


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Review-gated fuzzy people matching")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    parser.add_argument("--generate", action="store_true", help="Write a review file of candidates")
    parser.add_argument(
        "--apply", type=str, metavar="FILE", help="Apply merges from a reviewed file"
    )
    parser.add_argument(
        "--out", type=str, default=str(Path.home() / "Downloads" / "people-review.jsonl")
    )
    parser.add_argument("--threshold", type=float, default=0.82)
    args = parser.parse_args()

    conn = get_connection(args.db)
    if args.apply:
        rows = read_review_file(args.apply)
        n = apply_reviewed_merges(conn, rows)
        print(f"Applied {n} human-approved merge(s) from {args.apply}")
    else:  # default: generate
        cands = find_fuzzy_candidates(conn, threshold=args.threshold)
        n = write_review_file(cands, args.out)
        print(
            f"Wrote {n} candidate pair(s) to {args.out} — review, keep decision:'merge', then --apply"
        )
    conn.close()


if __name__ == "__main__":
    _cli()
