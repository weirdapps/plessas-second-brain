#!/usr/bin/env python3
"""Repair the people rows that the old name and address rules left wrong.

find_or_create_person used to let the longer name win, and a garbled name (Greek
written out as GBK and read back as ISO-8859-10) is twice as long as the real
one. Seven rows kept one for good, the owner's and three direct reports' among
them, so every lookup by name missed the real record and found an email-less
fragment instead (schema-load-1, mcp-2). It also linked a sender whose address
was not on record to a same-named person without saving the address, which left
about 195 active senders unknown by address (db-integrity-1). The code no longer
does either; this repairs what it already did:

  (a) a garbled name becomes the Greek it stands for; failing that, the name
      the address's own emails were most often sent under; failing that, the
      canonical name data/canonical_people.json gives the address,
  (b) a person with no address, linked as 'sender' to emails from exactly one
      address that no other person holds, gets that address. An address two
      such people sent from is left alone: which of them it belongs to is not
      known.

Defaults to a dry run, which opens the database read-only and prints the
before/after table. --apply plans again and writes inside one transaction under
the loader's write lock (BEGIN IMMEDIATE), so a sync that is writing finishes
first and nothing it adds is missed. Run it on the producer with the writers
stopped, then run the people dedup (python -m src.store.dedup_people), which can
now merge the email-less fragments into the repaired records. On a replica
--apply is refused with exit 2, since the next pull replaces the file.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import DATA_ROOT, DEFAULT_DB, is_replica, replica_refusal  # noqa: E402
from src.store.normalizer import looks_garbled, recover_garbled_greek  # noqa: E402
from src.store.schema import get_connection  # noqa: E402

# How long --apply waits for another writer to finish. Tests shorten it.
BUSY_TIMEOUT_MS = 60000


def load_canonical(path: Path) -> dict[str, str]:
    """canonical_people.json as {address: canonical name}; empty when absent."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    return {
        e["email"].strip().lower(): e["canonical_name"]
        for e in entries
        if e.get("email") and e.get("canonical_name")
    }


def _usable(name: str | None) -> str | None:
    """A candidate name as it may be stored, or None when it is no name."""
    if not name or not name.strip() or "@" in name:
        return None
    name = recover_garbled_greek(name) or name.strip()
    return None if looks_garbled(name) else name


def plan_renames(
    conn: sqlite3.Connection, canonical: dict[str, str]
) -> tuple[list[tuple[int, str | None, str, str, str]], list[tuple[int, str | None, str]]]:
    """(id, address, before, after, source) for each garbled name, and those left as they are."""
    renames = []
    unmended = []
    for pid, name, email in conn.execute("SELECT id, name, email FROM people").fetchall():
        if not looks_garbled(name):
            continue
        after, source = recover_garbled_greek(name), "recovered"
        address = (email or "").strip().lower()
        if after is None and address:
            senders = conn.execute(
                """
                SELECT sender_name FROM emails
                WHERE LOWER(TRIM(sender_address)) = ? AND sender_name IS NOT NULL
                GROUP BY sender_name
                ORDER BY COUNT(*) DESC, sender_name
                """,
                (address,),
            ).fetchall()
            after = next((n for n in (_usable(s[0]) for s in senders) if n), None)
            source = "sender name"
        if after is None and address:
            after, source = _usable(canonical.get(address)), "canonical"
        if after is None:
            unmended.append((pid, email, name))
        else:
            renames.append((pid, email, name, after, source))
    return renames, unmended


def plan_backfills(conn: sqlite3.Connection) -> tuple[list[tuple[int, str, str]], int, int]:
    """(id, name, address) to set; and how many were held by others, or shared."""
    candidates = conn.execute(
        """
        SELECT p.id, p.name, MIN(LOWER(TRIM(e.sender_address))) AS address
        FROM people p
        JOIN email_people ep ON ep.person_id = p.id AND ep.role_in_email = 'sender'
        JOIN emails e ON e.id = ep.email_id
        WHERE p.email IS NULL AND e.sender_address LIKE '%@%'
        GROUP BY p.id
        HAVING COUNT(DISTINCT LOWER(TRIM(e.sender_address))) = 1
        """
    ).fetchall()
    claims: dict[str, int] = {}
    for _pid, _name, address in candidates:
        claims[address] = claims.get(address, 0) + 1
    backfills = []
    held = shared = 0
    for pid, name, address in candidates:
        if conn.execute("SELECT 1 FROM people WHERE LOWER(email) = ?", (address,)).fetchone():
            held += 1
        elif claims[address] > 1:
            shared += 1
        else:
            backfills.append((pid, name, address))
    return backfills, held, shared


def _report(renames, unmended, backfills, held, shared) -> None:
    print(f"Renamed: {len(renames)}")
    for pid, email, before, after, source in renames:
        print(f"  {pid:>7}  {email or '-'}\n           {before}\n        -> {after}  [{source}]")
    if unmended:
        print(f"Garbled, with nothing to replace it: {len(unmended)}")
        for pid, email, name in unmended:
            print(f"  {pid:>7}  {email or '-'}  {name}")
    print(f"Addresses backfilled: {len(backfills)}")
    for pid, name, address in backfills:
        print(f"  {pid:>7}  {name}: (none) -> {address}")
    print(f"  left alone: {held} held by another person, {shared} sent from by several")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the repairs (default: dry run)")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--canonical", default=str(DATA_ROOT / "canonical_people.json"))
    args = parser.parse_args(argv)
    if args.apply and is_replica():
        # The dry run reads only; the repair rewrites people rows, and a
        # replica's copy is replaced by the next pull (see src/config.py).
        print(replica_refusal("the people repair"), file=sys.stderr)
        return 2

    db = Path(args.db)
    if not db.exists():
        print(f"Error: no database at {db}", file=sys.stderr)
        return 2
    canonical = load_canonical(Path(args.canonical))

    if not args.apply:
        # as_uri escapes the path: pasted in raw, a '#' or '?' in a directory
        # name cut it short or rewrote the query, and the wrong file opened.
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            renames, unmended = plan_renames(conn, canonical)
            backfills, held, shared = plan_backfills(conn)
        finally:
            conn.close()
        print("people repair: DRY RUN (pass --apply to write)")
        _report(renames, unmended, backfills, held, shared)
        return 0

    conn = get_connection(str(db))
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            renames, unmended = plan_renames(conn, canonical)
            backfills, held, shared = plan_backfills(conn)
            for pid, _email, _before, after, _source in renames:
                conn.execute("UPDATE people SET name = ? WHERE id = ?", (after, pid))
            for pid, _name, address in backfills:
                conn.execute(
                    "UPDATE people SET email = ? WHERE id = ? AND email IS NULL", (address, pid)
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    print("people repair: APPLIED")
    _report(renames, unmended, backfills, held, shared)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
