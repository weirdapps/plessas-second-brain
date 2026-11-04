"""`brain maint people-review` and `people-unmerge` (issue 170), apart from
src/store/person_identity.py so that the modules the loader and the MCP server reach never
reach the CLI.

people-review lists the name-only matches the people dedup queued, and clears them in one
command: --merge-all, --reject-all, or --merge / --reject with the ids to act on. Every
merge it makes is logged in person_merges; people-unmerge ID undoes one.
"""

import sys

from src.store import person_identity


def _open(args):
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    if not person_identity.active(conn):
        conn.close()
        print("No review queue: run `brain maint migrate --apply-gated` first.")
        return None
    return conn


def _show(rows) -> None:
    if not rows:
        print("people-review: nothing waiting")
        return
    print(f"people-review: {len(rows)} waiting (merge keeps the first row)")
    for r in rows:
        a = f"#{r['a']} {r['a_name']}" + (f" <{r['a_email']}>" if r["a_email"] else "")
        b = f"#{r['b']} {r['b_name']}" + (f" <{r['b_email']}>" if r["b_email"] else "")
        print(f"  [{r['id']}] {r['rule']}: {a}  <-  {b}  ({r['evidence'] or ''})")


def cmd_people_review(args) -> int:
    conn = _open(args)
    if conn is None:
        return 1
    try:
        waiting = person_identity.pending(conn)
        if args.merge_all:
            merges = [r["id"] for r in waiting]
            rejects: list[int] = []
        elif args.reject_all:
            merges, rejects = [], [r["id"] for r in waiting]
        else:
            merges, rejects = list(args.merge or []), list(args.reject or [])
        if not merges and not rejects:
            _show(waiting)
            return 0
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            merged = sum(person_identity.decide(conn, i, "merge") is not None for i in merges)
            for i in rejects:
                person_identity.decide(conn, i, "reject")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        print(
            f"people-review: {merged} merged, {len(rejects)} rejected, "
            f"{len(person_identity.pending(conn))} still waiting"
        )
    finally:
        conn.close()
    sys.stdout.flush()
    return 0


def cmd_people_unmerge(args) -> int:
    conn = _open(args)
    if conn is None:
        return 1
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            back = person_identity.unmerge(conn, args.merge_id)
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            print(f"people-unmerge: {exc}", file=sys.stderr)
            return 1
        except BaseException:
            conn.rollback()
            raise
        print(f"people-unmerge: merge {args.merge_id} undone, the row is #{back} again")
    finally:
        conn.close()
    return 0


def add_maint_parsers(maint_subparsers) -> None:
    parser = maint_subparsers.add_parser(
        "people-review",
        help="List the name-only people matches waiting for review, or clear them "
        "(--merge-all, --reject-all, --merge ID..., --reject ID...)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--merge-all", action="store_true", help="Merge every pair waiting")
    group.add_argument("--reject-all", action="store_true", help="Reject every pair waiting")
    parser.add_argument("--merge", type=int, nargs="+", metavar="ID", help="Merge these pairs")
    parser.add_argument("--reject", type=int, nargs="+", metavar="ID", help="Reject these pairs")
    parser.set_defaults(func=cmd_people_review)
    parser = maint_subparsers.add_parser(
        "people-unmerge", help="Undo one logged people merge (person_merges id)"
    )
    parser.add_argument("merge_id", type=int, help="The person_merges id")
    parser.set_defaults(func=cmd_people_unmerge)
