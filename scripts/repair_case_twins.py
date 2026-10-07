"""Give each email back its own extraction where the loader stored a case twin's.

Two Outlook message ids can differ in letter case alone, and the loader matched
extraction files by the lowercased name, so an email could be stored with its
twin's summary, decisions, action items, commitments, people and topics
(src/extract/extraction_files.py has the whole story). The loader now matches the
exact id; this repairs what it stored before.

For every stored email whose id has a case twin, in the database or on disk, the
stored summary is compared with the email's own extraction file:

    rewrite      its own file is on disk and the stored summary is a twin's: the
                 rows are replaced from the file
    lost         there is no own file (macOS wrote the twin's over it) and the
                 stored summary is a twin's: --reextract asks the model again, from
                 the stored email, and keeps the answer on disk
    correct      the stored summary is the own file's
    unverified   no own file, and the stored summary is no twin's: left alone
    unexplained  the stored summary is neither the own file's nor a twin's: left
                 alone, and listed

The vectors of the emails it rewrites are dropped, so the next index build embeds
their new summaries. Each rewrite records its vector as owed in sync_metadata, in
the same transaction, so a run stopped before it drops them leaves them for the
next --apply rather than forgotten: build_index embeds only ids it lacks.

    python scripts/repair_case_twins.py                        # report only
    python scripts/repair_case_twins.py --apply                # rewrite from files
    python scripts/repair_case_twins.py --apply --reextract    # and ask for the lost
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.config import (  # noqa: E402
    DATA_ROOT,
    DEFAULT_DB,
    EXTRACT_ENGINE,
    is_replica,
    replica_refusal,
)
from src.export.state import write_json_atomic  # noqa: E402
from src.extract.extraction_files import extraction_path  # noqa: E402
from src.redact import redact_payload  # noqa: E402
from src.store.embeddings import remove_vectors  # noqa: E402
from src.store.loader import replace_extraction, stored_email  # noqa: E402
from src.store.schema import get_connection  # noqa: E402

EXTRACTED = DATA_ROOT / "extracted"
SAMPLES = 10
HEADER_ROLES = ("sender", "recipient", "cc")
OWED_KEY = "repair_case_twins_vectors_owed"  # sync_metadata: rewritten, vector not dropped
SETTLE_EVERY = 500  # rewrites between two drops of the owed vectors


@dataclass
class Repair:
    email_id: int
    message_id: str
    wrong: dict
    right: dict | None  # None: its own extraction is lost


def _name_id(path: Path) -> str:
    """The message id a file is named for, under the current name or the old one."""
    head = path.stem.rpartition(".")[0]
    if head and extraction_path(path.parent, head).name == path.name:
        return head
    return path.stem


def _text(summary) -> str:
    return (summary or "").strip()


def plan(conn, extracted: Path) -> tuple[list[Repair], dict, list[int]]:
    """(the repairs, a count per outcome, the unexplained email ids)."""
    emails = [
        (int(email_id), str(message_id), summary)
        for email_id, message_id, summary in conn.execute(
            "SELECT id, message_id, summary FROM emails"
        )
    ]
    names = {path: _name_id(path) for path in extracted.glob("*.json")}
    ids_by_key: dict[str, set[str]] = defaultdict(set)
    for _, message_id, _ in emails:
        ids_by_key[message_id.lower()].add(message_id)
    for name_id in names.values():
        ids_by_key[name_id.lower()].add(name_id)
    twin_keys = {key for key, ids in ids_by_key.items() if len(ids) > 1}

    # Every extraction on disk under a twin key, by the id stored in it. A file under
    # the current name beats one under the old name, and either beats a file named
    # for another id: macOS wrote one twin's extraction over the other's file.
    on_disk: dict[str, dict] = {}
    rank: dict[str, int] = {}
    counts = dict.fromkeys(
        ("twin_emails", "correct", "rewrite", "lost", "unverified", "unexplained", "unreadable"),
        0,
    )
    for path, name_id in names.items():
        if name_id.lower() not in twin_keys:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            counts["unreadable"] += 1
            continue
        if not isinstance(data, dict):
            counts["unreadable"] += 1
            continue
        stored = data.get("message_id")
        owner = name_id if stored is None else str(stored)
        this_rank = 0 if owner != name_id else 1 if path.name == f"{name_id}.json" else 2
        if this_rank >= rank.get(owner, -1):
            on_disk[owner] = data
            rank[owner] = this_rank
    by_key: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for owner, data in on_disk.items():
        by_key[owner.lower()].append((owner, data))

    repairs: list[Repair] = []
    unexplained: list[int] = []
    for email_id, message_id, summary in emails:
        key = message_id.lower()
        if key not in twin_keys:
            continue
        counts["twin_emails"] += 1
        stored = _text(summary)
        own = on_disk.get(message_id)
        if own is not None and _text(own.get("summary")) == stored:
            counts["correct"] += 1
            continue
        wrong = next(
            (
                data
                for owner, data in by_key[key]
                if owner != message_id and _text(data.get("summary")) == stored
            ),
            None,
        )
        if own is None and (wrong is None or not stored):
            counts["unverified"] += 1
        elif wrong is None:
            counts["unexplained"] += 1
            unexplained.append(email_id)
        else:
            counts["rewrite" if own is not None else "lost"] += 1
            repairs.append(Repair(email_id, message_id, wrong=wrong, right=own))
    return repairs, counts, unexplained


def _header_named(extraction: dict) -> set[str]:
    """The names, lowercased, an extraction gave a header role (sender, recipient, cc)."""
    names: set[str] = set()
    people_roles = extraction.get("people_roles")
    if not isinstance(people_roles, dict):
        return names
    for name, roles in people_roles.items():
        for role in [roles] if isinstance(roles, str) else roles or []:
            if (role.get("role") if isinstance(role, dict) else role) in HEADER_ROLES:
                names.add(str(name).lower())
    return names


def _owed(conn) -> list[int]:
    row = conn.execute("SELECT value FROM sync_metadata WHERE key = ?", (OWED_KEY,)).fetchone()
    return json.loads(row[0]) if row else []


def _owe(conn, email_id: int) -> None:
    """Record the email's vector as owed, in the transaction of its rewrite."""
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        (OWED_KEY, json.dumps(sorted({*_owed(conn), email_id}))),
    )


def _settle(conn) -> int:
    """Drop the owed vectors, then the record of them; how many vectors went."""
    owed = _owed(conn)
    if not owed:
        return 0
    removed = remove_vectors(owed)
    conn.execute("DELETE FROM sync_metadata WHERE key = ?", (OWED_KEY,))
    conn.commit()
    return removed


def _ask_model(email: dict) -> tuple[dict | None, bool]:
    """(the extraction, or None; whether the model is out of quota or credentials)."""
    from src.extract import local

    _, extraction, is_quota, _ = local.extract_inline(
        email, os.environ.get("GEMINI_API_KEY"), engine=EXTRACT_ENGINE
    )
    return extraction, is_quota or local._shutdown


def apply(conn, repairs: list[Repair], extracted: Path, reextract: bool, limit: int) -> dict:
    """Rewrite each repair, one commit each: the producer's syncs write meanwhile. The
    ones with a file go first, so a model that runs out stops only the asking.

    Vectors are dropped after the commits: a build that ran between them found the
    old vectors still indexed and embedded nothing, and the next embeds the new
    summaries. Drops happen every SETTLE_EVERY rewrites, before the first question
    to the model, and on the way out, however the run ends.
    """
    rewritten = asked = failed = 0
    removed = _settle(conn)  # owed by a run that stopped before dropping them
    try:
        for repair in sorted(repairs, key=lambda r: r.right is None):
            right = repair.right
            if right is None:
                if not reextract or (limit and asked >= limit):
                    continue
                if not asked:
                    removed += _settle(conn)
                asked += 1
                email = stored_email(conn, repair.email_id, _header_named(repair.wrong))
                right, stop = _ask_model(email)
                if right is None:
                    failed += 1
                    if stop:
                        print(
                            "The model is out of quota or credentials; stopping.", file=sys.stderr
                        )
                        break
                    continue
                write_json_atomic(extraction_path(extracted, repair.message_id), right)
            # Redacted on the way in, as staging is: a file older than the redaction
            # may hold a credential scrub_secrets took out of the row.
            replace_extraction(
                conn,
                repair.email_id,
                stored_email(conn, repair.email_id),
                wrong=repair.wrong,
                right=redact_payload(right),
            )
            _owe(conn, repair.email_id)
            conn.commit()
            rewritten += 1
            if rewritten % SETTLE_EVERY == 0:
                removed += _settle(conn)
    except BaseException:
        conn.rollback()  # a rewrite cut short is not committed by the drop below
        raise
    finally:
        removed += _settle(conn)
    return {"rewritten": rewritten, "asked": asked, "failed": failed, "vectors_removed": removed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="rewrite; without it, report only")
    parser.add_argument(
        "--reextract", action="store_true", help="ask the model for emails whose file is lost"
    )
    parser.add_argument("--limit", type=int, default=0, help="most model calls (0: no limit)")
    args = parser.parse_args(argv)

    if args.apply and is_replica():
        print(replica_refusal("repairing case twins"), file=sys.stderr)
        return 2
    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    conn = get_connection(str(args.db))
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        repairs, counts, unexplained = plan(conn, EXTRACTED)
        print(" ".join(f"{name}={n}" for name, n in counts.items()))
        for repair in repairs[:SAMPLES]:
            right = "(lost)" if repair.right is None else _text(repair.right.get("summary"))[:70]
            print(
                f"  email {repair.email_id}: {_text(repair.wrong.get('summary'))[:70]!r}"
                f" -> {right!r}"
            )
        if unexplained:
            print(f"  unexplained, first {SAMPLES}: {unexplained[:SAMPLES]}")
        owed = _owed(conn)
        if owed:
            print(f"{len(owed)} rewritten emails still hold their old vectors; --apply drops them")
        if not args.apply:
            print("Report only; --apply rewrites.")
            return 0
        result = apply(conn, repairs, EXTRACTED, args.reextract, args.limit)
        print(" ".join(f"{name}={n}" for name, n in result.items()))
        return 1 if result["failed"] else 0
    finally:
        conn.close()


if __name__ == "__main__":
    # A stop (systemctl stop, kill) ends the run through apply's finally, which drops
    # the owed vectors, instead of where the default handler would leave it.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    raise SystemExit(main())
