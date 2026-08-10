"""CLI interface for the second-brain knowledge store.

Provides command-line access to export, extract, load, and query operations.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from src.config import (
    DATA_ROOT,
    DEFAULT_DB,
    EXTRACT_ENGINE,
    IMAGE_CLASSIFY_BUDGET_S,
    NEWS_DB_PATH,
)
from src.llm_deadline import install_llm_deadline_for_this_process


def format_email_result(email: dict, show_full: bool = False) -> str:
    """Format a single email result for display.

    Args:
        email: Email dict with date, subject, summary, etc.
        show_full: If True, show full details; otherwise show one-line summary

    Returns:
        Formatted string
    """
    if show_full:
        lines = [
            f"ID: {email.get('email_id', 'N/A')}",
            f"Date: {email.get('date', 'N/A')}",
            f"Subject: {email.get('subject', '(no subject)')}",
        ]

        if "sender" in email:
            lines.append(f"From: {email['sender']}")

        if "person_role" in email:
            lines.append(f"Role: {email['person_role']}")

        if "topic" in email:
            lines.append(f"Topic: {email['topic']}")

        if "topics" in email and email["topics"]:
            lines.append(f"Topics: {email['topics']}")

        if "summary" in email and email["summary"]:
            lines.append(f"Summary: {email['summary']}")

        if "snippet" in email and email["snippet"] != email.get("summary"):
            lines.append(f"Match: {email['snippet'][:200]}...")

        if "source" in email:
            lines.append(f"Source: {email['source']}")

        return "\n".join(lines)
    else:
        # One-line format
        date = email.get("date", "N/A")[:10]  # Just the date part
        subject = email.get("subject", "(no subject)")[:60]
        return f"[{date}] {subject}"


def format_decision_result(decision: dict) -> str:
    """Format a decision result for display.

    Args:
        decision: Decision dict with decision, decided_by, date, etc.

    Returns:
        Formatted string
    """
    lines = [
        f"ID: {decision.get('decision_id', 'N/A')}",
        f"Decision: {decision.get('decision', 'N/A')}",
    ]

    if decision.get("decided_by"):
        lines.append(f"Decided by: {decision['decided_by']}")

    lines.append(f"Date: {decision.get('date', 'N/A')}")

    if decision.get("email_subject"):
        lines.append(f"Email: {decision['email_subject']}")

    if decision.get("topics"):
        lines.append(f"Topics: {decision['topics']}")

    return "\n".join(lines)


def format_action_result(action: dict) -> str:
    """Format an action item result for display.

    Args:
        action: Action dict with task, owner, deadline, etc.

    Returns:
        Formatted string
    """
    lines = [
        f"ID: {action.get('action_id', 'N/A')}",
        f"Task: {action.get('task', 'N/A')}",
    ]

    if action.get("owner"):
        lines.append(f"Owner: {action['owner']}")

    if action.get("deadline"):
        lines.append(f"Deadline: {action['deadline']}")

    lines.append(f"Status: {action.get('status', 'open')}")

    if action.get("email_subject"):
        lines.append(f"Email: {action['email_subject']}")

    lines.append(f"Date: {action.get('date', 'N/A')}")

    return "\n".join(lines)


def cmd_export(args):
    """Execute email export command."""
    from src.export.apple_mail import export_emails

    print("Starting email export from Apple Mail Exchange mailboxes...")
    if args.limit:
        print(f"Limiting to {args.limit} emails per mailbox")
    else:
        print("Export will resume from last checkpoint (Archive) and scan other mailboxes")
    print()

    export_emails(limit=args.limit or 0)

    print()
    print("Export complete.")


def cmd_export_attachments(args):
    """Export attachments from Apple Mail."""
    from src.export.attachments import export_attachments

    print("Starting attachment export from Apple Mail...")
    if args.limit:
        print(f"Limiting to {args.limit} messages")
    if args.dry_run:
        print("DRY RUN — no files will be saved")
    print()

    export_attachments(limit=args.limit or 0, dry_run=args.dry_run)


def cmd_process_attachments(args):
    """Extract text and structured data from attachments."""
    from src.extract.attachment_pipeline import run_phase1, run_phase2
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found. Run 'brain load' first.")
        sys.exit(1)

    # Ensure schema is up to date
    conn = get_connection(db_path)
    run_migrations(conn)
    conn.close()

    phase = getattr(args, "phase", None)
    file_type = getattr(args, "type", None)
    limit = args.limit or 0

    if phase is None or phase == 1:
        print("Phase 1: Local text extraction...")
        stats = run_phase1(db_path, limit=limit, file_type=file_type)
        print(f"  Processed: {stats['processed']}")
        print(f"  Extracted: {stats['extracted']}")
        print(f"  Failed: {stats['failed']}")
        print(f"  Skipped: {stats['skipped']}")

    if phase is None or phase == 2:
        workers = getattr(args, "workers", 1) or 1
        print(f"\nPhase 2: Vertex AI structured extraction (workers={workers})...")
        stats = run_phase2(db_path, limit=limit, file_type=file_type, workers=workers)
        print(f"  Processed: {stats['processed']}")
        print(f"  Extracted: {stats['extracted']}")
        print(f"  Failed: {stats['failed']}")

    print("\nAttachment processing complete.")


def cmd_process_images(args):
    """Backfill image classification pipeline."""
    from src.extract.image_pipeline import run_backfill
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found. Run 'brain load' first.")
        sys.exit(1)

    # Ensure schema is up to date
    conn = get_connection(db_path)
    run_migrations(conn)

    # Run backfill
    print("Processing inline images...")
    if args.since:
        print(f"  Filtering to emails after {args.since}")
    if args.limit:
        print(f"  Limiting to {args.limit} images")
    if args.no_vision:
        print("  Stage 3 vision LLM disabled")
    if args.dry_run:
        print("  DRY RUN — counting only")
    if args.workers and args.workers > 1:
        print(f"  Using {args.workers} concurrent vision workers")
    print()

    stats = run_backfill(
        conn=conn,
        since=args.since,
        limit=args.limit or 1000,
        run_vision=not args.no_vision,
        dry_run=args.dry_run,
        workers=args.workers,
        unprocessed_only=not args.reprocess,
    )

    conn.close()

    print("\nImage processing complete:")
    print(f"  Scanned: {stats['scanned']}")
    print(f"  Classified: {stats['classified']}")
    print(f"  Missing: {stats['missing']}")
    print(f"  Failed: {stats['failed']}")


def cmd_process_sharepoint(args):
    """Scan emails for SharePoint URLs and fetch them."""
    from src.config import SHAREPOINT_DATA_DIR, SHAREPOINT_HOST
    from src.export.sharepoint_fetcher import (
        MAX_SHAREPOINT_ATTEMPTS,
        fetch_sharepoint_link,
        is_managed_sharepoint_host,
        record_link_in_db,
    )
    from src.extract.sharepoint_url_scanner import extract_sharepoint_urls
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found. Run 'brain load' first.")
        sys.exit(1)

    # Ensure schema is up to date
    conn = get_connection(db_path)
    run_migrations(conn)

    # Build query
    query = "SELECT id, message_id, content, date_received FROM emails WHERE content IS NOT NULL"
    params = []

    if args.since:
        query += " AND date_received >= ?"
        params.append(args.since)

    query += " ORDER BY date_received DESC"

    if args.limit and args.limit > 0:
        query += " LIMIT ?"
        params.append(args.limit)

    print("Scanning emails for SharePoint URLs...")
    if args.since:
        print(f"  Filtering to emails after {args.since}")
    if args.limit and args.limit > 0:
        print(f"  Limiting to {args.limit} emails")
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

    def _fetch_one(url: str, message_id: str) -> bool:
        """Fetch + record one URL, updating outcome stats. Returns True on a
        re-loginable (managed-host) auth failure so the caller stops the pass."""
        print(f"  Fetching: {url}")
        result = fetch_sharepoint_link(url, SHAREPOINT_DATA_DIR)
        # An auth failure on an external tenant (a host we hold no session for)
        # can never be fixed by our re-login, so record it distinctly and keep
        # going instead of aborting the whole pass.
        external_auth = result.status == "auth-required" and not is_managed_sharepoint_host(
            url, SHAREPOINT_HOST
        )
        recorded_status = "unsupported-host" if external_auth else result.status
        record_link_in_db(
            conn,
            url=url,
            message_id=message_id,
            status=recorded_status,
            fetched_path=str(result.local_path) if result.local_path else None,
            file_name=result.file_name,
            file_size=result.file_size,
        )
        conn.commit()

        if result.status == "ok":
            print(f"    ✓ Saved: {result.file_name}")
            stats["urls_fetched"] += 1
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

    # Retry pass: links seen before but never fetched OK (fetched_at NULL) or
    # gone stale get re-attempted regardless of whether their source email is
    # still inside the scan window — otherwise old failures never clear.
    # 'unsupported-host' is a permanent external tenant, so it is excluded.
    if not args.dry_run:
        retry_rows = conn.execute(
            "SELECT url, message_id FROM sharepoint_links "
            "WHERE (fetched_at IS NULL OR last_status = 'stale') "
            "AND COALESCE(last_status, '') != 'unsupported-host' "
            "AND attempts < ?",
            (MAX_SHAREPOINT_ATTEMPTS,),
        ).fetchall()
        if retry_rows:
            print(f"Retrying {len(retry_rows)} previously unfetched/stale link(s)...")
        for url, message_id in retry_rows:
            attempted.add(url)
            stats["urls_retried"] += 1
            if _fetch_one(url, message_id):
                break

    # Scan emails for new URLs (skip anything already fetched or attempted).
    if not stats["auth_required"]:
        cursor = conn.execute(query, params)
        for row in cursor:
            email_id, message_id, content, date_received = row
            stats["emails_scanned"] += 1

            urls = extract_sharepoint_urls(content)
            if not urls:
                continue

            stats["urls_found"] += len(urls)

            for url in urls:
                if url in existing_urls or url in attempted:
                    continue
                attempted.add(url)
                stats["urls_new"] += 1

                if args.dry_run:
                    continue

                if _fetch_one(url, message_id):
                    break

            if stats["auth_required"]:
                break

    conn.close()

    print("\nSharePoint processing complete:")
    print(f"  Emails scanned: {stats['emails_scanned']}")
    print(f"  URLs found: {stats['urls_found']}")
    print(f"  New URLs: {stats['urls_new']}")
    if not args.dry_run:
        print(f"  URLs fetched: {stats['urls_fetched']}")
        print(f"  URLs failed: {stats['urls_failed']}")
        if stats["urls_skipped_external"]:
            print(f"  External hosts skipped (no session): {stats['urls_skipped_external']}")
    if stats["auth_required"]:
        print(f"\n⚠ Auth required — run 'sharepoint-cli login --host {SHAREPOINT_HOST}' and retry")


def cmd_reverse_ingest(args):
    """Scan filesystem roots, dedup latest-version-per-logical-name, ingest survivors."""
    from src.extract.attachment_pipeline import ingest_document, run_phase1, run_phase2
    from src.ingest.reverse_scan import scan_roots, select_files_to_ingest, topic_label

    INGESTABLE_EXTENSIONS = {".pdf", ".pptx", ".xlsx", ".docx", ".md", ".txt"}

    roots = [Path(r).expanduser().resolve() for r in (args.root or [])]
    if not roots:
        roots = [
            Path("~/Documents/National").expanduser(),
            Path("~/Documents/Personal").expanduser(),
        ]

    print(f"Scanning {len(roots)} root(s):")
    for r in roots:
        marker = "" if r.exists() else "  (MISSING — skipped)"
        print(f"  {r}{marker}")

    scanned = scan_roots(roots, INGESTABLE_EXTENSIONS)
    selected = select_files_to_ingest(scanned)
    deduped = len(scanned) - len(selected)
    print(f"Scanned: {len(scanned)}; deduped: {deduped}; survivors: {len(selected)}")

    if args.dry_run:
        print("\n--dry-run set — listing survivors only, no DB writes:")
        for p in sorted(selected):
            print(f"  {p}")
        return

    db_path = str(args.db)
    new_attachment_ids: list[int] = []
    already = 0
    failed = 0

    for path in selected:
        label = topic_label(path, roots)
        if args.verbose:
            print(f"  ingest: {path}  (source={label})")
        try:
            result = ingest_document(str(path), db_path=db_path, source=label)
            if result.get("skipped"):
                already += 1
                continue
            new_attachment_ids.append(result["attachment_id"])
        except Exception as e:
            failed += 1
            print(f"  FAIL {path}: {e}", file=sys.stderr)

    print(
        f"Ingest: {len(new_attachment_ids)} newly created, "
        f"{already} already in DB, {failed} failed."
    )

    if new_attachment_ids:
        # Phase 1 = local text extraction (PDF/Word/Excel/PPT parsers).
        # ingest_document creates the attachment row but doesn't extract text;
        # without Phase 1, attachment_content stays empty and Phase 2 finds
        # nothing to summarize (extraction_status='extracted' filter).
        print(f"Phase 1: extracting text from {len(new_attachment_ids)} new attachments...")
        p1 = run_phase1(db_path=db_path, attachment_ids=new_attachment_ids)
        print(
            f"  extracted={p1.get('extracted', 0)}, "
            f"failed={p1.get('failed', 0)}, "
            f"skipped={p1.get('skipped', 0)}"
        )

        print(f"Phase 2: LLM-summarizing extracted attachments with {args.workers} workers...")
        p2 = run_phase2(
            db_path=db_path,
            attachment_ids=new_attachment_ids,
            workers=args.workers,
        )
        print(
            f"  extracted={p2.get('extracted', 0)}, "
            f"failed={p2.get('failed', 0)}, "
            f"processed={p2.get('processed', 0)}"
        )
    else:
        print("Phases 1+2: nothing new to process.")


def cmd_export_conversations(args):
    """Export Claude Code conversation history."""
    from src.export.conversation_export import export_conversations
    from src.store.schema import get_connection

    print("Exporting Claude Code conversations...")

    # Get already-exported session IDs from database
    exported_ids = set()
    db_path = str(args.db)
    if Path(db_path).exists():
        try:
            conn = get_connection(db_path)
            rows = conn.execute("SELECT session_id FROM conversations").fetchall()
            exported_ids = {r[0] for r in rows}
            conn.close()
            if exported_ids:
                print(f"  {len(exported_ids)} conversations already in database (will skip)")
        except Exception:
            pass

    days = getattr(args, "days", None)
    if days:
        print(f"  Scanning last {days} days")

    result = export_conversations(
        days=days,
        limit=args.limit or 0,
        workspace_filter=getattr(args, "workspace", None),
        exported_ids=exported_ids,
    )

    print(f"\nExported: {result['exported']}")
    print(f"Skipped: {result['skipped']}")
    print(f"Errors: {result['errors']}")
    if result["batch_file"]:
        print(f"Batch file: {result['batch_file']}")


def cmd_extract_conversations(args):
    """Extract structured data from staged conversations."""
    from src.extract.local import run_conversation_extraction

    print("Starting conversation extraction...")
    run_conversation_extraction(
        workers=getattr(args, "workers", 1) or 1,
        limit=args.limit or 0,
    )
    print("Conversation extraction complete.")


def cmd_ingest_conversation_incremental(args):
    """Ingest new conversation turns incrementally from an active session."""
    import json

    from src.export.conversation_export import parse_conversation
    from src.extract.claude_extract import extract_conversation
    from src.store.loader import load_single_conversation
    from src.store.schema import get_connection, run_migrations

    transcript = Path(args.transcript).expanduser().resolve()
    session_id = args.session_id
    checkpoint_dir = Path.home() / ".claude" / "memory-checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = checkpoint_dir / f"{session_id}.json"

    if not transcript.exists():
        print(f"Transcript not found: {transcript}", file=sys.stderr)
        sys.exit(1)

    # Read checkpoint
    last_line = 0
    cp = {}
    if checkpoint_file.exists():
        try:
            with open(checkpoint_file) as f:
                cp = json.load(f)
            last_line = cp.get("last_line", 0)
        except (json.JSONDecodeError, ValueError):
            pass  # Corrupt checkpoint — treat as fresh

    # Count current lines
    with open(transcript) as f:
        lines = f.readlines()
    total_lines = len(lines)

    if total_lines - last_line < 50:
        # Not enough new content
        return

    # Parse the full conversation (we need context)
    conv = parse_conversation(transcript)
    if not conv or not conv.get("turns"):
        return

    conv["session_id"] = session_id

    # Run extraction
    try:
        extraction = extract_conversation(conv)
    except Exception as e:
        print(f"Extraction failed: {e}", file=sys.stderr)
        return

    # Load into database
    db_path = str(args.db)
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return

    conn = get_connection(db_path)
    run_migrations(conn)

    # Delete existing record if re-ingesting (incremental update)
    existing = conn.execute(
        "SELECT id FROM conversations WHERE session_id = ?", (session_id,)
    ).fetchone()
    if existing:
        conv_id = existing["id"]
        # Delete dependent records first
        conn.execute("DELETE FROM conversation_topics WHERE conversation_id = ?", (conv_id,))
        turn_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM conversation_turns WHERE conversation_id = ?",
                (conv_id,),
            ).fetchall()
        ]
        for tid in turn_ids:
            conn.execute("DELETE FROM decisions WHERE conversation_turn_id = ?", (tid,))
            conn.execute("DELETE FROM action_items WHERE conversation_turn_id = ?", (tid,))
            conn.execute("DELETE FROM key_facts WHERE conversation_turn_id = ?", (tid,))
        conn.execute("DELETE FROM conversation_turns WHERE conversation_id = ?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()

    load_single_conversation(conn, conv, extraction)
    conn.commit()
    conn.close()

    # Update checkpoint
    with open(checkpoint_file, "w") as f:
        json.dump(
            {
                "last_line": total_lines,
                "last_at": datetime.now().isoformat(),
                "extract_count": (cp.get("extract_count", 0) + 1)
                if checkpoint_file.exists()
                else 1,
                "session_id": session_id,
            },
            f,
        )

    print(f"Ingested conversation {session_id} ({total_lines} lines, {len(conv['turns'])} turns)")


def cmd_load(args):
    """Execute database load command."""
    from src.store.loader import load_extractions
    from src.store.schema import create_database

    db_path = str(args.db)

    # Create database if it doesn't exist
    if not Path(db_path).exists():
        print(f"Creating new database at {db_path}...")
        create_database(db_path).close()

    print(f"Loading extractions into {db_path}...")

    extracted_dir = DATA_ROOT / "extracted"
    staging_dir = DATA_ROOT / "staging"

    count = load_extractions(db_path, str(extracted_dir), str(staging_dir))

    print(f"Loaded {count} emails")

    # Also load conversations if any are staged
    from src.store.loader import load_conversations

    conv_count = load_conversations(db_path)
    if conv_count > 0:
        print(f"Loaded {conv_count} conversations")


def cmd_prune_staged(args):
    """One-shot prune of fully-loaded batch files in data/staging.

    Auto-prune is folded into every `load` already; this command exists for
    backfilling historical accumulation (originally 5,419 files / ~1 GB on
    2026-05-02 before auto-prune was added).
    """
    from src.store.loader import prune_staged_batches

    db_path = str(args.db)
    staging_dir = DATA_ROOT / "staging"

    print(f"Scanning {staging_dir} against {db_path}...")
    pruned, freed = prune_staged_batches(db_path, str(staging_dir))
    print(f"Pruned {pruned} batch files, freed {freed / 1024 / 1024:.1f} MB")


def cmd_query_person(args):
    """Query emails by person."""
    from src.store.query import query_by_person
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    results = query_by_person(conn, args.name, limit=args.limit)
    conn.close()

    if not results:
        print(f"No emails found for person: {args.name}")
        return

    print(f"Found {len(results)} emails for {args.name}:\n")

    for i, email in enumerate(results, 1):
        print(f"{i}. {format_email_result(email)}")
        if args.verbose:
            print(format_email_result(email, show_full=True))
            print()


def cmd_query_topic(args):
    """Query emails by topic."""
    from src.store.query import query_by_topic
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    results = query_by_topic(conn, args.topic, limit=args.limit)
    conn.close()

    if not results:
        print(f"No emails found for topic: {args.topic}")
        return

    print(f"Found {len(results)} emails tagged with '{args.topic}':\n")

    for i, email in enumerate(results, 1):
        print(f"{i}. {format_email_result(email)}")
        if args.verbose:
            print(format_email_result(email, show_full=True))
            print()


def cmd_query_keyword(args):
    """Query emails by keyword (full-text search)."""
    from src.store.query import query_by_keyword, search_attachments
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    results = query_by_keyword(conn, args.keyword, limit=args.limit)

    if not results:
        print(f"No emails found matching: {args.keyword}")
    else:
        print(f"Found {len(results)} emails matching '{args.keyword}':\n")

        for i, email in enumerate(results, 1):
            print(f"{i}. {format_email_result(email)}")
            if args.verbose:
                print(format_email_result(email, show_full=True))
                print()

    # Also search attachment content
    att_results = search_attachments(conn, args.keyword, limit=10)
    if att_results:
        print(f"\n--- Attachment Results ({len(att_results)}) ---")
        for r in att_results:
            date = (r["email_date"] or "N/A")[:10]
            print(f"  [{date}] {r['filename']}")
            if r["email_subject"]:
                print(f"    Email: {r['email_subject']}")
            if r["snippet"]:
                print(f"    ...{r['snippet']}...")
            print()

    conn.close()


def cmd_query_date(args):
    """Query emails by date range."""
    from src.store.query import query_by_date_range
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    results = query_by_date_range(conn, args.start, args.end, limit=args.limit)
    conn.close()

    if not results:
        print(f"No emails found between {args.start} and {args.end}")
        return

    print(f"Found {len(results)} emails from {args.start} to {args.end}:\n")

    for i, email in enumerate(results, 1):
        print(f"{i}. {format_email_result(email)}")
        if args.verbose:
            print(format_email_result(email, show_full=True))
            print()


def cmd_query_decisions(args):
    """Query decisions."""
    from src.store.query import query_decisions
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    results = query_decisions(conn, topic=args.topic, person=args.person, limit=args.limit)
    conn.close()

    if not results:
        print("No decisions found")
        return

    print(f"Found {len(results)} decisions:\n")

    for i, decision in enumerate(results, 1):
        print(f"{i}. {format_decision_result(decision)}")
        print()


def cmd_query_actions(args):
    """Query action items."""
    from src.store.query import query_action_items
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    results = query_action_items(conn, owner=args.owner, status=args.status, limit=args.limit)
    conn.close()

    if not results:
        print("No action items found")
        return

    print(f"Found {len(results)} action items:\n")

    for i, action in enumerate(results, 1):
        print(f"{i}. {format_action_result(action)}")
        print()


def cmd_query_thread(args):
    """Show conversation thread for an email."""
    from src.store.query import query_thread
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    results = query_thread(conn, args.email_id, limit=args.limit)
    conn.close()

    if not results:
        print(f"No thread found for email ID: {args.email_id}")
        return

    print(f"Thread with {len(results)} emails:\n")

    for i, email in enumerate(results, 1):
        marker = ">>>" if email["email_id"] == args.email_id else "   "
        date = email.get("date", "N/A")[:16]
        sender = email.get("sender_name", "Unknown")
        subject = email.get("subject", "(no subject)")[:60]
        print(f"{marker} {i}. [{date}] {sender}: {subject}")
        if args.verbose and email.get("summary"):
            print(f"       Summary: {email['summary']}")
            print()


def cmd_query_combined(args):
    """Query with combined filters."""
    from src.store.query import query_combined
    from src.store.schema import get_connection

    conn = None
    try:
        conn = get_connection(str(args.db))
        results = query_combined(
            conn,
            person=args.person,
            topic=args.topic,
            keyword=args.keyword,
            start_date=args.start,
            end_date=args.end,
            limit=args.limit,
        )
    except ValueError as e:
        print(f"Error: {e}")
        return
    finally:
        if conn:
            conn.close()

    if not results:
        print("No emails found matching the combined criteria")
        return

    print(f"Found {len(results)} emails:\n")

    for i, email in enumerate(results, 1):
        print(f"{i}. {format_email_result(email)}")
        if args.verbose:
            print(format_email_result(email, show_full=True))
            print()


def cmd_prep(args):
    """Generate meeting prep dossier."""
    from src.store.query import meeting_prep
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    people_list = [p.strip() for p in args.people.split(",")]
    result = meeting_prep(
        conn, people_list, topic=args.topic, days=args.days, limit_per_person=args.limit
    )
    conn.close()

    for dossier in result["attendees"]:
        print(f"\n{'=' * 50}")
        print(f"  {dossier['name']}")
        print(f"{'=' * 50}")

        if dossier["emails"]:
            print(f"\nRecent emails ({len(dossier['emails'])}):")
            for e in dossier["emails"]:
                date = e.get("date", "N/A")[:10]
                role = e.get("role", "")
                print(f"  [{date}] ({role}) {e.get('subject', '(no subject)')[:60]}")
                if args.verbose and e.get("summary"):
                    print(f"    {e['summary'][:120]}")

        if dossier["topics"]:
            topics_str = ", ".join(f"{t['topic']} ({t['count']})" for t in dossier["topics"])
            print(f"\nTop topics: {topics_str}")

        if dossier["sentiment_summary"]:
            sent_str = ", ".join(f"{k}: {v}" for k, v in dossier["sentiment_summary"].items())
            print(f"Sentiment: {sent_str}")

        if dossier["decisions"]:
            print(f"\nDecisions ({len(dossier['decisions'])}):")
            for d in dossier["decisions"]:
                print(f"  [{d.get('date', 'N/A')[:10]}] {d['decision'][:80]}")

        if dossier["open_actions"]:
            print(f"\nOpen actions ({len(dossier['open_actions'])}):")
            for a in dossier["open_actions"]:
                deadline = f" (due: {a['deadline']})" if a.get("deadline") else ""
                print(f"  - {a['task'][:80]}{deadline}")

    if result.get("topic_context"):
        tc = result["topic_context"]
        print(f"\n{'=' * 50}")
        print(f"  Topic: {tc['topic']}")
        print(f"{'=' * 50}")

        if tc["decisions"]:
            print(f"\nRelated decisions ({len(tc['decisions'])}):")
            for d in tc["decisions"]:
                print(f"  [{d.get('date', 'N/A')[:10]}] {d['decision'][:80]}")

        if tc["key_facts"]:
            print(f"\nKey facts ({len(tc['key_facts'])}):")
            for f in tc["key_facts"]:
                print(f"  - {f['fact'][:100]}")

        if tc["action_items"]:
            print(f"\nOpen actions ({len(tc['action_items'])}):")
            for a in tc["action_items"]:
                deadline = f" (due: {a['deadline']})" if a.get("deadline") else ""
                print(f"  - {a['task'][:80]}{deadline}")


def cmd_sync(args):
    """Incremental sync: export new emails, extract, load."""
    from src.store.schema import get_connection, migrate_add_sync_metadata

    db_path = str(args.db)

    if not Path(db_path).exists():
        print("Error: Database not found. Run 'brain load' first to create it.")
        sys.exit(1)

    conn = get_connection(db_path)
    migrate_add_sync_metadata(conn)

    # Get last sync date
    cursor = conn.execute("SELECT value FROM sync_metadata WHERE key = 'last_sync_date'")
    row = cursor.fetchone()
    last_sync = row[0] if row else None

    if last_sync:
        print(f"Last sync: {last_sync}")
    else:
        # Fallback: use latest email date in DB
        cursor = conn.execute("SELECT MAX(date_received) FROM emails")
        row = cursor.fetchone()
        last_sync = row[0] if row and row[0] else None
        if last_sync:
            print(f"No sync history. Using latest email date: {last_sync}")
        else:
            print("Empty database. Run full export + load first.")
            conn.close()
            return

    conn.close()

    # Step 1: Export new emails. Skipped in catch-up mode (outlook-cli stages
    # hourly) AND always on non-macOS: Apple Mail / osascript don't exist on
    # Linux, so the VPS would die here — it ingests via outlook-cli, never Apple
    # Mail. Folding the platform check in keeps `sync` working on the VPS without
    # every caller having to pass --skip-export.
    skip_export = getattr(args, "skip_export", False) or sys.platform != "darwin"
    if skip_export:
        if getattr(args, "skip_export", False):
            print("\nStep 1: SKIPPED — using staged batches from hourly outlook-cli sync.")
        else:
            print("\nStep 1: SKIPPED — non-macOS host; mail is staged via outlook-cli.")
    else:
        print(f"\nStep 1: Exporting new emails (all mailboxes, after {last_sync})...")
        from src.export.apple_mail import export_emails

        export_emails(limit=args.limit or 0, since_date=last_sync)

    # Step 2: Extract (local mode)
    engine = getattr(args, "engine", None) or EXTRACT_ENGINE
    print(f"\nStep 2: Extracting new emails (engine: {engine})...")
    from src.extract.local import run_extraction

    run_extraction(limit=args.limit or 0, engine=engine, workers=args.workers or 1)

    # Step 3: Load into DB
    print("\nStep 3: Loading into database...")
    from src.store.loader import load_extractions
    from src.store.schema import get_connection as get_conn

    extracted_dir = DATA_ROOT / "extracted"
    staging_dir = DATA_ROOT / "staging"
    count = load_extractions(db_path, str(extracted_dir), str(staging_dir))
    print(f"Loaded {count} new emails")

    # Step 3b: Export attachments from Mail.app for new emails
    # (skipped in catch-up — outlook-cli already pulls attachments inline with the hourly sync)
    new_attachment_ids = []
    if count > 0 and not skip_export:
        print("\nStep 3b: Exporting attachments from Mail.app (Inbox + Sent)...")
        from src.export.attachments import export_sync_attachments

        att_result = export_sync_attachments(db_path)
        new_attachment_ids = att_result.get("attachment_ids", [])
        print(
            f"Attachments: {att_result['saved']} saved from {att_result['scanned']} messages scanned"
        )
    elif count > 0:
        print("\nStep 3b: SKIPPED — outlook-cli already fetched attachments hourly.")

    # Step 4: Deduplicate people
    if count > 0:
        print("\nStep 4: Deduplicating people...")
        from src.store.dedup_people import run_dedup

        dedup_result = run_dedup(db_path=db_path)
        reduced = dedup_result.get("total_reduced", 0)
        print(f"Dedup: {reduced} duplicates merged")

        # Step 5: Update embeddings for new emails
        print("\nStep 5: Updating embeddings...")
        from src.store.embeddings import build_index

        conn_embed = get_conn(db_path)
        build_index(conn_embed)
        conn_embed.close()
        print("Embeddings updated")
    else:
        print("\nNo new emails — skipping dedup and embeddings")

    # Step 6: Process new attachment content
    from src.extract.attachment_pipeline import run_phase1, run_phase2

    # Ensure attachment_content table exists
    from src.store.schema import run_migrations as run_mig

    conn_mig = get_conn(db_path)
    run_mig(conn_mig)
    conn_mig.close()

    print("\nStep 6: Processing new attachment content...")
    # Scope to newly exported attachments only — avoids competing with background backfill
    scope_ids = new_attachment_ids or None
    p1_stats = run_phase1(db_path, attachment_ids=scope_ids)
    if p1_stats["processed"] > 0:
        print(
            f"  Phase 1: {p1_stats['extracted']} text extracted, "
            f"{p1_stats['failed']} failed, {p1_stats['skipped']} skipped"
        )
        p2_stats = run_phase2(db_path, attachment_ids=scope_ids)
        print(f"  Phase 2: {p2_stats['extracted']} LLM extracted, {p2_stats['failed']} failed")
    else:
        print("  No new attachments to process")

    # Step 7: Sync conversations from Claude Code sessions
    print("\nStep 7: Syncing Claude Code conversations...")
    from src.export.conversation_export import export_conversations
    from src.store.loader import load_conversations as load_convs

    conn_conv = get_conn(db_path)
    run_mig(conn_conv)
    existing_convs = conn_conv.execute("SELECT session_id FROM conversations").fetchall()
    exported_ids = {r["session_id"] for r in existing_convs} if existing_convs else set()
    conn_conv.close()

    conv_export = export_conversations(days=7, exported_ids=exported_ids)
    if conv_export["exported"] > 0:
        print(f"  Exported {conv_export['exported']} new conversations")
        from src.extract.local import run_conversation_extraction

        run_conversation_extraction()
        conv_loaded = load_convs(db_path)
        print(f"  Loaded {conv_loaded} conversations")
    else:
        print("  No new conversations to sync")

    # Step 8: Classify new inline images (Stage 1 heuristic + Stage 3 vision LLM)
    print("\nStep 8: Classifying new inline images...")
    try:
        from src.extract.image_pipeline import run_backfill

        img_conn = get_conn(db_path)
        run_mig(img_conn)
        # Time-boxed: 200 vision calls at 4 workers is ~30min, which is the whole
        # systemd TimeoutStartSec for sb-daily-sync / sb-noon-catchup — step 8 was
        # spending the entire budget and getting SIGTERMed. Cap it so the sync
        # always returns cleanly; the leftover drains on the next run.
        img_stats = run_backfill(
            conn=img_conn,
            since=None,
            limit=200,
            run_vision=True,
            dry_run=False,
            workers=4,
            unprocessed_only=True,
            deadline_s=IMAGE_CLASSIFY_BUDGET_S,
        )
        img_conn.close()
        classified = img_stats.get("classified", 0)
        missing = img_stats.get("missing", 0)
        deferred = img_stats.get("deferred", 0)
        # `missing` = file gone from disk; `deferred` = budget spent, work requeued.
        # The old line printed `missing` under the label "remaining", which read as
        # "backlog empty: 0" every day while the queue was 200 deep. Queue depth is
        # reported by health_check.check_images (WARN past IMAGE_QUEUE_WARN); this
        # line just says what THIS run did.
        if classified > 0 or missing > 0 or deferred > 0:
            print(f"  Classified: {classified}, deferred: {deferred}, missing files: {missing}")
        else:
            print("  No unclassified images")
    except Exception as e:
        print(f"  Image classification skipped: {e}")

    # Update sync metadata
    conn = get_conn(db_path)
    migrate_add_sync_metadata(conn)
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('last_sync_date', ?)",
        (now,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('last_sync_count', ?)",
        (str(count),),
    )
    conn.commit()
    conn.close()

    print(f"\nSync complete. {count} emails added. Sync timestamp: {now}")


def cmd_teams_sync(args):
    """Run the Teams ingestion pipeline (Phase 1: channels only)."""
    from src.export.teams_export import discover_chats, pull_messages
    from src.extract.teams_mri import resolve_mris
    from src.extract.teams_pipeline import extract_threads
    from src.extract.teams_threads import bound_threads
    from src.store.embeddings import build_teams_index
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection(db_path)
    run_migrations(conn)

    if not getattr(args, "skip_pull", False):
        print("Step 1/6: discovering chats + channels...")
        d = discover_chats(conn, scope="all")
        print(
            f"  {d['chats_inserted']} new, {d['chats_updated']} updated, {d['chats_discovered']} total"
        )

        print("Step 2/6: pulling messages...")
        p = pull_messages(conn, concurrency=args.concurrency or 2)
        print(
            f"  {p['messages_inserted']} messages inserted across {p['chats_pulled']} chats; {p['errors']} errors"
        )
    else:
        print("Steps 1+2 SKIPPED — --skip-pull set.")

    print("Step 3/6: bounding threads...")
    b = bound_threads(conn)
    print(f"  {b['threads_created']} new threads, {b['threads_updated']} touched")

    print("Step 4/6: resolving MRIs...")
    r = resolve_mris(conn, max_per_run=50)
    print(
        f"  {r['resolved']} resolved, {r['permanent_fail']} 404'd, {r['retryable_fail']} retryable"
    )

    print("Step 5/6: extracting threads...")
    e = extract_threads(conn, workers=args.workers or 4, limit=args.limit or 0)
    print(f"  {e['extracted']} extracted, {e['skipped']} skipped, {e['failed']} failed")

    print("Step 6/6: embedding thread summaries...")
    n = build_teams_index(conn)
    print(f"  {n} new/updated embeddings")

    conn.close()
    print("teams-sync complete.")


def cmd_teams_search(args):
    from src.store.schema import get_connection
    from src.store.teams_query import search_teams

    conn = get_connection(str(args.db))
    results = search_teams(conn, args.query, kind=args.kind, limit=args.limit)
    conn.close()
    if not results:
        print(f"No teams matches for: {args.query}")
        return
    for i, r in enumerate(results, 1):
        print(f"\n{i}. [{r['team_name']} / {r['channel_topic']}] {r['title'] or '(untitled)'}")
        print(
            f"   {r['started_at'][:10]}..{r['ended_at'][:10]} ({r['message_count']} msgs, match={r['match']})"
        )
        if r.get("snippet"):
            print(f"   {r['snippet']}")


def cmd_teams_thread(args):
    from src.store.schema import get_connection
    from src.store.teams_query import thread_context

    conn = get_connection(str(args.db))
    ctx = thread_context(conn, args.thread_id)
    conn.close()
    if "error" in ctx:
        print(ctx["error"])
        return
    t = ctx["thread"]
    print(f"# {t['title']}")
    print(f"  {t['team_name']} / {t['channel_topic']}  ({t['started_at']} → {t['ended_at']})")
    print(f"  {t['message_count']} messages\n")
    if t.get("summary"):
        print(f"SUMMARY: {t['summary']}\n")
    print("MESSAGES:")
    for m in ctx["messages"]:
        sys_marker = " [system]" if m["is_system"] else ""
        print(f"  [{m['composed_at']}] {m['sender_display_name']}{sys_marker}: {m['content_text']}")
    if ctx["decisions"]:
        print("\nDECISIONS:")
        for d in ctx["decisions"]:
            print(f"  - {d['decision']}  ({d['decided_by']}, {d['decision_date']})")
    if ctx["action_items"]:
        print("\nACTIONS:")
        for a in ctx["action_items"]:
            print(f"  - {a['task']}  (owner: {a['owner']}, due: {a['deadline']}, {a['status']})")
    if ctx["key_facts"]:
        print("\nKEY FACTS:")
        for f in ctx["key_facts"]:
            print(f"  - {f['fact']}")


def cmd_teams_chat(args):
    from src.store.schema import get_connection
    from src.store.teams_query import chat_summary

    conn = get_connection(str(args.db))
    s = chat_summary(conn, args.chat_id, days=args.days)
    conn.close()
    if "error" in s:
        print(s["error"])
        return
    c = s["chat"]
    print(f"# {c['team_name']} / {c['topic']}  (chat_id={c['id']}, kind={c['chat_kind']})")
    print(f"  Last activity: {c['last_message_at']}\n")
    print(f"Recent threads ({len(s['threads'])}):")
    for t in s["threads"]:
        print(f"  [{t['ended_at'][:10]}] {t['title']}  ({t['message_count']} msgs)")
    print(f"\nLast messages ({len(s['last_messages'])}):")
    for m in s["last_messages"]:
        snippet = (m["content_text"] or "")[:80]
        print(f"  [{m['composed_at'][:16]}] {m['sender_display_name']}: {snippet}")
    print(f"\nOpen actions ({len(s['open_actions'])}):")
    for a in s["open_actions"]:
        print(f"  - {a['task']} (owner: {a['owner']}, due: {a['deadline']})")


def cmd_teams_stats(args):
    from src.store.schema import get_connection
    from src.store.teams_query import stats

    conn = get_connection(str(args.db))
    s = stats(conn)
    conn.close()
    print("Teams Statistics")
    print("=" * 40)
    for k, v in s.items():
        print(f"{k:30s} {v}")


def cmd_calendar_sync(args):
    """Sync calendar events from Outlook into the knowledge store."""
    from datetime import timedelta

    from src.config import USER_EMAIL_PATTERN
    from src.export.calendar_export import get_event_body, list_events, parse_event
    from src.extract.calendar_extractor import extract_event
    from src.store.calendar_loader import load_event, load_proxy_emails
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found.")
        sys.exit(1)

    conn = get_connection(db_path)
    run_migrations(conn)

    proxy_emails = load_proxy_emails(str(DATA_ROOT / "canonical_people.json"))

    now = datetime.now()
    if args.backfill and args.since:
        since = datetime.fromisoformat(args.since)
    elif args.backfill:
        since = now - timedelta(days=365)
    else:
        since = now - timedelta(days=7)

    until_dt = datetime.fromisoformat(args.until) if args.until else (now + timedelta(days=30))

    print(f"Calendar sync: {since.date()} to {until_dt.date()}")
    raw_events = list_events(since, until_dt)
    print(f"  Fetched {len(raw_events)} events")

    stats = {"loaded": 0, "skipped_unchanged": 0, "extracted": 0, "failed": 0}

    for raw in raw_events:
        event = parse_event(raw)

        # Skip if unchanged (compare modified_at)
        existing = conn.execute(
            "SELECT modified_at FROM calendar_events WHERE outlook_event_id = ?",
            (event["outlook_event_id"],),
        ).fetchone()
        if existing and existing["modified_at"] == event.get("modified_at"):
            stats["skipped_unchanged"] += 1
            continue

        # Fetch full event (list-calendar returns a subset without Attendees,
        # ResponseStatus, IsRecurring etc. — get-event has everything)
        body_html = ""
        body_raw = get_event_body(event["outlook_event_id"])
        if body_raw and isinstance(body_raw, dict):
            event = parse_event(body_raw)
            body_obj = body_raw.get("Body", {})
            if isinstance(body_obj, dict):
                body_html = body_obj.get("Content", "")

        # Extract
        extraction = {"body_summary": "", "decisions": [], "action_items": []}
        if not args.skip_extraction and body_html and len(body_html.strip()) >= 50:
            try:
                extraction = extract_event(event, body_html)
                stats["extracted"] += 1
            except Exception as e:
                print(
                    f"  Extraction failed for {event.get('subject', '???')}: {e}",
                    file=sys.stderr,
                )
                stats["failed"] += 1

        # Load
        load_event(
            conn,
            event,
            extraction,
            user_email_pattern=USER_EMAIL_PATTERN,
            proxy_emails=proxy_emails,
        )
        stats["loaded"] += 1

    conn.close()
    print(f"  Loaded:     {stats['loaded']}")
    print(f"  Unchanged:  {stats['skipped_unchanged']}")
    print(f"  Extracted:  {stats['extracted']}")
    print(f"  Failed:     {stats['failed']}")


def _staged_news_ids(staging_dir: Path) -> set[str]:
    """news:* message_ids already sitting in staging batches (unreadable files skipped)."""
    import json

    staged: set[str] = set()
    for batch_file in sorted(staging_dir.glob("batch-*.json")):
        try:
            records = json.loads(batch_file.read_text(encoding="utf-8"))["emails"]
            staged.update(
                r["message_id"] for r in records if str(r.get("message_id", "")).startswith("news:")
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return staged


def cmd_news_sync(args):
    """Stage news-reader syntheses and articles as batches for the next sync."""
    import tempfile

    from src.export.news_export import PIPELINES, export_news
    from src.store.schema import get_connection

    news_db = Path(args.news_db)
    if not news_db.exists():
        print(f"Error: news database not found: {news_db}")
        sys.exit(1)

    pipelines = args.pipeline or list(PIPELINES)
    staging_dir = DATA_ROOT / "staging"

    # Already-loaded or already-staged news items, so re-runs skip them
    exported_ids = _staged_news_ids(staging_dir)
    db_path = str(args.db)
    if Path(db_path).exists():
        try:
            conn = get_connection(db_path)
            rows = conn.execute(
                "SELECT message_id FROM emails WHERE message_id LIKE 'news:%'"
            ).fetchall()
            exported_ids |= {r[0] for r in rows}
            conn.close()
        except Exception:
            pass

    print(f"News sync: {news_db}")
    print(f"  Pipelines: {', '.join(pipelines)}")
    print(f"  Relevance >= {args.relevance}, limit {args.limit or 'none'}")
    print(f"  {len(exported_ids)} news items already loaded or staged (will skip)")

    if args.dry_run:
        # Export into a throwaway directory to count what would be staged
        with tempfile.TemporaryDirectory() as tmp_staging:
            preview = export_news(
                news_db=news_db,
                staging_dir=Path(tmp_staging),
                relevance_threshold=args.relevance,
                pipelines=pipelines,
                exported_ids=exported_ids,
                limit=args.limit,
            )
        print("\nDRY RUN — nothing written.")
        print(f"  Would stage syntheses: {preview['syntheses']}")
        print(f"  Would stage articles: {preview['articles']}")
        print(f"  Would skip (already exported): {preview['skipped']}")
        return

    result = export_news(
        news_db=news_db,
        staging_dir=staging_dir,
        relevance_threshold=args.relevance,
        pipelines=pipelines,
        exported_ids=exported_ids,
        limit=args.limit,
    )

    print(f"\nSyntheses: {result['syntheses']}")
    print(f"Articles: {result['articles']}")
    print(f"Skipped: {result['skipped']}")
    print(f"Batch files: {len(result['batch_files'])}")
    for batch_file in result["batch_files"]:
        print(f"  {batch_file}")


def cmd_migrate(args):
    """Run all database migrations."""
    from src.store.schema import get_connection, get_schema_version, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found.")
        sys.exit(1)

    conn = get_connection(db_path)

    before = get_schema_version(conn)
    print(f"Current schema version: {before}")
    print("Running migrations...")

    run_migrations(conn)

    after = get_schema_version(conn)
    conn.close()

    if after > before:
        print(f"Migrated from version {before} to {after}.")
    else:
        print("Database is already up to date.")


def cmd_import_people(args):
    """Import canonical people from JSON file."""
    import json

    from src.store.dedup_people import merge_person, normalize_name
    from src.store.schema import get_connection

    db_path = str(args.db)
    json_path = args.file or str(DATA_ROOT / "canonical_people.json")

    with open(json_path) as f:
        canonical = json.load(f)

    conn = get_connection(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")

    updated = 0
    merged = 0

    for entry in canonical:
        email = entry.get("email")
        name = entry["canonical_name"]
        role = entry.get("role")
        department = entry.get("department")
        aliases = entry.get("aliases", [])

        # Step 1: Find primary person by email (case-insensitive)
        primary_id = None
        if email:
            row = conn.execute(
                "SELECT id FROM people WHERE LOWER(email) = LOWER(?)", (email,)
            ).fetchone()
            if row:
                primary_id = row[0]

        # Step 2: If not found by email, try by normalized name
        if primary_id is None:
            norm_name = normalize_name(name)
            rows = conn.execute("SELECT id, name FROM people").fetchall()
            for r in rows:
                if normalize_name(r[1]) == norm_name:
                    primary_id = r[0]
                    break

        # Step 3: If still not found, skip (don't create people not in DB)
        if primary_id is None:
            continue

        # Update canonical name, role, department
        conn.execute(
            "UPDATE people SET name = ?, role = ?, department = ? WHERE id = ?",
            (name, role, department, primary_id),
        )
        updated += 1

        # Step 4: Find and merge alias records
        for alias in aliases:
            norm_alias = normalize_name(alias)
            alias_rows = conn.execute(
                "SELECT id, name FROM people WHERE id != ?", (primary_id,)
            ).fetchall()
            for ar in alias_rows:
                if normalize_name(ar[1]) == norm_alias:
                    merge_person(conn, primary_id, ar[0])
                    merged += 1

    conn.commit()
    conn.close()

    print(f"Updated {updated} people, merged {merged} alias records.")


def cmd_stale(args):
    """Find stale threads and overdue actions."""
    from src.store.query import find_overdue_actions, find_stale_threads
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))

    print("STALE THREADS (you sent last, no reply)")
    print("=" * 50)
    stale = find_stale_threads(conn, days=args.days)
    if stale:
        for i, t in enumerate(stale, 1):
            print(f"{i}. [{t['days_waiting']}d waiting] {t['subject']}")
    else:
        print("  No stale threads found.")

    print()
    print("OVERDUE ACTION ITEMS")
    print("=" * 50)
    overdue = find_overdue_actions(conn)
    if overdue:
        for i, a in enumerate(overdue, 1):
            owner = a.get("owner", "unassigned")
            print(f"{i}. [{a['days_overdue']}d overdue] {a['task'][:80]} (owner: {owner})")
    else:
        print("  No overdue actions found.")

    conn.close()


def cmd_embed(args):
    """Build or rebuild the embedding index."""
    from src.store.embeddings import build_index
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    count = build_index(conn, force=args.force)
    conn.close()

    if count > 0:
        print(f"Generated embeddings for {count} emails.")
    else:
        print("Embedding index is up to date.")


def cmd_query_semantic(args):
    """Semantic search across email summaries."""
    from src.store.embeddings import query_semantic
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    results = query_semantic(conn, args.query, limit=args.limit)
    conn.close()

    if not results:
        print(f"No results for: {args.query}")
        return

    print(f"Top {len(results)} semantic matches for '{args.query}':\n")

    for i, email in enumerate(results, 1):
        sim = email.get("similarity", 0)
        print(
            f"{i}. [{email.get('date', 'N/A')[:10]}] (sim: {sim:.3f}) {email.get('subject', '(no subject)')[:60]}"
        )
        if args.verbose and email.get("summary"):
            print(f"   {email['summary'][:120]}")
            print()


def cmd_ingest(args):
    """Ingest standalone documents or URLs into the knowledge store."""
    from src.extract.attachment_pipeline import ingest_document, run_phase1, run_phase2
    from src.store.schema import get_connection, run_migrations

    if not args.files and not args.url:
        print("Error: Provide file paths or --url arguments.")
        sys.exit(1)

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found. Run 'brain load' first.")
        sys.exit(1)

    # Ensure schema is up to date
    conn = get_connection(db_path)
    run_migrations(conn)
    conn.close()

    all_attachment_ids = []

    # Ingest URLs
    for url in args.url:
        from src.extract.web_ingest import ingest_url

        print(f"\nFetching: {url}")
        try:
            result = ingest_url(
                url,
                title=args.title,
                source=args.source,
                db_path=db_path,
            )
            if result.get("skipped"):
                print(f"  Skipped: {result['reason']}")
                continue
            print(
                f"  Created email_id={result['email_id']}, attachment_id={result['attachment_id']}"
            )
            all_attachment_ids.append(result["attachment_id"])
        except Exception as e:
            print(f"  Error fetching URL: {e}")

    # Ingest local files
    for file_path in args.files or []:
        file_path = str(Path(file_path).expanduser().resolve())
        print(f"\nIngesting: {Path(file_path).name}")

        result = ingest_document(file_path, db_path=db_path, source=args.source)

        if result.get("skipped"):
            print(f"  Skipped: {result['reason']}")
            continue

        print(f"  Created email_id={result['email_id']}, attachment_id={result['attachment_id']}")
        all_attachment_ids.append(result["attachment_id"])

    if not all_attachment_ids:
        print("\nNo new documents to process.")
        return

    # Phase 1: local text extraction
    print(f"\nPhase 1: Extracting text from {len(all_attachment_ids)} document(s)...")
    p1 = run_phase1(db_path, attachment_ids=all_attachment_ids)
    print(f"  Extracted: {p1['extracted']}, Failed: {p1['failed']}, Skipped: {p1['skipped']}")

    # Phase 2: LLM structured extraction
    workers = getattr(args, "workers", 1) or 1
    print(f"\nPhase 2: LLM structured extraction (workers={workers})...")
    p2 = run_phase2(db_path, attachment_ids=all_attachment_ids, workers=workers)
    print(f"  Extracted: {p2['extracted']}, Failed: {p2['failed']}")

    print(f"\nIngest complete. {len(all_attachment_ids)} item(s) added to knowledge store.")


def cmd_stats(args):
    """Show database statistics."""
    from src.store.query import get_stats
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    stats = get_stats(conn)
    conn.close()

    print("Second Brain Statistics")
    print("=" * 50)
    print()
    print(f"Total Emails:        {stats['total_emails']:,}")
    print(f"Total Topics:        {stats['total_topics']:,}")
    print(f"Total People:        {stats['total_people']:,}")
    print(f"Total Decisions:     {stats['total_decisions']:,}")
    print(f"Total Action Items:  {stats['total_action_items']:,}")
    print()

    if stats["earliest_email"] and stats["latest_email"]:
        earliest = stats["earliest_email"][:10]
        latest = stats["latest_email"][:10]
        print(f"Date Range:          {earliest} to {latest}")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="brain", description="Second Brain - Email knowledge store CLI"
    )

    # Global options
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Database path (default: {DEFAULT_DB})",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Export command
    parser_export = subparsers.add_parser("export", help="Export emails from Apple Mail")
    parser_export.add_argument(
        "--limit", type=int, help="Limit number of emails to export (default: all)"
    )
    parser_export.set_defaults(func=cmd_export)

    # Export attachments command
    parser_export_att = subparsers.add_parser(
        "export-attachments", help="Export attachments from Apple Mail"
    )
    parser_export_att.add_argument("--limit", type=int, help="Max messages to scan")
    parser_export_att.add_argument(
        "--dry-run", action="store_true", help="Show what would be saved"
    )
    parser_export_att.set_defaults(func=cmd_export_attachments)

    # Export conversations command
    parser_export_conv = subparsers.add_parser(
        "export-conversations", help="Export Claude Code conversation history"
    )
    parser_export_conv.add_argument(
        "--days", type=int, help="Only export last N days (default: all)"
    )
    parser_export_conv.add_argument("--limit", type=int, help="Max conversations to export")
    parser_export_conv.add_argument(
        "--workspace", type=str, help="Filter by workspace path substring"
    )
    parser_export_conv.set_defaults(func=cmd_export_conversations)

    # Extract conversations command
    parser_extract_conv = subparsers.add_parser(
        "extract-conversations",
        help="Extract structured data from staged conversations",
    )
    parser_extract_conv.add_argument("--limit", type=int, help="Max conversations to extract")
    parser_extract_conv.add_argument(
        "--workers", type=int, default=1, help="Concurrent workers (default: 1)"
    )
    parser_extract_conv.set_defaults(func=cmd_extract_conversations)

    # Ingest conversation incremental
    parser_ingest_conv = subparsers.add_parser(
        "ingest-conversation-incremental",
        help="Incrementally ingest active conversation",
    )
    parser_ingest_conv.add_argument("--transcript", required=True, help="Path to JSONL transcript")
    parser_ingest_conv.add_argument("--session-id", required=True, help="Session UUID")
    parser_ingest_conv.set_defaults(func=cmd_ingest_conversation_incremental)

    # Process attachments command
    parser_process_att = subparsers.add_parser(
        "process-attachments", help="Extract text and structured data from attachments"
    )
    parser_process_att.add_argument(
        "--phase",
        type=int,
        choices=[1, 2],
        help="Run only phase 1 (local extraction) or phase 2 (LLM extraction)",
    )
    parser_process_att.add_argument(
        "--type",
        type=str,
        choices=["pdf", "word", "pptx", "excel", "image", "eml", "rpmsg"],
        help="Process only a specific file type",
    )
    parser_process_att.add_argument(
        "--limit", type=int, default=0, help="Maximum number of attachments to process"
    )
    parser_process_att.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent LLM workers for phase 2 (default 1)",
    )
    parser_process_att.set_defaults(func=cmd_process_attachments)

    # Process images command
    parser_process_img = subparsers.add_parser(
        "process-images", help="Backfill inline image classification"
    )
    parser_process_img.add_argument(
        "--since", type=str, help="Only process images from emails after YYYY-MM-DD"
    )
    parser_process_img.add_argument(
        "--limit", type=int, default=1000, help="Max images to process (default 1000)"
    )
    parser_process_img.add_argument(
        "--no-vision", action="store_true", help="Skip Stage 3 vision LLM"
    )
    parser_process_img.add_argument(
        "--dry-run", action="store_true", help="Count only, no classification"
    )
    parser_process_img.add_argument(
        "--reprocess",
        action="store_true",
        help="Re-scan all images (default: only messages not yet processed)",
    )
    parser_process_img.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent vision workers (default 1; >1 parallelizes the slow LLM calls)",
    )
    parser_process_img.set_defaults(func=cmd_process_images)

    # Process SharePoint command
    parser_process_sp = subparsers.add_parser(
        "process-sharepoint", help="Scan emails for SharePoint URLs and fetch them"
    )
    parser_process_sp.add_argument("--since", type=str, help="Only scan emails after YYYY-MM-DD")
    parser_process_sp.add_argument(
        "--limit", type=int, default=0, help="Max emails to scan (0 = all, default 0)"
    )
    parser_process_sp.add_argument(
        "--dry-run", action="store_true", help="Scan and count only, no fetching"
    )
    parser_process_sp.set_defaults(func=cmd_process_sharepoint)

    # reverse-ingest command
    parser_reverse = subparsers.add_parser(
        "reverse-ingest",
        help="Scan ~/Documents and ingest the latest version of each logical document",
    )
    parser_reverse.add_argument(
        "--root",
        action="append",
        type=Path,
        help="Directory to scan (repeatable; defaults to ~/Documents/National + ~/Documents/Personal)",
    )
    parser_reverse.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel LLM workers for Phase 2 (default: 4)",
    )
    parser_reverse.add_argument(
        "--dry-run",
        action="store_true",
        help="List survivors without ingesting",
    )
    parser_reverse.add_argument(
        "--verbose",
        action="store_true",
        help="Per-file log lines",
    )
    parser_reverse.set_defaults(func=cmd_reverse_ingest)

    # Load command
    parser_load = subparsers.add_parser("load", help="Load extractions into database")
    parser_load.set_defaults(func=cmd_load)

    # Prune-staged command (one-shot backfill; auto-prune already runs in `load`)
    parser_prune_staged = subparsers.add_parser(
        "prune-staged",
        help="Delete batch-*.json files in data/staging whose message_ids are all in the DB",
    )
    parser_prune_staged.set_defaults(func=cmd_prune_staged)

    # Query subcommands
    parser_query = subparsers.add_parser("query", help="Query the knowledge store")
    query_subparsers = parser_query.add_subparsers(dest="query_type", help="Query type")

    # Common query options
    def add_common_query_args(p):
        p.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
        p.add_argument("-v", "--verbose", action="store_true", help="Show full details")

    # Query thread
    parser_query_thread = query_subparsers.add_parser("thread", help="Show conversation thread")
    parser_query_thread.add_argument("email_id", type=int, help="Email ID")
    add_common_query_args(parser_query_thread)
    parser_query_thread.set_defaults(func=cmd_query_thread)

    # Query by person
    parser_query_person = query_subparsers.add_parser("person", help="Query by person")
    parser_query_person.add_argument("name", help="Person name or email")
    add_common_query_args(parser_query_person)
    parser_query_person.set_defaults(func=cmd_query_person)

    # Query by topic
    parser_query_topic = query_subparsers.add_parser("topic", help="Query by topic")
    parser_query_topic.add_argument("topic", help="Topic name")
    add_common_query_args(parser_query_topic)
    parser_query_topic.set_defaults(func=cmd_query_topic)

    # Query by keyword
    parser_query_keyword = query_subparsers.add_parser("keyword", help="Full-text search")
    parser_query_keyword.add_argument("keyword", help="Search keyword")
    add_common_query_args(parser_query_keyword)
    parser_query_keyword.set_defaults(func=cmd_query_keyword)

    # Query by date range
    parser_query_date = query_subparsers.add_parser("date", help="Query by date range")
    parser_query_date.add_argument("start", help="Start date (YYYY-MM-DD)")
    parser_query_date.add_argument("end", help="End date (YYYY-MM-DD)")
    add_common_query_args(parser_query_date)
    parser_query_date.set_defaults(func=cmd_query_date)

    # Query decisions
    parser_query_decisions = query_subparsers.add_parser("decisions", help="Query decisions")
    parser_query_decisions.add_argument("--topic", help="Filter by topic")
    parser_query_decisions.add_argument("--person", help="Filter by person (decided_by)")
    parser_query_decisions.add_argument("--limit", type=int, default=20, help="Max results")
    parser_query_decisions.set_defaults(func=cmd_query_decisions)

    # Query action items
    parser_query_actions = query_subparsers.add_parser("actions", help="Query action items")
    parser_query_actions.add_argument("--owner", help="Filter by owner")
    parser_query_actions.add_argument(
        "--status", default="open", help="Filter by status (default: open)"
    )
    parser_query_actions.add_argument("--limit", type=int, default=20, help="Max results")
    parser_query_actions.set_defaults(func=cmd_query_actions)

    # Query combined
    parser_query_combined = query_subparsers.add_parser(
        "combined", help="Query with multiple filters"
    )
    parser_query_combined.add_argument("--person", help="Filter by person")
    parser_query_combined.add_argument("--topic", help="Filter by topic")
    parser_query_combined.add_argument("--keyword", help="Full-text search keyword")
    parser_query_combined.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser_query_combined.add_argument("--end", help="End date (YYYY-MM-DD)")
    add_common_query_args(parser_query_combined)
    parser_query_combined.set_defaults(func=cmd_query_combined)

    # Prep command (meeting preparation)
    parser_prep = subparsers.add_parser("prep", help="Generate meeting prep dossier")
    parser_prep.add_argument("people", help="Comma-separated list of attendee names or emails")
    parser_prep.add_argument("--topic", help="Meeting topic for focused context")
    parser_prep.add_argument(
        "--days", type=int, default=365, help="Lookback period in days (default: 365)"
    )
    parser_prep.add_argument(
        "--limit", type=int, default=10, help="Max emails per person (default: 10)"
    )
    parser_prep.add_argument("-v", "--verbose", action="store_true", help="Show email summaries")
    parser_prep.set_defaults(func=cmd_prep)

    # Sync command (incremental)
    parser_sync = subparsers.add_parser("sync", help="Incremental sync (export + extract + load)")
    parser_sync.add_argument("--limit", type=int, help="Max emails to process")
    parser_sync.add_argument(
        "--engine",
        choices=["gemini", "claude"],
        default=None,
        help=f"LLM engine for extraction (default: {EXTRACT_ENGINE})",
    )
    parser_sync.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent extraction workers (default: 1)",
    )
    parser_sync.add_argument(
        "--skip-export",
        action="store_true",
        help="Skip Apple Mail export (use already-staged batches from outlook-cli). "
        "Lets a midday catch-up run without restarting Mail.app.",
    )
    parser_sync.set_defaults(func=cmd_sync)

    # teams-sync command
    parser_teams_sync = subparsers.add_parser("teams-sync", help="Ingest Teams channels (Phase 1)")
    parser_teams_sync.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Max parallel chat pulls (default: 2)",
    )
    parser_teams_sync.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent extraction workers (default: 4)",
    )
    parser_teams_sync.add_argument(
        "--skip-pull",
        action="store_true",
        help="Skip Steps 1+2 (re-extract / re-embed only)",
    )
    parser_teams_sync.add_argument("--limit", type=int, help="Max threads to extract this run")
    parser_teams_sync.set_defaults(func=cmd_teams_sync)

    parser_teams_search = subparsers.add_parser("teams-search", help="Search Teams content")
    parser_teams_search.add_argument("query", help="Search query")
    parser_teams_search.add_argument(
        "--kind", choices=["thread", "message", "both"], default="both"
    )
    parser_teams_search.add_argument("--limit", type=int, default=20)
    parser_teams_search.set_defaults(func=cmd_teams_search)

    parser_teams_thread = subparsers.add_parser("teams-thread", help="Show full Teams thread")
    parser_teams_thread.add_argument("thread_id", type=int)
    parser_teams_thread.set_defaults(func=cmd_teams_thread)

    parser_teams_chat = subparsers.add_parser(
        "teams-chat", help="Recent activity for a Teams chat/channel"
    )
    parser_teams_chat.add_argument("chat_id", type=int)
    parser_teams_chat.add_argument("--days", type=int, default=30)
    parser_teams_chat.set_defaults(func=cmd_teams_chat)

    parser_teams_stats = subparsers.add_parser("teams-stats", help="Teams ingestion statistics")
    parser_teams_stats.set_defaults(func=cmd_teams_stats)

    # calendar-sync command
    parser_cal = subparsers.add_parser("calendar-sync", help="Sync calendar events from Outlook")
    parser_cal.add_argument(
        "--backfill",
        action="store_true",
        help="Backfill mode (default: 12 months back)",
    )
    parser_cal.add_argument("--since", type=str, help="Start date ISO (e.g. 2025-05-14)")
    parser_cal.add_argument("--until", type=str, help="End date ISO (default: now + 30d)")
    parser_cal.add_argument(
        "--skip-extraction", action="store_true", help="Skip LLM body extraction"
    )
    parser_cal.set_defaults(func=cmd_calendar_sync)

    # news-sync command (stages batches only; `sync` drains them)
    from src.export.news_export import DEFAULT_RELEVANCE_THRESHOLD, PIPELINES

    parser_news = subparsers.add_parser(
        "news-sync", help="Stage news-reader digests and articles into data/staging"
    )
    parser_news.add_argument(
        "--news-db",
        type=Path,
        default=NEWS_DB_PATH,
        help=f"News database path (default: {NEWS_DB_PATH})",
    )
    parser_news.add_argument(
        "--relevance",
        type=int,
        default=DEFAULT_RELEVANCE_THRESHOLD,
        help=f"Minimum article relevance score (default: {DEFAULT_RELEVANCE_THRESHOLD})",
    )
    parser_news.add_argument(
        "--pipeline",
        action="append",
        choices=PIPELINES,
        help="Pipeline to include, repeatable (default: all)",
    )
    parser_news.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max total records to stage, syntheses first (default: 0 = all)",
    )
    parser_news.add_argument(
        "--dry-run", action="store_true", help="Report what would be staged, write nothing"
    )
    parser_news.set_defaults(func=cmd_news_sync)

    # Migrate command
    parser_migrate = subparsers.add_parser("migrate", help="Run database migrations")
    parser_migrate.set_defaults(func=cmd_migrate)

    # Import people command
    parser_import = subparsers.add_parser("import-people", help="Import canonical people from JSON")
    parser_import.add_argument(
        "--file",
        help="Path to canonical_people.json (default: data/canonical_people.json)",
    )
    parser_import.set_defaults(func=cmd_import_people)

    # Stale command
    parser_stale = subparsers.add_parser("stale", help="Find stale threads and overdue actions")
    parser_stale.add_argument(
        "--days", type=int, default=5, help="Stale threshold in days (default: 5)"
    )
    parser_stale.set_defaults(func=cmd_stale)

    # Embed command (build embedding index)
    parser_embed = subparsers.add_parser("embed", help="Build embedding index for semantic search")
    parser_embed.add_argument(
        "--force", action="store_true", help="Rebuild all embeddings from scratch"
    )
    parser_embed.set_defaults(func=cmd_embed)

    # Query semantic
    parser_query_semantic = query_subparsers.add_parser("semantic", help="Semantic search")
    parser_query_semantic.add_argument("query", help="Natural language search query")
    add_common_query_args(parser_query_semantic)
    parser_query_semantic.set_defaults(func=cmd_query_semantic)

    # Ingest command (standalone documents)
    parser_ingest = subparsers.add_parser(
        "ingest", help="Ingest standalone documents or URLs into the knowledge store"
    )
    parser_ingest.add_argument("files", nargs="*", help="File paths to ingest")
    parser_ingest.add_argument(
        "--url",
        action="append",
        default=[],
        help="URL(s) to fetch and ingest (repeatable)",
    )
    parser_ingest.add_argument(
        "--title",
        type=str,
        default=None,
        help="Title for the ingested content (auto-detected if omitted)",
    )
    parser_ingest.add_argument(
        "--source",
        type=str,
        default=None,
        help='Source label (e.g. "Revolut Annual Report 2024")',
    )
    parser_ingest.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Concurrent LLM workers for Phase 2 (default 1)",
    )
    parser_ingest.set_defaults(func=cmd_ingest)

    # Stats command
    parser_stats = subparsers.add_parser("stats", help="Show database statistics")
    parser_stats.set_defaults(func=cmd_stats)

    # Parse and execute
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "query" and not args.query_type:
        parser_query.print_help()
        sys.exit(1)

    # Execute command
    try:
        # Before any command runs, so the shared policy's budget test reflects this
        # unit's real TimeoutStartSec instead of resolve_deadline's flat 900s default.
        # A no-op off systemd, and inside the try on purpose: a unit whose timeout
        # cannot fund one worst-case LLM call raises here, and exiting 1 with the
        # reason on stderr is the loud refusal that beats a SIGTERM later.
        install_llm_deadline_for_this_process()
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
