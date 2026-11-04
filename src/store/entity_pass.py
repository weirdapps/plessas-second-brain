"""The consolidation pass per entity (issue 168): one item, asked in several threads.

The consolidation pass (src/store/consolidate.py) takes one thread at a time, so the same
request sent twice in two threads, or a decision reported in two separate mails, stays two
current items. Schema v42 gives each item its counterpart (ledger.py
counterpart_person_id) and a hash of its normalised text, which make that case a rule:

- Only items whose counterpart a hard identifier found (ledger counterpart_via 'address':
  the parent email's From address, or a commitment party with that address), as the
  owner decided for people in issue 170. A counterpart matched by name alone keeps its
  counterpart_person_id for display and is never consolidated; such pairs are counted as
  name_only_candidates and left alone.
- Items of one kind (decision, action, commitment), current (consolidate.current_sql), with
  the same item_hash, the same ledger status (an open action never joins a done one) and
  the same counterpart entity are one item when they fall within ENTITY_WINDOW_DAYS of
  the first of them, by observed_at. The counterpart entity is the entity a people row is
  an alias of (entity_aliases kind 'person'), else the people row itself, so two rows of
  one person agree.
- The earliest stays; each later one gets canonical_id, logged in item_labels as a label
  with method 'entity', its prior values, thread_key 'entity:<key>' and the run's id, so
  `brain maint consolidate --revert RUN_ID` puts a run back. Nothing is deleted.
- An item from a mail the owner sent is never made the duplicate of one from someone
  else's (as in the thread pass). Outside the window the same text is a request made again
  (a monthly report), not a duplicate.

The hourly sync (consolidate.run_in_sync) takes the items stored since its last run; its
first run records where it starts and takes nothing older. `brain maint
consolidate-entities` is the backfill over every item, a dry run unless --apply. Nothing
runs on a store without the ledger (v42 is gated, src/store/schema.py).
"""

import json
import sqlite3
import sys
import uuid
from datetime import UTC, date, datetime

from src.store import ledger

ENTITY_WINDOW_DAYS = 30
HIGH_WATER_KEY = "entity_pass_high_water"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _items(conn: sqlite3.Connection, table: str, since: int, via: str = "address") -> list[dict]:
    """The current items of `table` whose counterpart was found `via` ('address', a hard
    identifier, or 'name'), each with its entity key, parent, status, date and whether the
    owner sent it; only hashes some item past `since` holds. None on a store whose rows
    say nothing of how their counterpart was found."""
    from src import config
    from src.store import consolidate
    from src.store.owner import is_owner_address

    if "counterpart_via" not in ledger._columns(conn, table):
        return []
    sender = (
        "(SELECT e.sender_address FROM emails e WHERE e.id = x.email_id)"
        if "email_id" in ledger._columns(conn, table)
        else "NULL"
    )
    sql = (
        f"SELECT x.id, x.item_hash, x.observed_at,"
        f" {ledger._parent_sql(conn, table, 'x')} AS parent,"
        f" {ledger._lifecycle_sql(table, 'x')} AS status,"
        f" COALESCE((SELECT 'e' || a.entity_id FROM entity_aliases a WHERE a.kind = 'person'"
        f" AND a.value = CAST(x.counterpart_person_id AS TEXT)),"
        f" 'p' || x.counterpart_person_id) AS entity, {sender} AS sender_address"
        f" FROM {table} x WHERE x.counterpart_via = ? AND x.item_hash IS NOT NULL"
        f" AND {consolidate.current_sql(conn, 'x', table)} AND x.item_hash IN (SELECT y.item_hash FROM {table} y"
        f" WHERE y.id > ? AND y.counterpart_via = ?)"
        f" ORDER BY x.observed_at, x.id"
    )
    cur = conn.execute(sql, (via, since, via))
    names = [d[0] for d in cur.description]
    items = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    for item in items:
        item["mine"] = is_owner_address(item["sender_address"], config.USER_EMAIL_PATTERN)
    return items


def _label(conn, table: str, item: dict, target: dict, run_id: str) -> None:
    prior = {"canonical_id": None, "superseded_by": None, "invalid_at": None}
    conn.execute(f"UPDATE {table} SET canonical_id = ? WHERE id = ?", (target["id"], item["id"]))
    conn.execute(
        "INSERT INTO item_labels (item_table, item_id, label, target_id, evidence, prior,"
        " method, thread_key, run_id, created_at)"
        " VALUES (?, ?, 'duplicate_of', ?, ?, ?, 'entity', ?, ?, ?)",
        (
            table,
            item["id"],
            target["id"],
            f"same text, same counterpart, {target['parent']} and {item['parent']}",
            json.dumps(prior),
            f"entity:{item['entity']}",
            run_id,
            _now(),
        ),
    )


def _plan(items: list[dict], since: int) -> list[tuple[dict, dict]]:
    """(duplicate, canonical) pairs: within each group, by date, an item within the window of
    its cluster's first joins it; past the window a new cluster starts, so a request made
    again every month stays one current item a month."""
    groups: dict[tuple, list[dict]] = {}
    for item in items:
        groups.setdefault((item["entity"], item["item_hash"], item["status"]), []).append(item)
    pairs = []
    for members in groups.values():
        head: dict | None = None
        start: date | None = None
        for item in members:
            seen = _day(item["observed_at"])
            if (
                head is None
                or seen is None
                or start is None
                or (seen - start).days > ENTITY_WINDOW_DAYS
            ):
                head, start = item, seen
                continue
            if item["id"] <= since or item["parent"] == head["parent"]:
                continue
            if item["mine"] and not head["mine"]:
                continue
            pairs.append((item, head))
    return pairs


def run(conn: sqlite3.Connection, since: dict[str, int] | None) -> dict:
    """Label the duplicates of items newer than `since` (the largest id seen per table; None
    for every item). The caller commits."""
    if not ledger.enabled(conn):
        return {"enabled": False}
    run_id = f"entity-{uuid.uuid4().hex[:12]}"
    labels = candidates = 0
    by_table: dict[str, int] = {}
    for table in ledger.TABLES:
        mark = (since or {}).get(table, 0)
        pairs = _plan(_items(conn, table, mark), mark)
        for item, target in pairs:
            _label(conn, table, item, target, run_id)
        by_table[table] = len(pairs)
        labels += len(pairs)
        # The same rule on counterparts matched by name alone: counted for review, never
        # written (issue 170).
        candidates += len(_plan(_items(conn, table, mark, via="name"), mark))
    return {
        "enabled": True,
        "run_id": run_id,
        "labels": labels,
        "by_table": by_table,
        "name_only_candidates": candidates,
    }


def _max_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0]
        for t in ledger.TABLES
    }


def run_hourly(conn: sqlite3.Connection) -> dict:
    """The sync's pass over the items stored since its last run. The caller commits."""
    if not ledger.enabled(conn):
        return {"enabled": False}
    row = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = ?", (HIGH_WATER_KEY,)
    ).fetchone()
    marks = _max_ids(conn)
    try:
        since = json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        since = None
    out = {"enabled": True, "started_from_scratch": since is None, "labels": 0}
    if since is not None:
        out["labels"] = run(conn, since)["labels"]
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        (HIGH_WATER_KEY, json.dumps(marks)),
    )
    return out


def cmd_consolidate_entities(args) -> int:
    """`brain maint consolidate-entities`: the backfill over every item, a dry run (rolled
    back) unless --apply."""
    from src.config import is_replica, replica_refusal
    from src.store.schema import get_connection

    if args.apply and is_replica():
        print(replica_refusal("consolidate-entities --apply"), file=sys.stderr)
        return 2
    conn = get_connection(str(args.db))
    try:
        if not ledger.enabled(conn):
            print("No item ledger: run `brain maint migrate --ledger` first (schema v42).")
            return 1
        result = run(conn, None)
        detail = ", ".join(f"{t} {n}" for t, n in result["by_table"].items())
        if args.apply:
            conn.commit()
            print(
                f"{result['labels']} labels ({detail}); revert: brain maint consolidate"
                f" --revert {result['run_id']}"
            )
        else:
            conn.rollback()
            print(f"dry run: {result['labels']} labels ({detail}); --apply writes them")
        print(
            f"{result['name_only_candidates']} more pairs match on a name alone: never"
            " written (issue 170, hard identifiers only)"
        )
    finally:
        conn.close()
    return 0


def add_maint_parser(maint_subparsers) -> None:
    parser = maint_subparsers.add_parser(
        "consolidate-entities",
        help="Mark the same item asked in several threads of one counterpart as one "
        "(schema v42; dry run unless --apply)",
        description=__doc__,
    )
    parser.add_argument("--apply", action="store_true", help="Write the labels")
    parser.set_defaults(func=cmd_consolidate_entities)
