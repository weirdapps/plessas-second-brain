"""Inbox→Archive move detection.

The hourly outlook-cli sync uses a `--since` cursor to fetch new arrivals.
That handles new emails but cannot detect MOVES — when the user triages an
email out of Inbox into Archive (manually or via /triage-inbox), the email's
`ReceivedDateTime` is unchanged and falls below the cursor. The DB row stays
labelled `mailbox_name='Inbox'` forever.

Reconciliation: list Outlook's *current* Inbox via the API, then any DB row
labelled `mailbox_name='Inbox'` whose `message_id` is no longer there has
been moved out. We assume → Archive (matches the dominant /triage-inbox
flow). Subfolder routing would mislabel — accept that until it bites.

Runs after every hourly Inbox sync (~1s — current Inbox is tiny).
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3  # for sqlite3.Error exception catch only
import subprocess
import sys
from pathlib import Path

from src.store.schema import get_connection

logger = logging.getLogger(__name__)


def list_current_inbox_ids(max_results: int = 5000) -> tuple[set[str], set[str]]:
    """Return (outlook_ids, internet_message_ids) currently in Outlook's Inbox.

    The Outlook REST `Id` is what outlook-cli-sourced rows store in
    `emails.message_id`. The `InternetMessageId` is the RFC822 Message-ID,
    which the loader populates in `emails.internet_message_id` regardless
    of source — so we can cross-match AppleScript-sourced rows too.
    """
    result = subprocess.run(
        [
            "outlook-cli",
            "list-mail",
            "--folder",
            "Inbox",
            "--select",
            "Id,InternetMessageId",
            "--all",
            "--max",
            str(max_results),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    items = json.loads(result.stdout or "[]")
    outlook_ids = {m["Id"] for m in items if m.get("Id")}
    internet_ids = {m["InternetMessageId"] for m in items if m.get("InternetMessageId")}
    return outlook_ids, internet_ids


def reconcile_moves(
    db_path: Path,
    target_mailbox: str = "Archive",
    max_results: int = 5000,
) -> dict:
    """Mark DB rows labelled 'Inbox' that aren't in the live Inbox as moved.

    Two passes:
      1. Match by `message_id` (outlook-cli-sourced rows: AAMk... ids)
      2. Match by `internet_message_id` (AppleScript-sourced legacy rows
         that have a numeric message_id but the same RFC822 Message-ID
         as one of the live Outlook entries).

    Returns a summary: {scanned_inbox, by_outlook_id, by_internet_id, moved}.
    """
    outlook_ids, internet_ids = list_current_inbox_ids(max_results=max_results)

    conn = get_connection(str(db_path))
    try:
        # Pass 1 — outlook-cli-sourced rows.
        rows = conn.execute(
            "SELECT message_id FROM emails WHERE mailbox_name = 'Inbox' AND message_id LIKE 'AAMk%'"
        ).fetchall()
        by_outlook_id = [r[0] for r in rows if r[0] not in outlook_ids]

        # Pass 2 — AppleScript-sourced rows: match on internet_message_id.
        rows = conn.execute(
            "SELECT message_id, internet_message_id FROM emails "
            "WHERE mailbox_name = 'Inbox' "
            "AND message_id NOT LIKE 'AAMk%' "
            "AND message_id NOT LIKE '-%' "
            "AND internet_message_id IS NOT NULL"
        ).fetchall()
        by_internet_id = [r[0] for r in rows if r[1] not in internet_ids]

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
            conn.commit()
    finally:
        conn.close()

    return {
        "scanned_inbox": len(outlook_ids),
        "by_outlook_id": len(by_outlook_id),
        "by_internet_id": len(by_internet_id),
        "moved": len(moved),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Inbox→Archive moves")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).parent.parent.parent / "data" / "brain.db",
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

    if not args.db.exists():
        logger.error("DB not found: %s", args.db)
        return 1

    try:
        result = reconcile_moves(args.db, target_mailbox=args.target_mailbox)
    except subprocess.CalledProcessError as e:
        logger.error("outlook-cli failed: rc=%d stderr=%s", e.returncode, e.stderr)
        return 2
    except subprocess.TimeoutExpired:
        logger.error("outlook-cli timed out listing Inbox")
        return 3
    except (json.JSONDecodeError, sqlite3.Error) as e:
        logger.error("reconcile failed: %s", e)
        return 4

    logger.info(
        "Reconciled: live_inbox=%d moved=%d (by_outlook_id=%d by_internet_id=%d) → %s",
        result["scanned_inbox"],
        result["moved"],
        result["by_outlook_id"],
        result["by_internet_id"],
        args.target_mailbox,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
