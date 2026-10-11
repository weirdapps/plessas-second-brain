"""`brain process-sharepoint`: scan mail for SharePoint links, fetch them, store their text.

Moved from src/cli.py on 2026-10-11 as it was: the CLI parses the arguments and calls
run(args) with the argparse namespace.
"""

import sys
from pathlib import Path

from src.config import SHAREPOINT_DATA_DIR
from src.pipeline import EXIT_REAUTH


def run(args):
    """Scan emails for SharePoint URLs and fetch them."""
    from src.config import SHAREPOINT_HOST
    from src.export.sharepoint_fetcher import (
        is_managed_sharepoint_host,
        record_link_in_db,
        retry_candidates,
        target_of,
    )
    from src.extract.sharepoint_ingest import fetch_and_ingest
    from src.extract.sharepoint_url_scanner import extract_sharepoint_urls
    from src.store.email_html import markup_or_text
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found. Run 'brain load' first.")
        sys.exit(1)

    # The files earlier fetches left on disk: stored as text, then left to the sweep. No
    # fetching, so no tenant is needed (src/extract/sharepoint_ingest.py).
    if getattr(args, "ingest_fetched", False):
        from src.config import ATTACHMENTS_DIR
        from src.extract.sharepoint_ingest import ingest_fetched_backlog

        conn = get_connection(db_path)
        run_migrations(conn)
        try:
            backlog = ingest_fetched_backlog(
                conn, [SHAREPOINT_DATA_DIR, ATTACHMENTS_DIR / "sharepoint-refetch"]
            )
        finally:
            conn.close()
        print("SharePoint backlog: " + ", ".join(f"{k} {v:,}" for k, v in backlog.items()))
        return 0

    # What the old scanner and the old page fetch left in sharepoint_links: queued for the
    # retry pass, not fetched, so no tenant is needed (src/export/sharepoint_repair.py).
    if getattr(args, "repair_links", False):
        from src.export.sharepoint_repair import reread_pages, rescan_links

        conn = get_connection(db_path)
        run_migrations(conn)
        try:
            links = rescan_links(conn, apply=not args.dry_run)
            pages = reread_pages(conn, apply=not args.dry_run)
        finally:
            conn.close()
        print(
            f"Mangled links: {links['emails']:,} email(s) read again, {links['dropped']:,}"
            f" dropped, {links['added']:,} recorded for the retry pass"
        )
        print(
            f"Unread pages: {pages['links']:,} link(s) to {pages['pages']:,} page(s) offered again"
        )
        print(
            "DRY RUN: nothing changed"
            if args.dry_run
            else "Run 'brain process-sharepoint' to read them now, or leave them to the nightly pass"
        )
        return 0

    # Fetching is gated on our own tenant (fetch_sharepoint_link), so with the
    # placeholder host every real link would be refused and parked as the
    # permanent 'unsupported-host'. That happened on the producer, where the
    # variable was never set; refuse loudly instead. A dry run fetches nothing.
    import os

    from src.export.sharepoint_fetcher import managed_sharepoint_hosts

    if not args.dry_run and not managed_sharepoint_hosts(os.environ.get("SHAREPOINT_HOST", "")):
        print(
            "Error: SHAREPOINT_HOST is not set to a bare host. Set it (environment or "
            "~/.config/second-brain/env) to the tenant sharepoint-cli is logged in to.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Ensure schema is up to date
    conn = get_connection(db_path)
    run_migrations(conn)

    # Build query. Links are read from the markup: an HTML body is kept in
    # email_html, the text in content has no hrefs.
    #
    # The scan starts past the highest emails.id it has already read. It used
    # to read the newest N rows by date_received, and the nightly N of 200 was
    # a few hours of a weekday's 400-500 rows, so links in the rest of the day
    # were never recorded, and retry_candidates only retries recorded links.
    # An id also catches Archive and Sent mail loaded late with an old date.
    # News carries no links and only used up the window. --since is the manual
    # backfill: it rescans by date from the start and leaves the mark alone.
    query = (
        "SELECT e.id, e.message_id, e.content, e.date_received, h.html FROM emails e "
        "LEFT JOIN email_html h ON h.email_id = e.id WHERE e.content IS NOT NULL "
        "AND COALESCE(e.mailbox_name, '') <> 'News'"
    )
    params: list = []

    mark_row = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = 'sharepoint_scan_last_id'"
    ).fetchone()
    scan_mark = int(mark_row[0]) if mark_row else 0

    if args.since:
        query += " AND e.date_received >= ?"
        params.append(args.since)
    else:
        query += " AND e.id > ?"
        params.append(scan_mark)

    query += " ORDER BY e.id"

    # --limit bounds a --since rescan only. repo-autoupdate pulls this code onto
    # the producer without copying the wrappers, and the old nightly line passes
    # --limit 200: bounding the mark scan with it would read 200 emails a night
    # against 400-500 a weekday, and the mark would never catch up.
    if args.since and args.limit and args.limit > 0:
        query += " LIMIT ?"
        params.append(args.limit)

    # The regex scan is cheap; the fetches are not. With no mark yet the first
    # run finds the whole backlog, and this keeps it inside the nightly unit's
    # TimeoutStartSec: the mark stops before the first email it could not
    # finish, and the next run starts there.
    max_fetches = getattr(args, "max_fetches", 0) or 0
    # Wall clock too: a fetch now also extracts the file, and the nightly pass has one hour for
    # every stage. Checked before each fetch; the scan mark stops before the email it left.
    import time

    deadline_s = getattr(args, "deadline_s", None)
    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    def _out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    print("Scanning emails for SharePoint URLs...")
    if args.since:
        print(f"  Filtering to emails after {args.since}")
    else:
        print(f"  Starting past email id {scan_mark}")
    if args.since and args.limit and args.limit > 0:
        print(f"  Limiting to {args.limit} emails")
    if max_fetches > 0 and not args.dry_run:
        print(f"  Fetching at most {max_fetches} new URLs")
    if args.dry_run:
        print("  DRY RUN — scan and count only, no fetching")
    print()

    stats = {
        "emails_scanned": 0,
        "urls_found": 0,
        "urls_new": 0,
        "urls_retried": 0,
        "urls_fetched": 0,
        "urls_failed": 0,
        "urls_not_content": 0,
        "urls_skipped_external": 0,
        "auth_required": False,
    }

    # URLs already fetched successfully — never re-fetch these.
    existing_urls = {
        r[0] for r in conn.execute("SELECT url FROM sharepoint_links WHERE fetched_at IS NOT NULL")
    }
    # URLs attempted this run (retry pass + scan) — avoids double-fetching a URL
    # the email scan rediscovers after the retry pass already handled it.
    attempted: set[str] = set()
    # One fetch per target this run: the same page is linked from hundreds of emails, under as
    # many query strings, and every link to it records the one result (target_of).
    by_target: dict[str, tuple] = {}

    def _fetch_one(url: str, message_id: str) -> bool:
        """Fetch + record one URL, updating outcome stats. Returns True on a
        re-loginable (managed-host) auth failure so the caller stops the pass."""
        print(f"  Fetching: {url}")
        target = target_of(url)
        if target in by_target:
            result, document = by_target[target]
            print("    (the same target as a link above: not fetched again)")
        else:
            # Fetched into a temporary directory and stored as text; no file is kept.
            result, document = fetch_and_ingest(conn, url, message_id)
            by_target[target] = (result, document)
        # An auth failure on an external tenant (a host we hold no session for)
        # can never be fixed by our re-login, so record it distinctly and keep
        # going instead of aborting the whole pass. A foreign tenant announces
        # itself with HTTP 403 at least as often as with exit-4 auth_required,
        # and that landed in 'http-error', which the retry pass re-requests
        # every night for a host our login can never open.
        # The managed-host guard is what makes widening this safe: 403 from OUR
        # OWN tenant is a per-item authorisation refusal that can be granted
        # later, so it must stay 'http-error' and stay in the retry pool.
        # 'unsupported-host' is permanent, and parking a managed link there
        # would be the same abandonment bug in a new place.
        # The fetcher refuses a foreign tenant itself, before any request, and
        # says so with 'unsupported-host'; the relabel below stays as the second
        # line for a result that reached a host we hold no session for.
        external_auth = result.status == "unsupported-host" or (
            (result.status == "auth-required" or result.http_status == 403)
            and not is_managed_sharepoint_host(url, SHAREPOINT_HOST)
        )
        recorded_status = "unsupported-host" if external_auth else result.status
        record_link_in_db(
            conn,
            url=url,
            message_id=message_id,
            status=recorded_status,
            fetched_path=None,
            file_name=result.file_name,
            file_size=result.file_size,
            document_message_id=document,
        )
        conn.commit()

        if result.status == "ok":
            print(f"    ✓ Stored: {result.file_name}")
            stats["urls_fetched"] += 1
        elif result.status == "not-content":
            print("    · Not content (nothing to read): not fetched")
            stats["urls_not_content"] += 1
        elif external_auth:
            print("    ⤼ External host (no session) — skipping")
            stats["urls_skipped_external"] += 1
        elif result.status == "auth-required":
            print("    ✗ Auth required (session expired) — breaking")
            stats["auth_required"] = True
            return True
        else:
            print(f"    ✗ {result.status}: {result.error_message or 'unknown error'}")
            stats["urls_failed"] += 1
        return False

    # The links earlier fetches recorded 'ok' with no text stored: most were a viewer or
    # sign-in page saved as the file. Each is read again once, by what it points at. A link
    # attempted since the refetch began is done, so a run stopped by its budget resumes where
    # it stopped and a finished one finds nothing. Delete the sync_metadata row
    # 'sharepoint_refetch_since' to read them all again.
    if getattr(args, "refetch_content", False):
        from datetime import UTC, datetime

        mark = conn.execute(
            "SELECT value FROM sync_metadata WHERE key = 'sharepoint_refetch_since'"
        ).fetchone()
        since = mark[0] if mark else datetime.now(UTC).isoformat()
        if not mark and not args.dry_run:
            conn.execute(
                "INSERT INTO sync_metadata (key, value) VALUES ('sharepoint_refetch_since', ?)",
                (since,),
            )
            conn.commit()
        rows = conn.execute(
            "SELECT l.url, l.message_id FROM sharepoint_links l "
            "WHERE l.last_status = 'ok' AND COALESCE(l.last_attempt_at, '') < ? "
            "AND NOT EXISTS (SELECT 1 FROM attachments a "
            "  JOIN attachment_content ac ON ac.attachment_id = a.id "
            "  WHERE a.message_id = l.document_message_id "
            "  AND LENGTH(TRIM(COALESCE(ac.extracted_text, ''))) > 0) "
            "ORDER BY l.url",
            (since,),
        ).fetchall()
        targets = len({target_of(url) for url, _m in rows})
        print(f"Refetch: {len(rows)} link(s), {targets} target(s), with no text since {since}")
        if not args.dry_run:
            for url, message_id in rows:
                if max_fetches > 0 and stats["urls_retried"] >= max_fetches:
                    print(f"  Fetch cap of {max_fetches} reached: the rest wait for the next run")
                    break
                if _out_of_time():
                    print("  Time budget spent: the rest wait for the next run")
                    break
                stats["urls_retried"] += 1
                if _fetch_one(url, message_id):
                    break
        conn.close()
        print(
            f"\nRefetch: {stats['urls_retried']} read, {stats['urls_fetched']} stored, "
            f"{stats['urls_failed']} failed, {stats['urls_not_content']} not content"
        )
        if stats["auth_required"]:
            print(
                f"\n⚠ Auth required: run 'sharepoint-cli login --host {SHAREPOINT_HOST}' and retry"
            )
            return EXIT_REAUTH
        return 0

    # Retry pass: links seen before but never fetched OK (fetched_at NULL) or
    # gone stale get re-attempted regardless of whether their source email is
    # still inside the scan window — otherwise old failures never clear.
    # 'unsupported-host' is a permanent external tenant, so it is excluded.
    if not args.dry_run:
        retry_rows = retry_candidates(conn, managed_host=SHAREPOINT_HOST)
        if retry_rows:
            print(f"Retrying {len(retry_rows)} previously unfetched/stale link(s)...")
        for url, message_id in retry_rows:
            # Bounded like the scan below: each fetch now extracts too, and the backlog pass
            # can offer many lost links at once.
            if max_fetches > 0 and stats["urls_retried"] >= max_fetches:
                break
            if _out_of_time():
                print("  Time budget spent: the rest wait for the next run")
                break
            attempted.add(url)
            stats["urls_retried"] += 1
            if _fetch_one(url, message_id):
                break

    # Scan emails for new URLs (skip anything already fetched or attempted).
    # scanned_to is the last email every link of which was dealt with.
    scanned_to = scan_mark
    fetches = 0
    capped = False
    if not stats["auth_required"]:
        # The scan reads on a connection of its own. Its open cursor holds a read
        # snapshot, and on the connection every fetch commits on, a commit from
        # any other process in between left the next write on a stale snapshot:
        # SQLite refuses that at once with 'database is locked', busy_timeout
        # does not retry it, and the pass died with the mark unwritten.
        scan_conn = get_connection(db_path)
        try:
            for row in scan_conn.execute(query, params):
                email_id, message_id, content, date_received, html = row
                stats["emails_scanned"] += 1

                urls = extract_sharepoint_urls(markup_or_text(content, html))
                stats["urls_found"] += len(urls)

                for url in urls:
                    if url in existing_urls or url in attempted:
                        continue
                    if not args.dry_run and max_fetches > 0 and fetches >= max_fetches:
                        capped = True
                        break
                    if not args.dry_run and _out_of_time():
                        capped = True
                        break
                    attempted.add(url)
                    stats["urls_new"] += 1

                    if args.dry_run:
                        continue

                    fetches += 1
                    if _fetch_one(url, message_id):
                        break

                if stats["auth_required"] or capped:
                    break
                scanned_to = email_id
        finally:
            scan_conn.close()

    # Only a real pass over the mark's own range moves it: a dry run fetched
    # nothing, and a --since backfill did not read every id above the mark.
    if not args.dry_run and not args.since and scanned_to > scan_mark:
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) "
            "VALUES ('sharepoint_scan_last_id', ?)",
            (str(scanned_to),),
        )
        conn.commit()
    conn.close()

    print("\nSharePoint processing complete:")
    print(f"  Emails scanned: {stats['emails_scanned']}")
    print(f"  URLs found: {stats['urls_found']}")
    print(f"  New URLs: {stats['urls_new']}")
    if not args.dry_run:
        print(f"  URLs fetched: {stats['urls_fetched']}")
        print(f"  URLs failed: {stats['urls_failed']}")
        if stats["urls_not_content"]:
            print(f"  Not content (not fetched): {stats['urls_not_content']}")
        if stats["urls_skipped_external"]:
            print(f"  External hosts skipped (no session): {stats['urls_skipped_external']}")
        if capped:
            print(f"  Fetch cap of {max_fetches} reached; the next run continues from here")
    if stats["auth_required"]:
        print(f"\n⚠ Auth required — run 'sharepoint-cli login --host {SHAREPOINT_HOST}' and retry")
        # The warning alone left the nightly stage green while new links piled
        # up unfetched. run_stage in sb-attachment-pass.sh judges the exit code.
        return EXIT_REAUTH
    return 0
