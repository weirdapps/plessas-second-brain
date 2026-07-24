"""Consolidate duplicate topics that differ only by separators / case / accents.

`normalize_topic` now canonicalizes new topics at insert time; this one-time pass
merges the *existing* rows created under the old, looser normalization (which kept
"cards-migration", "cards migration" and "cards_migration" as three topics — the
cause of ~53% single-use topic rows).

Safe by construction: topics are grouped by the same `normalize_topic` used for
lookups, so a merge only ever collapses genuine formatting variants of one topic.
Runs only under an explicit invocation (no cron); `--dry-run` rolls back.
"""

import sqlite3
from collections import defaultdict
from pathlib import Path

from src.store.normalizer import normalize_topic
from src.store.schema import get_connection

REPO_ROOT = Path(__file__).parent.parent.parent
DB_PATH = REPO_ROOT / "data" / "brain.db"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def merge_topic(conn: sqlite3.Connection, keep_id: int, remove_id: int) -> None:
    """Reassign every reference to a topic onto keep_id, then delete it.

    Covers both junction tables that reference topics(id) — email_topics and
    conversation_topics (the latter only exists once its migration has run) — plus
    the self-referential parent_id, so the final DELETE never trips the FK.
    """
    conn.execute(
        "UPDATE OR IGNORE email_topics SET topic_id = ? WHERE topic_id = ?",
        (keep_id, remove_id),
    )
    conn.execute("DELETE FROM email_topics WHERE topic_id = ?", (remove_id,))
    if _table_exists(conn, "conversation_topics"):
        conn.execute(
            "UPDATE OR IGNORE conversation_topics SET topic_id = ? WHERE topic_id = ?",
            (keep_id, remove_id),
        )
        conn.execute("DELETE FROM conversation_topics WHERE topic_id = ?", (remove_id,))
    conn.execute("UPDATE topics SET parent_id = ? WHERE parent_id = ?", (keep_id, remove_id))
    conn.execute("DELETE FROM topics WHERE id = ?", (remove_id,))


def _usage_counts(conn: sqlite3.Connection) -> dict[int, int]:
    return {
        r[0]: r[1]
        for r in conn.execute("SELECT topic_id, COUNT(*) FROM email_topics GROUP BY topic_id")
    }


def run_topic_dedup(db_path: str | None = None, dry_run: bool = False) -> dict:
    """Merge separator/case/accent variants of the same topic. Returns stats."""
    conn = get_connection(db_path or str(DB_PATH))
    before = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    usage = _usage_counts(conn)

    groups: dict[str, list] = defaultdict(list)
    for t in conn.execute("SELECT id, name, display_name FROM topics"):
        canon = normalize_topic(t["name"])
        if canon:
            groups[canon].append(t)

    merged = 0
    rekeyed = 0
    for canon, members in groups.items():
        if len(members) == 1:
            # Lone topic whose stored name isn't canonical yet — re-key it so future
            # find_or_create_topic lookups (which use normalize_topic) hit this row.
            if members[0]["name"] != canon:
                conn.execute(
                    "UPDATE OR IGNORE topics SET name = ? WHERE id = ?",
                    (canon, members[0]["id"]),
                )
                rekeyed += 1
            continue
        # Keep the most-used variant (tiebreak: lowest id); best display name = longest.
        keep = max(members, key=lambda t: (usage.get(t["id"], 0), -t["id"]))
        best_display = max((m["display_name"] or m["name"] for m in members), key=len)
        for m in members:
            if m["id"] != keep["id"]:
                merge_topic(conn, keep["id"], m["id"])
                merged += 1
        conn.execute(
            "UPDATE OR IGNORE topics SET name = ?, display_name = ? WHERE id = ?",
            (canon, best_display, keep["id"]),
        )

    after = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    result = {"before": before, "after": after, "merged": merged, "rekeyed": rekeyed}

    if dry_run:
        conn.rollback()
        print(f"DRY RUN — {result} (rolled back)")
    else:
        conn.commit()
        print(f"Topic dedup: {result}")
    conn.close()
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Consolidate duplicate topics")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_topic_dedup(args.db, args.dry_run)
