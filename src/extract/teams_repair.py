"""Mend what earlier Teams code stored: `brain maint teams-repair` and
`brain maint teams-empty-summaries`. No Teams call, no model call.

teams-repair, in one transaction (a dry run rolls it back, so its counts are exact):

1. Senders. Every channel post was stored with no sender MRI, so no person, because
   only a contacts URL was read and posts name their sender bare (4,817 on the
   replica of 2026-10-20). teams_export.fill_senders reads each payload and links
   the senders the MRI cache knows; the hourly resolve_mris looks up the rest.
2. Participants. teams_threads.participants was never written. Each thread gets the
   MRIs of who wrote in it (teams_threads.thread_participants), and the names beside
   them are read again, which step 1 can add to.
3. Titles. A thread was titled 'team / topic: <first message>', and only a channel
   has a team, so 7,109 titles began '? / '. Each is written again with the label
   extraction now uses (teams_pipeline.chat_label) and the same first message.
4. The MRI cache. Senders that are no person (a thread, an app, a meeting visitor)
   were written back as 'permanent_fail' rows: 2,311 chat IDs among 2,709 such rows.
   resolve_mris no longer selects them, and their rows are deleted.

Nothing is sent back to extraction. teams-empty-summaries is apart because it does:
76 threads extracted on 2026-05-04 kept an empty summary, so they have no vector.
Their decisions, actions and key facts (127 key facts) are deleted and the threads
go back to 'pending', which the next teams-sync extracts: one model call each.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from src.export.teams_export import fill_senders
from src.extract.teams_pipeline import _generate_title, chat_label, substantive_messages
from src.extract.teams_threads import thread_participants

# The prefix the old label gave every thread outside a channel.
PLACEHOLDER_TITLE = "? / "
# Item tables extraction writes per thread and deletes before it writes again.
THREAD_ITEMS = ("decisions", "action_items", "key_facts")


def fill_participants(conn: sqlite3.Connection) -> dict:
    """Write each thread's participants, and its names where they changed as a set."""
    counts = {"participants_set": 0, "names_set": 0}
    rows = conn.execute(
        "SELECT id, participants, participant_display_names FROM teams_threads ORDER BY id"
    ).fetchall()
    for tid, stored, stored_names in rows:
        participants = json.dumps(thread_participants(conn, tid))
        if participants != stored:
            conn.execute(
                "UPDATE teams_threads SET participants = ? WHERE id = ?", (participants, tid)
            )
            counts["participants_set"] += 1
        # As bound_threads reads them, in whatever order SQLite returns them; a run that
        # finds the same names in another order writes nothing.
        names = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT sender_display_name FROM teams_messages WHERE thread_id = ?"
                " AND is_system = 0 AND sender_display_name IS NOT NULL",
                (tid,),
            )
        ]
        try:
            held = set(json.loads(stored_names or "[]"))
        except ValueError:
            held = None
        if held != set(names):
            conn.execute(
                "UPDATE teams_threads SET participant_display_names = ? WHERE id = ?",
                (json.dumps(names), tid),
            )
            counts["names_set"] += 1
    return counts


def fix_titles(conn: sqlite3.Connection) -> dict:
    """Title again every thread whose title starts with the old placeholder."""
    counts = {"placeholder_titles": 0, "titles_fixed": 0}
    threads = conn.execute(
        """
        SELECT t.id, t.title, t.participant_display_names, c.team_name,
               c.topic AS chat_topic, c.chat_kind
        FROM teams_threads t JOIN teams_chats c ON c.id = t.chat_id
        WHERE substr(t.title, 1, ?) = ?
        ORDER BY t.id
        """,
        (len(PLACEHOLDER_TITLE), PLACEHOLDER_TITLE),
    ).fetchall()
    for thread in threads:
        counts["placeholder_titles"] += 1
        messages = conn.execute(
            "SELECT content_text, is_system FROM teams_messages WHERE thread_id = ?"
            " ORDER BY composed_at",
            (thread["id"],),
        ).fetchall()
        prompt_messages = [
            {"content": m["content_text"] or ""} for m in substantive_messages(messages)
        ]
        title = _generate_title({"chat_label": chat_label(thread)}, prompt_messages)
        if title != thread["title"]:
            conn.execute("UPDATE teams_threads SET title = ? WHERE id = ?", (title, thread["id"]))
            counts["titles_fixed"] += 1
    return counts


def forget_non_person_mris(conn: sqlite3.Connection) -> int:
    """Delete the cache rows of senders no person lookup can name; return how many."""
    return conn.execute(
        "DELETE FROM teams_mri_resolution WHERE status <> 'resolved' AND mri NOT LIKE '8:orgid:%'"
    ).rowcount


def repair(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Steps 1 to 4 in one transaction, rolled back when `dry_run`; their counts."""
    if conn.in_transaction:
        conn.commit()
    try:
        counts = fill_senders(conn)
        counts.update(fill_participants(conn))
        counts.update(fix_titles(conn))
        counts["mri_rows_deleted"] = forget_non_person_mris(conn)
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return counts


def reset_empty_summaries(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Send the extracted threads with an empty summary back to extraction, their items
    deleted first; a dry run counts. Returns the threads and the item rows of each table."""
    if conn.in_transaction:
        conn.commit()
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM teams_threads WHERE extraction_status = 'extracted'"
            " AND COALESCE(TRIM(summary), '') = '' ORDER BY id"
        )
    ]
    counts = {"threads": len(ids)}
    marks = ",".join("?" * len(ids))
    try:
        for table in THREAD_ITEMS:
            counts[table] = (
                conn.execute(
                    f"DELETE FROM {table} WHERE teams_thread_id IN ({marks})", ids
                ).rowcount
                if ids
                else 0
            )
        if ids:
            conn.execute(
                "UPDATE teams_threads SET extraction_status = 'pending', extraction_error = NULL"
                f" WHERE id IN ({marks})",
                ids,
            )
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return counts


def _open(args) -> sqlite3.Connection | None:
    from src.store.schema import get_connection

    if not Path(args.db).exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        return None
    return get_connection(str(args.db))


def cmd_teams_repair(args) -> int:
    conn = _open(args)
    if conn is None:
        return 1
    try:
        c = repair(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print(f"teams-repair: {'dry run, nothing written' if args.dry_run else 'applied'}")
    print(
        f"  senders: {c['senders_filled']:,} filled from payloads,"
        f" {c['without_sender']:,} with none to read"
    )
    print(
        f"  people: {c['persons_linked']:,} messages linked, {c['names_filled']:,} names filled"
        " (the hourly teams-sync resolves the senders not yet known)"
    )
    print(
        f"  threads: {c['participants_set']:,} participants written,"
        f" {c['names_set']:,} name lists read again"
    )
    print(
        f"  titles: {c['titles_fixed']:,} of {c['placeholder_titles']:,} '? / ' titles written"
        " again"
    )
    print(f"  MRI cache: {c['mri_rows_deleted']:,} rows of senders that are no person deleted")
    return 0


def cmd_teams_empty_summaries(args) -> int:
    conn = _open(args)
    if conn is None:
        return 1
    try:
        c = reset_empty_summaries(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    verb = "would go" if args.dry_run else "went"
    print(
        f"teams-empty-summaries: {c['threads']:,} threads {verb} back to extraction;"
        f" {c['key_facts']:,} key facts, {c['decisions']:,} decisions and"
        f" {c['action_items']:,} actions {'to delete' if args.dry_run else 'deleted'}"
    )
    if c["threads"] and not args.dry_run:
        print("  The next teams-sync extracts them, one model call each.")
    return 0


def add_maint_parsers(maint_subparsers) -> None:
    """The two commands, under `brain maint`."""
    for name, func, help_text in (
        (
            "teams-repair",
            cmd_teams_repair,
            "Fill Teams senders, thread participants and titles; clean the MRI cache"
            " (no Teams or model call)",
        ),
        (
            "teams-empty-summaries",
            cmd_teams_empty_summaries,
            "Send the Teams threads extracted with an empty summary back to extraction",
        ),
    ):
        parser = maint_subparsers.add_parser(
            name,
            help=help_text,
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("--dry-run", action="store_true", help="Count only: nothing is written")
        parser.set_defaults(func=func)
