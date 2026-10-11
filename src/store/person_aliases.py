"""Record which identity a mention row stands for: an alias, never a merge.

78% of the people rows are mentions: names extraction found in text, with no address,
Teams MRI or WhatsApp JID. One colleague was 20 rows, the identity and 19 spellings of
her name, so a name fitting all of them could not say which was meant (match_count 20)
and recall attached no dossier. This proposes, for each mention row, the identity it
stands for when two signals agree:

  * the names agree in transliteration: a shared surname in any script or common
    romanisation, and no word of one against the other (transliterate.names_agree),
    the words of the identity's address counting as its own;
  * the rows occur in the same threads: at least one, or at least half of the
    mention's threads when its name is a single word, since a row named only
    'Nikos' gathers every Nikos the model ever read.

A mention that two identities fit, both sharing its threads, is ambiguous and left
alone; so is anything already aliased, and the owner, whom src/store/owner.py
resolves on its own. --apply records each proposal as a sync_metadata row,
'person_alias:<mention id>' -> '<identity id>', which person_resolve.resolve_identity
reads. No people row changes, and no schema: the identity split of issue 170 is where
an alias table belongs, and these rows reach a replica with its next pull. Undo one
with DELETE FROM sync_metadata WHERE key = 'person_alias:<id>', or all of them with
key LIKE 'person_alias:%'.

    python -m src.store.person_aliases            # dry run: print the proposals
    python -m src.store.person_aliases --apply    # record them (refused on a replica)
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.config import DEFAULT_DB, is_replica, replica_refusal
from src.store.person_resolve import ALIAS_PREFIX, people_index, surname_fits
from src.store.query import thread_keys
from src.store.transliterate import name_words

# A mention named by one word must share at least this share of its threads with the
# identity: one word is anyone's.
_ONE_WORD_SHARE = 0.5


@dataclass
class AliasPlan:
    """The aliases to record, and how many mentions were left alone and why."""

    proposals: list[dict] = field(default_factory=list)
    ambiguous: int = 0  # two or more identities fit and share its threads
    unshared: int = 0  # identities fit by name, none shares enough of its threads


def _aliased(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT key FROM sync_metadata WHERE key >= ? AND key < ?",
        (ALIAS_PREFIX, ALIAS_PREFIX[:-1] + ";"),
    )
    out = set()
    for (key,) in rows:
        try:
            out.add(int(key[len(ALIAS_PREFIX) :]))
        except ValueError:
            continue
    return out


def _shared_threads(
    conn: sqlite3.Connection, mention_id: int, identity_ids: set[int]
) -> tuple[dict[int, int], int]:
    """How many of the mention's threads each identity also appears in, and how many
    threads the mention has. An email with no thread (query._THREAD: News, a blank
    subject) is a thread of its own."""
    emails = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT email_id FROM email_people WHERE person_id = ?", (mention_id,)
        )
    ]
    keys = thread_keys(conn, emails)
    threads = sorted({t for t in keys.values() if t})
    lone = sorted(e for e, t in keys.items() if not t)
    ids = json.dumps(sorted(identity_ids))
    shared: dict[int, set] = {}
    if threads:
        for pid, thread in conn.execute(
            "SELECT DISTINCT ep.person_id, e.conversation_id FROM emails e "
            "JOIN email_people ep ON ep.email_id = e.id "
            "WHERE e.conversation_id IN (SELECT value FROM json_each(?)) "
            "AND ep.person_id IN (SELECT value FROM json_each(?))",
            (json.dumps(threads), ids),
        ):
            shared.setdefault(pid, set()).add(thread)
    if lone:
        for pid, email_id in conn.execute(
            "SELECT DISTINCT person_id, email_id FROM email_people "
            "WHERE email_id IN (SELECT value FROM json_each(?)) "
            "AND person_id IN (SELECT value FROM json_each(?))",
            (json.dumps(lone), ids),
        ):
            shared.setdefault(pid, set()).add(email_id)
    return {pid: len(found) for pid, found in shared.items()}, len(threads) + len(lone)


def propose_aliases(conn: sqlite3.Connection) -> AliasPlan:
    """The mention rows that stand for one identity by name and by thread, unrecorded."""
    index = people_index(conn)
    aliased = _aliased(conn)
    plan = AliasPlan()
    for pid, name in index.names.items():
        if pid in index.kinds or pid in index.owner_rows or pid in aliased:
            continue
        words = name_words(name)
        fits = surname_fits(index, words) - index.owner_rows
        if not fits:
            continue
        shared, total = _shared_threads(conn, pid, fits)
        one_word = sum(not w.initial for w in words) < 2
        enough = {
            identity: n
            for identity, n in shared.items()
            if n and (not one_word or n >= _ONE_WORD_SHARE * total)
        }
        if len(enough) > 1:
            plan.ambiguous += 1
        elif not enough:
            plan.unshared += 1
        else:
            ((identity, n),) = enough.items()
            plan.proposals.append(
                {
                    "mention_id": pid,
                    "mention": name,
                    "identity_id": identity,
                    "identity": index.names[identity],
                    "email": index.emails.get(identity),
                    "shared_threads": n,
                    "mention_threads": total,
                }
            )
    return plan


def apply_aliases(conn: sqlite3.Connection, proposals: list[dict]) -> int:
    """Record each proposal as a person_alias row; an alias already recorded is kept.
    Returns how many were written. Changes no people row and commits nothing."""
    written = 0
    for p in proposals:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sync_metadata (key, value) VALUES (?, ?)",
            (f"{ALIAS_PREFIX}{p['mention_id']}", str(p["identity_id"])),
        )
        written += cur.rowcount
    return written


def _report(plan: AliasPlan) -> None:
    print(f"Aliases proposed: {len(plan.proposals)}")
    for p in plan.proposals:
        print(
            f"  {p['mention_id']:>7}  {p['mention']}\n"
            f"       -> {p['identity_id']}  {p['identity']} <{p['email']}>"
            f"  [{p['shared_threads']} of {p['mention_threads']} threads shared]"
        )
    print(f"  left alone: {plan.ambiguous} ambiguous, {plan.unshared} sharing too few threads")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the proposals (the default)")
    mode.add_argument("--apply", action="store_true", help="record them in sync_metadata")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args(argv)
    if args.apply and is_replica():
        # The dry run reads only; recording writes, and a replica's copy is
        # replaced by the next pull (see src/config.py).
        print(replica_refusal("recording person aliases"), file=sys.stderr)
        return 2
    db = Path(args.db)
    if not db.exists():
        print(f"Error: no database at {db}", file=sys.stderr)
        return 2

    if not args.apply:
        from src.store.sql_readonly import connect_read_only

        conn = connect_read_only(db)
        try:
            plan = propose_aliases(conn)
        finally:
            conn.close()
        print("person aliases: DRY RUN (pass --apply to record them)")
        _report(plan)
        return 0

    from src.store.schema import get_connection

    conn = get_connection(str(db))
    try:
        # Planned again under the write lock, so what is recorded is what the
        # store holds when it is written.
        conn.execute("BEGIN IMMEDIATE")
        try:
            plan = propose_aliases(conn)
            written = apply_aliases(conn, plan.proposals)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    print(f"person aliases: APPLIED ({written} recorded)")
    _report(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
