"""Inbox→Archive move detection.

The hourly outlook-cli sync uses a `--since` cursor to fetch new arrivals.
That handles new emails but cannot detect MOVES — when the user triages an
email out of Inbox into Archive (manually or via /triage-inbox), the email's
`ReceivedDateTime` is unchanged and falls below the cursor. The DB row stays
labelled `mailbox_name='Inbox'` forever.

Reconciliation: list Outlook's *current* Inbox via the API, then any DB row
labelled `mailbox_name='Inbox'` that is no longer there, by Graph id or by
RFC822 Message-ID, has been moved out. We assume → Archive (matches the
dominant /triage-inbox flow). Subfolder routing would mislabel — accept that
until it bites.

Deletion is not a move to Archive, and assuming it was kept mail the owner
deleted while calling it archived. So each run also lists Deleted Items and
Junk Email (one call each) and records, for every stored email found there by
Graph id or Message-ID (a move mints a new Graph id), where it is and when it
was first seen deleted: a sync_metadata row keyed `mail_location:<message_id>`
holding JSON {location, deleted_at, internet_message_id}. Such a row is never
relabelled Archive, even after the folder purges it, and its record is removed
once the mail is back in the Inbox. No row is deleted: what to forget is the
retention policy's decision. Matched by Message-ID, a record says that a copy
with that Message-ID is there: mail sent with a copy to oneself keeps a second
copy in Sent Items, which this does not list, so deleting the Inbox copy records
the email as deleted. A forget policy has to allow for that.

Runs after every hourly Inbox sync (~1s — current Inbox is tiny). Exits 2,
touching nothing, on a replica (see src.config.is_replica).

Exit codes follow the estate's M365 convention, which the wrapper and its
operators read: 4 means re-authenticate, 5 means outlook-cli or the service
behind it misbehaved, and 75 (EX_TEMPFAIL) means a local, transient failure
such as a locked database.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from src.config import DEFAULT_DB, is_replica, replica_refusal
from src.export.mail_reconcile import normalize_imid
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError, run_outlook_cli
from src.store.schema import get_connection

# Exit code for a run refused on a replica, the same as src.cli's. It was 5,
# which every M365 wrapper here reads as "upstream misbehaved".
REFUSED_ON_REPLICA = 2

# sync_metadata key prefix of the per-email location record (see the docstring).
LOCATION_KEY = "mail_location:"
DELETED_ITEMS = "Deleted Items"
# Listed beside the Inbox: the well-known alias outlook-cli resolves without a
# folder lookup, and the name recorded. Later entries win, so mail in both reads
# as deleted.
ELSEWHERE_FOLDERS = (("JunkEmail", "Junk Email"), ("DeletedItems", DELETED_ITEMS))

logger = logging.getLogger(__name__)


def list_folder_ids(folder: str, max_results: int = 5000) -> tuple[set[str], set[str]]:
    """Return (outlook_ids, internet_message_ids) currently in `folder`: one call."""
    # Through the shared adapter, like every other outlook-cli caller: it adds
    # --no-auto-reauth, so a timer never opens an interactive login, honours
    # OUTLOOK_CLI_PATH, and raises OutlookCliAuthRequired on exit 4.
    items = run_outlook_cli(
        [
            "list-mail",
            "--folder",
            folder,
            "--select",
            "Id,InternetMessageId",
            "--all",
            "--max",
            str(max_results),
        ],
        timeout_sec=120,
    )
    outlook_ids = {m["Id"] for m in items if m.get("Id")}
    internet_ids = {m["InternetMessageId"] for m in items if m.get("InternetMessageId")}
    return outlook_ids, internet_ids


def list_current_inbox_ids(max_results: int = 5000) -> tuple[set[str], set[str]]:
    """Return (outlook_ids, internet_message_ids) currently in Outlook's Inbox.

    The Outlook REST `Id` is what outlook-cli-sourced rows store in
    `emails.message_id`. The `InternetMessageId` is the RFC822 Message-ID,
    which the loader populates in `emails.internet_message_id` regardless
    of source — so we can cross-match AppleScript-sourced rows too.
    """
    return list_folder_ids("Inbox", max_results=max_results)


def reconcile_moves(
    db_path: Path,
    target_mailbox: str = "Archive",
    max_results: int = 5000,
    track_elsewhere: bool = False,
) -> dict:
    """Mark DB rows labelled 'Inbox' that aren't in the live Inbox as moved.

    Two passes:
      1. outlook-cli-sourced rows (AAMk... ids), present by `message_id` or,
         under the new Graph id a move back mints, by RFC822 Message-ID
      2. Match by `internet_message_id` (AppleScript-sourced legacy rows
         that have a numeric message_id but the same RFC822 Message-ID
         as one of the live Outlook entries).

    With `track_elsewhere`, which main() passes, Deleted Items and Junk Email
    are listed as well, and the stored mail found there is recorded where it is
    instead of being relabelled (see the module docstring).

    Returns a summary: {scanned_inbox, by_outlook_id, by_internet_id, moved},
    and with `track_elsewhere` {deleted, junk, restored, located}: the records
    written and removed this run, and how many emails carry one.
    """
    outlook_ids, internet_ids = list_current_inbox_ids(max_results=max_results)

    # Refuse an implausible listing before touching a single row. This function
    # relabels by ABSENCE, so a listing that is empty or truncated does not mean
    # "the inbox is empty", it means "we could not see the inbox", and the two
    # produce opposite actions from identical input. outlook-cli answers 0 with
    # an empty list on several non-fault paths (a throttled tenant, an expired
    # session on a code path that does not map to exit 4, a wrong folder name),
    # and the relabel is one-way with no undo. outlook_export.py already refuses
    # on exactly this shape.
    if not outlook_ids:
        return {
            "scanned_inbox": 0,
            "by_outlook_id": 0,
            "by_internet_id": 0,
            "moved": 0,
            "status": "refused-empty-listing",
            "detail": (
                "outlook-cli returned no Inbox messages. Treating that as an "
                "unreadable inbox, not an empty one; nothing was relabelled."
            ),
        }
    if len(outlook_ids) >= max_results:
        return {
            "scanned_inbox": len(outlook_ids),
            "by_outlook_id": 0,
            "by_internet_id": 0,
            "moved": 0,
            "status": "refused-truncated-listing",
            "detail": (
                f"listing hit the {max_results} cap, so absence from it proves "
                "nothing. Raise --max-results and re-run."
            ),
        }

    # Listed only once the Inbox listing is known to be usable.
    elsewhere = (
        {location: list_folder_ids(folder, max_results) for folder, location in ELSEWHERE_FOLDERS}
        if track_elsewhere
        else None
    )
    inbox_keys = {normalize_imid(i) for i in internet_ids} - {""}

    conn = get_connection(str(db_path))
    try:
        located: set[str] = set()
        changes: dict[str, int] = {}
        if elsewhere is not None:
            located, changes = _note_locations(conn, outlook_ids, inbox_keys, elsewhere)

        # Pass 1 — outlook-cli-sourced rows.
        rows = conn.execute(
            "SELECT message_id, internet_message_id FROM emails"
            " WHERE mailbox_name = 'Inbox' AND message_id LIKE 'AAMk%'"
        ).fetchall()
        by_outlook_id = [
            r[0]
            for r in rows
            if r[0] not in outlook_ids
            and normalize_imid(r[1]) not in inbox_keys
            and str(r[0]) not in located
        ]

        # Pass 2 — AppleScript-sourced rows: match on internet_message_id.
        rows = conn.execute(
            "SELECT message_id, internet_message_id FROM emails "
            "WHERE mailbox_name = 'Inbox' "
            "AND message_id NOT LIKE 'AAMk%' "
            "AND message_id NOT LIKE '-%' "
            "AND internet_message_id IS NOT NULL"
        ).fetchall()
        by_internet_id = [
            r[0]
            for r in rows
            if normalize_imid(r[1]) not in inbox_keys and str(r[0]) not in located
        ]

        moved = by_outlook_id + by_internet_id
        if moved:
            # Chunk the UPDATE — sqlite default parameter limit ~999.
            chunk = 500
            for i in range(0, len(moved), chunk):
                batch = moved[i : i + chunk]
                placeholders = ",".join("?" * len(batch))
                conn.execute(
                    f"UPDATE emails SET mailbox_name = ? WHERE message_id IN ({placeholders})",
                    [target_mailbox, *batch],
                )
        # The relabel and the location records land together.
        conn.commit()
    finally:
        conn.close()

    summary = {
        "scanned_inbox": len(outlook_ids),
        "by_outlook_id": len(by_outlook_id),
        "by_internet_id": len(by_internet_id),
        "moved": len(moved),
        "status": "ok",
    }
    if elsewhere is not None:
        summary.update(changes, located=len(located))
    return summary


def _chunks(values: list, size: int = 500) -> Iterator[list]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _note_locations(
    conn: sqlite3.Connection,
    inbox_ids: set[str],
    inbox_keys: set[str],
    elsewhere: dict[str, tuple[set[str], set[str]]],
) -> tuple[set[str], dict[str, int]]:
    """Record which stored emails sit in Deleted Items or Junk Email, and forget
    those back in the Inbox.

    A move mints a new Graph id, so a listed message is matched to its row by
    Graph id, an alias of it, or its RFC822 Message-ID compared without brackets
    or case. A record is written when an email is first seen in a folder; a
    message with a copy in the Inbox is where that copy is. Returns the
    message_ids that carry a record after this run, and what changed.
    """
    by_id: dict[str, str] = {}
    by_key: dict[str, str] = {}
    for location, (ids, imids) in elsewhere.items():
        by_id.update(dict.fromkeys(ids, location))
        by_key.update(dict.fromkeys((normalize_imid(i) for i in imids), location))
    by_key.pop("", None)

    # Every stored Message-ID, through its covering index: the column sits after
    # the body, which reading it from the table would page through.
    key_of: dict[int, str] = {}
    for email_id, imid in conn.execute(
        "SELECT id, internet_message_id FROM emails WHERE internet_message_id IS NOT NULL"
    ):
        key = normalize_imid(imid)
        if key:
            key_of[email_id] = key
    found = {email_id: by_key[key] for email_id, key in key_of.items() if key in by_key}
    has_aliases = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_aliases'"
    ).fetchone()
    for chunk in _chunks(sorted(by_id)):
        marks = ",".join("?" * len(chunk))
        for email_id, message_id in conn.execute(
            f"SELECT id, message_id FROM emails WHERE message_id IN ({marks})", chunk
        ):
            found[email_id] = by_id[str(message_id)]
        if has_aliases:
            for email_id, alias in conn.execute(
                f"SELECT email_id, message_id FROM email_aliases WHERE message_id IN ({marks})",
                chunk,
            ):
                found[email_id] = by_id[str(alias)]
    message_id_of: dict[int, str] = {}
    for chunk in _chunks(sorted(found)):
        marks = ",".join("?" * len(chunk))
        for email_id, message_id in conn.execute(
            f"SELECT id, message_id FROM emails WHERE id IN ({marks})", chunk
        ):
            message_id_of[email_id] = str(message_id)

    records: dict[str, dict] = {}
    for meta_key, value in conn.execute(
        "SELECT key, value FROM sync_metadata WHERE key GLOB ?", (LOCATION_KEY + "*",)
    ):
        try:
            loaded = json.loads(value)
        except ValueError:
            loaded = None
        records[meta_key[len(LOCATION_KEY) :]] = loaded if isinstance(loaded, dict) else {}

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    changes = {"deleted": 0, "junk": 0, "restored": 0}
    for email_id, location in found.items():
        message_id = message_id_of.get(email_id)
        imid_key = key_of.get(email_id)
        if message_id is None or message_id in inbox_ids or (imid_key and imid_key in inbox_keys):
            continue
        if records.get(message_id, {}).get("location") == location:
            continue
        record = {
            "location": location,
            "deleted_at": now if location == DELETED_ITEMS else None,
            "internet_message_id": imid_key,
        }
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
            (LOCATION_KEY + message_id, json.dumps(record)),
        )
        records[message_id] = record
        changes["deleted" if location == DELETED_ITEMS else "junk"] += 1

    # Back in the Inbox, by either id: no longer deleted or junk.
    for message_id, record in list(records.items()):
        if message_id in inbox_ids or (record.get("internet_message_id") or "") in inbox_keys:
            conn.execute("DELETE FROM sync_metadata WHERE key = ?", (LOCATION_KEY + message_id,))
            del records[message_id]
            changes["restored"] += 1
    return set(records), changes


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Inbox→Archive moves")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
    )
    parser.add_argument(
        "--target-mailbox",
        default="Archive",
        help="What to label messages no longer in live Inbox (default: Archive)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if is_replica():
        # It relabels emails, and a replica's copy is replaced by the next pull
        # (see src/config.py).
        logger.error(replica_refusal("the Inbox reconcile"))
        return REFUSED_ON_REPLICA

    if not args.db.exists():
        logger.error("DB not found: %s", args.db)
        return 1

    try:
        result = reconcile_moves(args.db, target_mailbox=args.target_mailbox, track_elsewhere=True)
    # An auth loss used to exit 2 and a locked database 4, so the wrapper
    # reported lock contention as "re-authenticate" and a real auth loss as
    # something else.
    except OutlookCliAuthRequired as e:
        logger.error("outlook-cli needs re-authentication: %s", e.stderr)
        return 4
    except OutlookCliError as e:
        logger.error("outlook-cli failed: rc=%d stderr=%s", e.exit_code, e.stderr)
        return 5
    except subprocess.TimeoutExpired:
        logger.error("outlook-cli timed out listing Inbox")
        return 5
    except (json.JSONDecodeError, sqlite3.Error) as e:
        logger.error("reconcile failed: %s", e)
        return 75

    logger.info(
        "Reconciled: live_inbox=%d moved=%d (by_outlook_id=%d by_internet_id=%d) → %s",
        result["scanned_inbox"],
        result["moved"],
        result["by_outlook_id"],
        result["by_internet_id"],
        args.target_mailbox,
    )
    if "located" in result:
        logger.info(
            "Deleted Items and Junk: %d newly deleted, %d newly in Junk, %d back in the Inbox;"
            " %d emails recorded",
            result["deleted"],
            result["junk"],
            result["restored"],
            result["located"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
