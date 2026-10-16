"""`brain maint calendar-bodies`: store the text of the events stored before it was kept.

calendar_events.body was in the schema from the first release and nothing wrote it: every event
on the producer had an empty body (audit 2026-10-16). calendar-sync stores it since, but only for
an event it fetches, a new or a changed one. This fetches, one event at a time through
`outlook-cli get-event`, the text of each stored event that has none, newest first, and writes
that column alone (src/extract/calendar_extractor.py event_body_text): no extraction, no model
call, no other column. A cancelled event is not asked for (Outlook no longer lists it). A fetch
that fails leaves the event as it was, offered again by the next run. Exit 4 when outlook-cli
needs a login, 0 otherwise.
"""

import sqlite3
import sys

from src.pipeline import EXIT_REAUTH


def run(args) -> int:
    from src.export import calendar_export
    from src.export.outlook_cli import OutlookCliAuthRequired
    from src.extract.calendar_extractor import event_body_text
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    try:
        limit = f" LIMIT {int(args.limit)}" if args.limit and args.limit > 0 else ""
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT outlook_event_id FROM calendar_events WHERE body IS NULL"
                f" AND NOT is_cancelled ORDER BY start_at DESC{limit}"
            )
        ]
        print(f"calendar-bodies: {len(ids):,} event(s) with no text stored")
        if args.dry_run:
            print("DRY RUN: nothing fetched or written")
            return 0
        stored = failed = 0
        for event_id in ids:
            try:
                raw = calendar_export.get_event_body(event_id)
            except OutlookCliAuthRequired:
                print(
                    f"  Outlook needs a login; {stored:,} stored, the rest wait for the next run",
                    file=sys.stderr,
                )
                return EXIT_REAUTH
            body = raw.get("Body") if isinstance(raw, dict) else None
            if not isinstance(body, dict):
                failed += 1
                continue
            conn.execute(
                "UPDATE calendar_events SET body = ? WHERE outlook_event_id = ? AND body IS NULL",
                (event_body_text(body.get("Content") or ""), event_id),
            )
            conn.commit()
            stored += 1
        print(f"  {stored:,} stored, {failed:,} not fetched (offered again by the next run)")
        return 0
    except sqlite3.OperationalError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
