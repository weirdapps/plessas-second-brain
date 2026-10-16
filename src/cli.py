"""CLI interface for the second-brain knowledge store.

Provides command-line access to export, extract, load, and query operations.
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from src.config import (
    ATTACHMENTS_DIR,
    DATA_ROOT,
    DEFAULT_DB,
    EXTRACT_ENGINE,
    NEWS_DB_PATH,
    SHAREPOINT_DATA_DIR,
)
from src.llm_deadline import install_llm_deadline_for_this_process
from src.store.file_sweep import SWEEP_POLICY_FILE


def _token_budget(args):
    """The run's token budget: --token-budget, else BRAIN_ATTACHMENT_TOKEN_BUDGET; None for no
    limit (neither set, or 0). ValueError for a value that is not a whole number of 0 or more."""
    from src.llm_cost import TokenBudget

    limit = getattr(args, "token_budget", None)
    if limit is None:
        raw = os.environ.get("BRAIN_ATTACHMENT_TOKEN_BUDGET", "").strip()
        try:
            limit = int(raw) if raw else 0
        except ValueError:
            limit = -1
        if limit < 0:
            raise ValueError(
                f"BRAIN_ATTACHMENT_TOKEN_BUDGET must be a whole number of tokens, not {raw!r}"
            )
    if limit < 0:
        raise ValueError(f"--token-budget must be 0 or more, not {limit}")
    return TokenBudget(limit) if limit > 0 else None


def _print_estimate(est: dict) -> None:
    """The lines a dry-run estimate prints (src/extract/attachment_pipeline.py estimate_rows)."""
    from src.llm_cost import OUTPUT_TOKENS_PER_CALL

    print(f"  attachments   : {est['rows']:,}")
    print(f"  calls         : {est['calls']:,}")
    print(
        f"  input tokens  : {est['input_tokens']:,}"
        f" (at {est['chars_per_token']:g} characters a token)"
    )
    print(f"  output tokens : {est['output_tokens']:,} ({OUTPUT_TOKENS_PER_CALL:,} a call)")
    print(
        f"  cost          : ${est['cost_usd']:,.2f} at {est['model']} Vertex eu rates"
        f" (${est['batch_cost_usd']:,.2f} as a batch job)"
    )


def cmd_process_attachments(args):
    """Extract text and structured data from attachments."""
    from src.extract.attachment_pipeline import (
        estimate_phase2,
        record_text_pass,
        run_phase1,
        run_phase2,
    )
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

    if getattr(args, "estimate", False):
        # Phase 1 reads files and calls no model; what a run would spend is Phase 2's.
        print("Phase 2 estimate, pending attachments (no model call made):")
        _print_estimate(estimate_phase2(db_path, limit=limit, file_type=file_type))
        return 0
    # Checked before Phase 1 when Phase 2 will run, so a bad value stops the run before any
    # work; a Phase 1 run does not read it.
    budget = None
    if phase is None or phase == 2:
        try:
            budget = _token_budget(args)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
    if getattr(args, "batch", False) or getattr(args, "batch_collect", False):
        from src.extract.vertex_batch import cli_process_attachments

        return cli_process_attachments(args, db_path, budget)

    # Bounds the run for a caller under an external timeout — the nightly job
    # has TimeoutStartSec=1h and now carries the whole backfill, where a count
    # limit is no safer than it was in the hourly sync.
    deadline_s = getattr(args, "deadline_s", None)

    # A phase that failed everything it tried. The exit code was 0 whatever the
    # counts, and run_stage in sb-attachment-pass.sh judges only the exit code,
    # so a night where every attachment failed was logged as a passing stage.
    # Failures beside successes are per-item, and the other phase still runs.
    phase_failed = False

    if phase is None or phase == 1:
        print("Phase 1: Local text extraction...")
        stats = run_phase1(db_path, limit=limit, file_type=file_type, deadline_s=deadline_s)
        print(f"  Processed: {stats['processed']}")
        print(f"  Extracted: {stats['extracted']}")
        print(f"  Failed: {stats['failed']}")
        print(f"  Skipped: {stats['skipped']}")
        if stats.get("encrypted"):
            print(f"  Encrypted (no text without a key): {stats['encrypted']}")
        if stats.get("reused"):
            print(f"  Reused (same file): {stats['reused']}")
        if stats.get("deferred"):
            print(f"  Deferred (out of time): {stats['deferred']}")
        phase_failed |= stats["failed"] > 0 and stats["extracted"] == 0
        if file_type is None:
            # A pass over every kind: while one runs, the hourly sync leaves it the images and
            # archives (attachment_pipeline.TEXT_PASS_KEY).
            conn = get_connection(db_path)
            try:
                record_text_pass(conn)
            finally:
                conn.close()

    if phase is None or phase == 2:
        workers = getattr(args, "workers", 1) or 1
        print(f"\nPhase 2: Vertex AI structured extraction (workers={workers})...")
        stats = run_phase2(
            db_path,
            limit=limit,
            file_type=file_type,
            workers=workers,
            deadline_s=deadline_s,
            token_budget=budget,
        )
        print(f"  Processed: {stats['processed']}")
        print(f"  Extracted: {stats['extracted']}")
        print(f"  Failed: {stats['failed']}")
        if stats.get("deferred"):
            print(f"  Deferred (out of time): {stats['deferred']}")
        if stats.get("over_budget"):
            # A planned stop, not a failure: the rest stay pending for the next run.
            print(f"  Left pending (token budget reached): {stats['over_budget']}")
        if budget is not None:
            print(
                f"  Token budget: {budget.spent:,} of {budget.limit:,} tokens spent"
                f" in {budget.calls:,} calls"
            )
        phase_failed |= stats["failed"] > 0 and stats["extracted"] == 0

    print("\nAttachment processing complete.")
    if phase_failed:
        print("A phase failed every attachment it tried", file=sys.stderr)
        return 1
    return 0


def cmd_register_attachments(args):
    """Bulk-register attachments outlook-cli has already downloaded.

    The counterpart to the hourly sync's Step 3b, which is deliberately confined
    to a recent window so it fits TimeoutStartSec=600. This one has no window
    and runs from the nightly job, whose budget is an hour, so the backlog left
    by the 2026-06-30 VPS cutover drains here instead of starving the sync.
    """
    from src.export.outlook_attachments import register_downloaded_attachments
    from src.store.schema import get_connection as get_conn

    db_path = args.db or str(DEFAULT_DB)
    print("Registering downloaded attachments...")
    conn = get_conn(db_path)
    try:
        stats = register_downloaded_attachments(
            conn, ATTACHMENTS_DIR, limit=args.limit if args.limit > 0 else None
        )
    finally:
        conn.close()
    print(
        f"  Registered: {stats['registered']:,} from {stats['scanned']:,} message dirs\n"
        f"  Awaiting their email: {stats['deferred']:,}"
    )


def cmd_hash_attachments(args):
    """Record a content hash for every attachment whose file is still on disk.

    Run before anything deletes attachment files: afterwards the hash is the only evidence
    of what a file held. Resumable; see src/store/file_hashes.py.
    """
    from src.store.file_hashes import hash_attachments
    from src.store.schema import get_connection as get_conn

    conn = get_conn(str(args.db or DEFAULT_DB))
    try:
        stats = hash_attachments(conn, limit=args.limit if args.limit > 0 else None)
        left = conn.execute("SELECT COUNT(*) FROM attachments WHERE sha256 IS NULL").fetchone()[0]
    finally:
        conn.close()
    print(
        f"  Hashed: {stats['hashed']:,}  file missing: {stats['missing']:,}"
        f"  still unhashed: {left:,}"
    )
    return 0


def cmd_mail_reconcile(args):
    """List the Outlook messages the store lacks; on the producer, stage them again.

    The report only reads the store, so it runs on a replica. --record-aliases,
    --refetch and --health-json write, and are the producer's
    (src/export/mail_reconcile.py). Exit codes follow the M365 convention: 4
    re-authenticate, 5 outlook-cli misbehaved. --health-json writes the mail-loss
    counters whatever the reconcile's outcome, so they stay fresh when it fails.
    """
    import subprocess
    from datetime import UTC

    from src import config
    from src.export import mail_reconcile as mr
    from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError
    from src.export.state import write_json_atomic

    writes = args.refetch or args.record_aliases or args.health_json is not None
    if writes and config.is_replica():
        print(
            config.replica_refusal("mail-reconcile --refetch/--record-aliases/--health-json"),
            file=sys.stderr,
        )
        return 2
    if not Path(args.db).exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        return 1
    now = datetime.now(UTC)
    since = mr.parse_since(args.since, now)
    until = mr.parse_since(args.until, now) if args.until else now
    if since is None or until is None:
        print(
            "Error: --since and --until take ISO dates, e.g. 2025-03-16, or a time"
            " before now, e.g. 7d or 36h",
            file=sys.stderr,
        )
        return 1
    folders = [name.strip() for name in args.folders.split(",") if name.strip()]
    report = None
    stats = None
    error = None
    rc = 0
    try:
        report = mr.reconcile(args.db, folders, since, until, DATA_ROOT / "staging")
        _print_reconcile(report)
        if args.json:
            write_json_atomic(Path(args.json), report.as_json())
            print(f"Report written to {args.json}")
        if args.record_aliases:
            from src.store.schema import get_connection as get_conn

            conn = get_conn(str(args.db))
            try:
                added = mr.record_aliases(conn, report.copies)
            finally:
                conn.close()
            print(f"Aliases: {added:,} new of {len(report.copies):,} copies found by Message-ID")
        if args.refetch:
            stats = mr.refetch(report.missing, concurrency=args.concurrency, limit=args.limit)
            print(
                f"Refetch: staged {stats['staged']:,} of {stats['requested']:,} for the next sync,"
                f" {len(stats['failed']):,} failed, attachments for {stats['attachments']:,}"
            )
            for message_id in stats["failed"][:20]:
                print(f"  failed: {message_id}")
    except OutlookCliAuthRequired as e:
        print(f"outlook-cli needs re-authentication: {e.stderr}", file=sys.stderr)
        rc, error = 4, "outlook-cli needs re-authentication (exit 4)"
    except (OutlookCliError, subprocess.TimeoutExpired, mr.TruncatedListing) as e:
        print(f"outlook-cli failed: {e}", file=sys.stderr)
        rc, error = 5, f"outlook-cli failed ({type(e).__name__})"
    if args.health_json is not None:
        written = mr.write_mail_loss(
            Path(args.health_json) if args.health_json else None,
            report=report,
            refetch_stats=stats,
            error=error,
        )
        print(
            f"Mail-loss counters: {written['fetch_gave_up']:,} given up,"
            f" {written['quarantined']:,} quarantined"
        )
    return rc


def cmd_retry_stubs(args):
    """Extract again the emails loaded as stubs, and replace each stub in place.

    See src/extract/stub_retry.py. Exits 1 when an email failed again, 75 when
    quota or an expired credential ended the run.
    """
    from src.extract.stub_retry import retry_stubs, select_stubs
    from src.store.schema import get_connection as get_conn

    conn = get_conn(str(args.db))
    try:
        if args.dry_run:
            stubs = select_stubs(conn, args.since, args.limit)
            print(f"{len(stubs):,} stub(s) would be extracted again")
            for email_id, message_id in stubs[:20]:
                print(f"  email {email_id}: {message_id}")
            return 0
        stats = retry_stubs(
            conn, DATA_ROOT / "extracted", args.engine or EXTRACT_ENGINE, args.since, args.limit
        )
    finally:
        conn.close()
    print(
        f"{stats['stubs']:,} stub(s): {stats['replaced']:,} extracted and replaced,"
        f" {stats['failed']:,} failed"
        + (" (stopped: quota or credentials)" if stats["stopped"] else "")
    )
    if stats["stopped"]:
        return 75
    return 1 if stats["failed"] else 0


def _print_reconcile(report) -> None:
    from collections import Counter

    cutoff = report.cutoff.isoformat() if report.cutoff else "none"
    print(
        f"Outlook against the store, {report.since:%Y-%m-%d %H:%M} to {report.until:%Y-%m-%d %H:%M}"
        f" UTC (the store's newest mail: {cutoff})"
    )
    kinds = ("listed", "by_id", "by_rfc822_id", "by_subject_time", "staged", "newer", "missing")
    print("  " + f"{'folder':<12}" + "".join(f"{k:>16}" for k in kinds))
    for folder, counts in report.counts.items():
        print("  " + f"{folder:<12}" + "".join(f"{counts.get(k, 0):>16,}" for k in kinds))
    months = Counter(str(m.get("ReceivedDateTime") or "")[:7] for m in report.missing)
    print(
        f"Missing: {len(report.missing):,}"
        + (f" by month {dict(sorted(months.items()))}" if months else "")
    )
    for m in report.missing[:20]:
        print(
            f"  {str(m.get('ReceivedDateTime') or '')[:16]}  {m['folder']:<10}"
            f" {str(m.get('Subject') or '')[:70]}"
        )
    if len(report.missing) > 20:
        print(f"  ... and {len(report.missing) - 20:,} more (--json writes them all)")


def cmd_ingest_session_notes(args):
    """Store what Claude sessions wrote to notes (src/export/session_notes.py)."""
    from src.export.session_notes import ingest_session_notes, transcripts_to_scan
    from src.store.schema import get_connection, run_migrations

    conn = get_connection(str(args.db))
    try:
        run_migrations(conn)
        files = transcripts_to_scan(conn, all_files=args.all)
        stats = ingest_session_notes(conn, files)
    finally:
        conn.close()
    print(
        f"session notes: {len(files):,} transcripts, "
        + ", ".join(f"{k} {v:,}" for k, v in stats.items())
    )
    return 0


def cmd_reextract(args):
    """Read again and summarise again rows earlier code capped, skipped or could not read."""
    from src.extract.reextract import SELECTORS, estimate_reextract, reextract

    which = {name for name in SELECTORS if getattr(args, name, False)}
    full_parts = getattr(args, "full_parts", None) or None
    if getattr(args, "batch_collect", False) or getattr(args, "probe", False):
        from src.extract.vertex_batch import cli_reextract

        return cli_reextract(args, which)
    if not which and not full_parts:
        print(
            "Error: choose at least one of "
            + ", ".join(f"--{name}" for name in SELECTORS)
            + ", or --full-parts",
            file=sys.stderr,
        )
        return 2
    chosen = ", ".join(sorted(which) + (["full parts"] if full_parts else []))
    no_spreadsheets = getattr(args, "no_spreadsheets", False)
    if no_spreadsheets:
        chosen += ", no spreadsheets"
    if getattr(args, "estimate", False):
        est = estimate_reextract(
            args.db,
            which,
            limit=args.limit or None,
            after_id=getattr(args, "after_id", 0) or None,
            full_parts=full_parts,
            no_spreadsheets=no_spreadsheets,
        )
        print(f"reextract ({chosen}) ESTIMATE (no model call made, nothing changed):")
        print(f"  selected      : {est['selected']:,}")
        print(f"  no text yet   : {est['unknown']:,} (read first, so not estimated)")
        if "duplicates" in est:
            print(f"  duplicates    : {est['duplicates']:,} (take a copy, so not estimated)")
        _print_estimate(est)
        return 0
    try:
        budget = _token_budget(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    batch = getattr(args, "batch", False)
    if batch:
        from src.extract.vertex_batch import config_problem

        problem = config_problem()
        if problem:
            print(f"Error: {problem}", file=sys.stderr)
            return 2
    stats = reextract(
        args.db,
        which,
        limit=args.limit or None,
        dry_run=args.dry_run,
        workers=args.workers,
        root=args.root,
        after_id=getattr(args, "after_id", 0) or None,
        full_parts=full_parts,
        token_budget=budget,
        batch=batch,
        no_spreadsheets=no_spreadsheets,
    )
    print(f"reextract ({chosen}){' DRY RUN' if args.dry_run else ''}:")
    for key in (
        "selected",
        "flagged",
        "reread",
        "resummarise",
        "missing",
        "kept",
        "unchanged",
        "relabelled",
        "ocr_close",
        "spreadsheets",
        "summarised",
        "duplicates",
        "failed",
        "over_budget",
    ):
        print(f"  {key:<12}: {stats.get(key, 0):,}")
    # No thousands separator: a batch runner passes this back as --after-id.
    print(f"  {'highest id':<12}: {stats['highest_id']}")
    if "batch" in stats:
        from src.extract.vertex_batch import print_submit

        print_submit(stats["batch"])
        if stats.get("duplicates_waiting"):
            print(
                f"  duplicates    : {stats['duplicates_waiting']:,} take the summary of their copy"
                " when the job is collected"
            )
    return 0


def cmd_ocr_regarble(args):
    """Find stored OCR text that is Greek read as Latin letters (src/extract/ocr_regarble.py)."""
    from src.extract.ocr_regarble import cmd

    return cmd(args)


def cmd_refetch_garbled(args):
    """Fetch again from the mailbox the swept originals of garbled OCR texts
    (src/export/refetch_garbled.py)."""
    from src.export.refetch_garbled import cmd

    return cmd(args)


def cmd_sharepoint_shells(args):
    """Forget the hollow SharePoint documents an old fetch left, and queue again the links whose
    content is not held (src/export/sharepoint_shells.py)."""
    from src.export.sharepoint_shells import cmd

    return cmd(args)


def cmd_calendar_bodies(args):
    """Store the text of the calendar events stored before it was kept
    (src/pipeline/calendar_bodies.py)."""
    from src.pipeline.calendar_bodies import run

    return run(args)


def cmd_topic_pages(args):
    """Write the pages of the live topics whose evidence is new (src/store/topic_pages.py)."""
    from src.pipeline.topic_pages import cmd

    return cmd(args)


def cmd_findings(args):
    """The findings sessions filed, and the rejected ones with why (src/store/findings.py)."""
    from src.store.findings import cmd

    return cmd(args)


def cmd_briefs(args):
    """One pass of the pre-meeting brief pilot (src/pipeline/briefs.py)."""
    from src.pipeline.briefs import run

    return run(args)


def cmd_figures_backfill(args):
    """Read every report edition's figures into the numbers registry (src/store/figures.py)."""
    from src.store.figures import cmd

    return cmd(args)


def cmd_utc_stamps(args):
    """Convert the attachment and sync stamps stored in local time to UTC (src/store/stamps.py)."""
    from src.store.stamps import cmd

    return cmd(args)


def cmd_sweep_files(args):
    """Delete attachment files whose content is already stored (src/store/file_sweep.py).

    Report-only unless --apply, or unless --policy is given and the policy file says apply.
    Explicit flags win over the file.
    """
    from src.store.file_sweep import SweepPolicy, load_policy, parse_timestamp, sweep_files
    from src.store.schema import get_connection as get_conn

    policy = load_policy() if args.policy else SweepPolicy()
    if args.apply:
        policy = SweepPolicy(apply=True, only_newer_than=policy.only_newer_than)
    if args.only_newer_than:
        policy = SweepPolicy(
            apply=policy.apply, only_newer_than=parse_timestamp(args.only_newer_than)
        )
    conn = get_conn(str(args.db or DEFAULT_DB))
    try:
        stats = sweep_files(
            conn,
            Path(args.root),
            policy,
            sharepoint_root=Path(sp) if (sp := getattr(args, "sharepoint_root", None)) else None,
        )
    finally:
        conn.close()
    cutoff = policy.only_newer_than.isoformat() if policy.only_newer_than else "none"
    verb = "deleted" if policy.apply else "would delete"
    print(f"file sweep: {'APPLIED' if policy.apply else 'REPORT ONLY'} (only newer than: {cutoff})")
    print(f"  content stored, file removable : {stats['deletable']:,}")
    print(f"  of which older than the cutoff : {stats['before_cutoff']:,}")
    print(f"  {verb:<31}: {stats['to_delete']:,} ({stats['bytes_freed'] / 2**20:,.0f} MiB)")
    print(f"  awaiting text extraction       : {stats['pending-text']:,}")
    print(f"  unread (Phase 1 could not read) : {stats['unread']:,}")
    print(f"  content not held (kept)        : {stats['not-held']:,}")
    print(f"  images awaiting vision or text : {stats['pending-image']:,}")
    print(f"  unregistered (orphan reaper)   : {stats['unregistered']:,}")
    print(f"  directories removed            : {stats['dirs_removed']:,}")
    print(f"  delete errors                  : {stats['errors']:,}")
    return 0


# process-images transcribes the content images it leaves without text, inside the nightly
# attachment pass's hour: a few a day in steady state (~5% of content images have no OCR text),
# so a small slice keeps them current. A backlog is for `transcribe-images`.
TRANSCRIBE_NIGHTLY_LIMIT = 50
TRANSCRIBE_NIGHTLY_BUDGET_S = 300.0


def _print_transcription(stats: dict) -> None:
    print(f"  Transcribed: {stats['transcribed']} ({stats['empty']} showed no text)")
    print(f"  Failed: {stats['failed']}, deferred (out of time): {stats['deferred']}")
    print(f"  Missing files: {stats['missing']}")
    print(f"  Attachment rows written: {stats['projected']}")


def cmd_transcribe_images(args) -> int:
    """Transcribe content images whose attachments hold no text from their file."""
    from src.extract.image_pipeline import run_transcription
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found. Run 'brain load' first.")
        return 1
    conn = get_connection(db_path)
    try:
        run_migrations(conn)
        stats = run_transcription(
            conn,
            limit=args.limit or None,
            workers=args.workers,
            dry_run=args.dry_run,
            deadline_s=args.deadline_s,
        )
    finally:
        conn.close()

    print(f"Content images to transcribe (a file on disk): {stats['candidates']}")
    if args.dry_run:
        todo = min(stats["candidates"], args.limit) if args.limit else stats["candidates"]
        print(f"  Missing files: {stats['missing']}")
        print(f"  Would transcribe: {todo} (one vision call each)")
        return 0
    _print_transcription(stats)
    # Every call failed: a dead stage must not read as a quiet night.
    if stats["failed"] and not (stats["transcribed"] or stats["empty"]):
        return 1
    return 0


def cmd_process_images(args):
    """Backfill image classification pipeline."""
    from src.extract.image_pipeline import run_backfill, run_transcription
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
    transcription = None
    if not args.dry_run and not args.no_vision:
        transcription = run_transcription(
            conn,
            limit=TRANSCRIBE_NIGHTLY_LIMIT,
            workers=args.workers,
            deadline_s=TRANSCRIBE_NIGHTLY_BUDGET_S,
        )

    conn.close()

    print("\nImage processing complete:")
    print(f"  Scanned: {stats['scanned']}")
    print(f"  Classified: {stats['classified']}")
    if stats.get("recorded"):
        print(f"  Recorded from the hash (file gone): {stats['recorded']}")
    print(f"  Missing: {stats['missing']}")
    print(f"  Failed: {stats['failed']}")
    print(f"  Attachment rows given a description: {stats['projected']}")
    if transcription is not None:
        print("\nTranscription of content images:")
        _print_transcription(transcription)


# split-html holds a batch in memory until it writes it, so a batch ends at about
# this many characters of bodies, their text and their kept HTML (about twice as
# many bytes for Greek) as well as at --batch emails.
SPLIT_HTML_BATCH_CHARS = 64_000_000


def cmd_split_html(args) -> int:
    """Keep the HTML of emails loaded before schema v23 beside their text.

    Converts each HTML body to the text a reader sees, keeps the HTML compressed
    in email_html, and lets the index follow; a batch per transaction, so the
    writers that share the database wait seconds, not minutes, and a stopped run
    resumes where it was. Ends with an FTS optimize, which drops the deleted
    terms. The file only shrinks after a VACUUM.

    Credentials are redacted on the way, as the ingest path has done since #55:
    an older body can hold one, and in email_html it would be compressed, out of
    sight of the index and of a grep of the file. secure_delete zeroes the pages
    the rewrite frees, which the replica pull and the snapshots would copy. A dry
    run opens the database read-only, so it neither migrates nor converts.
    """
    import sqlite3

    from src.extract.html_text import LEADING
    from src.redact import redact_secrets
    from src.store.email_html import pack, split_body
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1
    if args.dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        migrated = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_html'"
        ).fetchone()
    else:
        conn = get_connection(db_path)
        run_migrations(conn)
        conn.execute("PRAGMA secure_delete = ON")
        migrated = True
    # A prefilter in SQL, on the same opening looks_like_html reads; it decides.
    not_kept = (
        "NOT EXISTS (SELECT 1 FROM email_html h WHERE h.email_id = e.id) AND " if migrated else ""
    )
    blanks = ", ".join(str(ord(c)) for c in LEADING)
    candidates = [
        row[0]
        for row in conn.execute(
            f"SELECT e.id FROM emails e WHERE {not_kept}"
            f"ltrim(e.content, char({blanks})) LIKE '<%' ORDER BY e.id"
        )
    ]
    converted = before = after = kept = 0
    size = max(1, args.batch)
    pos = 0
    while pos < len(candidates):
        # Read and convert with no lock held: the writers sharing the database
        # wait for the writes only, not for the parsing.
        ready: list[tuple[int, str, str | None, bytes, int]] = []
        held = 0
        while pos < len(candidates) and len(ready) < size and held < SPLIT_HTML_BATCH_CHARS:
            email_id = candidates[pos]
            pos += 1
            row = conn.execute("SELECT content FROM emails WHERE id = ?", (email_id,)).fetchone()
            if row is None or row[0] is None:
                continue
            content = row[0]
            body, html = split_body(redact_secrets(content))
            if html is not None:
                blob = pack(html)
                ready.append((email_id, content, body, blob, len(html.encode("utf-8"))))
                held += len(content) + len(body or "") + len(blob)
        for email_id, content, body, blob, html_bytes in ready:
            if not args.dry_run:
                # Unless the body changed since it was read: a re-load wins.
                if not conn.execute(
                    "UPDATE emails SET content = ? WHERE id = ? AND content = ?",
                    (body, email_id, content),
                ).rowcount:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO email_html (email_id, html) VALUES (?, ?)",
                    (email_id, blob),
                )
            converted += 1
            before += html_bytes
            after += len((body or "").encode("utf-8"))
            kept += len(blob)
        if not args.dry_run:
            conn.commit()
    verb = "would convert" if args.dry_run else "converted"
    print(
        f"split-html: {verb} {converted} email{'s' if converted != 1 else ''}: "
        f"bodies {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB of text, "
        f"HTML kept in {kept / 1e6:.1f} MB"
    )
    # Whenever HTML has been kept, not only when this run kept some: a run stopped
    # before its optimize left the words of what it redacted in the index.
    if not args.dry_run and (converted or conn.execute("SELECT 1 FROM email_html").fetchone()):
        conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('optimize')")
        conn.commit()
        print("  full-text index optimized; VACUUM returns the space to the disk")
    conn.close()
    return 0


def cmd_process_sharepoint(args):
    """Scan emails for SharePoint URLs and fetch them (src/pipeline/sharepoint.py)."""
    from src.pipeline import sharepoint

    return sharepoint.run(args)


def cmd_sharepoint_refused(args):
    """Deprecated: `brain tool sharepoint_index '{"operation": "list_stale"}'`."""
    return _deprecated_read(
        args, "sharepoint-refused", "sharepoint_index", {"operation": "list_stale"}
    )


def cmd_reverse_ingest(args):
    """Scan the directories you name, dedup latest-version-per-logical-name, ingest survivors.

    Explicit paths only. The scheduled scan of the OneDrive document trees was retired on
    2026-10-07: document text comes from mail, and a file saved from mail into those trees
    was ingested a second time as a twin of its attachment.
    """
    from src.extract.attachment_pipeline import ingest_document, run_phase1, run_phase2
    from src.ingest.reverse_scan import scan_roots, select_files_to_ingest, topic_label

    INGESTABLE_EXTENSIONS = {".pdf", ".pptx", ".xlsx", ".docx", ".md", ".txt"}

    roots = [Path(r).expanduser().resolve() for r in (args.root or [])]
    if not roots:
        print(
            "Error: name the directories to scan with --root (repeatable). The scheduled "
            "scan of the document trees was retired on 2026-10-07.",
            file=sys.stderr,
        )
        sys.exit(2)

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

    # The sessions in the database, with the end and turn count they were loaded
    # at: each is skipped unless it has gone on since.
    loaded = {}
    db_path = str(args.db)
    if Path(db_path).exists():
        try:
            conn = get_connection(db_path)
            rows = conn.execute("SELECT session_id, ended_at, turn_count FROM conversations")
            loaded = {r[0]: (r[1], r[2]) for r in rows}
            conn.close()
            if loaded:
                print(f"  {len(loaded)} conversations already in database (skipped unless grown)")
        except Exception:
            pass

    days = getattr(args, "days", None)
    if days:
        print(f"  Scanning last {days} days")

    result = export_conversations(
        days=days,
        limit=args.limit or 0,
        workspace_filter=getattr(args, "workspace", None),
        loaded=loaded,
        include_agents=getattr(args, "include_agents", False),
    )

    print(f"\nExported: {result['exported']}")
    print(f"Skipped: {result['skipped']}")
    if result["agents"]:
        print(f"Agent sessions left out: {result['agents']} (--include-agents keeps them)")
    print(f"Errors: {result['errors']}")
    if result["pending"]:
        print(f"Staged before, not loaded yet: {result['pending']}")
    if result["staged"]:
        print(f"Staged {len(result['staged'])} files in {Path(result['staged'][0]).parent}")


def cmd_extract_conversations(args):
    """Extract structured data from staged conversations."""
    from src.extract.local import run_conversation_extraction

    print("Starting conversation extraction...")
    run_conversation_extraction(
        workers=getattr(args, "workers", 1) or 1,
        limit=args.limit or 0,
    )
    print("Conversation extraction complete.")


def cmd_relabel_conversations(args) -> int:
    """Relabel the stored conversation turns the owner did not write as 'agent'.

    For what the exporter stored before it read who wrote a record; once, on the
    producer (src/export/conversation_relabel.py). A dry run opens the database
    read-only and only counts.
    """
    import sqlite3

    from src.config import CLAUDE_CODE_PROJECTS_DIR
    from src.export.conversation_relabel import relabel_conversations
    from src.store.schema import get_connection

    db_path = str(args.db)
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1
    if args.dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = get_connection(db_path)
    try:
        counts = relabel_conversations(
            conn, CLAUDE_CODE_PROJECTS_DIR, dry_run=args.dry_run, batch=args.batch
        )
    finally:
        conn.close()
    print(
        f"{counts['conversations']} conversations: {counts['by_transcript']} read against "
        f"their transcript, {counts['by_text']} by their text ({counts['misaligned']} of "
        "them did not match their transcript)"
    )
    print(
        f"{counts['turns_relabelled']} turns relabelled as agent"
        + (" (dry run: nothing written)" if args.dry_run else "")
    )
    print(
        f"{counts['agent_sessions_before']} agent sessions before, "
        f"{counts['agent_sessions_after']} after; their [PREFERENCE] facts, left out by "
        f"default: {counts['agent_preferences']}"
    )
    return 0


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


def cmd_tool(args):
    """Call an MCP tool by name with JSON arguments, or list the tools (src/mcp_call.py)."""
    from src import mcp_call

    return mcp_call.command(args.name, args.arguments, list_tools=args.list, db=args.db)


# The read commands that mirrored MCP tools by hand, deprecated on 2026-10-11 for one
# release (README, "Deprecated read commands"). Each prints one line on stderr naming the
# `brain tool` call that replaces it and makes that call, so its answer is the tool's JSON.
# A limit is held to the tools' 1..200, as the server holds a caller that skips the input
# schema; -v and prep's --limit are accepted and ignored.

# What `brain teams-stats` printed, as the one row of a sql_query: no tool counts these.
TEAMS_STATS_SQL = (
    "SELECT (SELECT COUNT(*) FROM teams_chats) AS total_teams_chats, "
    "(SELECT COUNT(*) FROM teams_threads) AS total_teams_threads, "
    "(SELECT COUNT(*) FROM teams_messages) AS total_teams_messages, "
    "(SELECT MIN(composed_at) FROM teams_messages) AS earliest_teams_message, "
    "(SELECT MAX(composed_at) FROM teams_messages) AS latest_teams_message, "
    "(SELECT COUNT(*) FROM teams_threads WHERE extraction_status = 'pending') "
    "AS teams_extraction_pending, "
    "(SELECT COUNT(*) FROM teams_threads WHERE extraction_status = 'failed') "
    "AS teams_extraction_failed"
)


def _limit(value: int) -> int:
    from src.mcp_server import MAX_LIMIT

    return max(1, min(int(value), MAX_LIMIT))


def _deprecated_read(args, old: str, tool: str, arguments: dict) -> int:
    """Answer the deprecated read command `old` with the tool it mirrored, after one line
    on stderr naming the `brain tool` call that replaces it. Arguments left unset are left
    out, so the tool's own defaults apply."""
    import json
    import shlex

    from src import mcp_call

    given = {key: value for key, value in arguments.items() if value is not None}
    words = ["brain"]
    if Path(args.db) != DEFAULT_DB:
        words += ["--db", str(args.db)]
    words += ["tool", tool]
    if given:
        words.append(json.dumps(given, ensure_ascii=False))
    print(
        f"brain {old} is deprecated (removed in the next release): use {shlex.join(words)}",
        file=sys.stderr,
    )
    return mcp_call.run(tool, given, db=args.db)


def cmd_query_person(args):
    """Deprecated: `brain tool query_emails` with a person."""
    return _deprecated_read(
        args, "query person", "query_emails", {"person": args.name, "limit": _limit(args.limit)}
    )


def cmd_query_topic(args):
    """Deprecated: `brain tool query_emails` with a topic."""
    return _deprecated_read(
        args, "query topic", "query_emails", {"topic": args.topic, "limit": _limit(args.limit)}
    )


def cmd_query_keyword(args):
    """Deprecated: `brain tool search_emails`; attachments are `brain tool search_attachments`."""
    return _deprecated_read(
        args, "query keyword", "search_emails", {"query": args.keyword, "limit": _limit(args.limit)}
    )


def cmd_query_date(args):
    """Deprecated: `brain tool query_emails` with a start and an end date."""
    return _deprecated_read(
        args,
        "query date",
        "query_emails",
        {"start_date": args.start, "end_date": args.end, "limit": _limit(args.limit)},
    )


def cmd_query_decisions(args):
    """Deprecated: `brain tool query_decisions`."""
    return _deprecated_read(
        args,
        "query decisions",
        "query_decisions",
        {"topic": args.topic, "person": args.person, "limit": _limit(args.limit)},
    )


def cmd_query_actions(args):
    """Deprecated: `brain tool query_actions`."""
    return _deprecated_read(
        args,
        "query actions",
        "query_actions",
        {"owner": args.owner, "status": args.status, "limit": _limit(args.limit)},
    )


def cmd_query_thread(args):
    """Deprecated: `brain tool email_thread`."""
    return _deprecated_read(
        args,
        "query thread",
        "email_thread",
        {"email_id": args.email_id, "limit": _limit(args.limit)},
    )


def cmd_query_combined(args):
    """Deprecated: `brain tool query_emails` with the filters given."""
    return _deprecated_read(
        args,
        "query combined",
        "query_emails",
        {
            "person": args.person,
            "topic": args.topic,
            "keyword": args.keyword,
            "start_date": args.start,
            "end_date": args.end,
            "limit": _limit(args.limit),
        },
    )


def cmd_prep(args):
    """Deprecated: `brain tool meeting_prep`."""
    return _deprecated_read(
        args,
        "prep",
        "meeting_prep",
        {"people": args.people, "topic": args.topic, "days": args.days},
    )


def cmd_sync(args):
    """Incremental sync over staged mail and the steps after it (src/pipeline/sync.py)."""
    from src.pipeline import sync

    return sync.run(args)


def cmd_teams_sync(args):
    """Ingest Teams channels and chats (src/pipeline/teams_sync.py)."""
    from src.pipeline import teams_sync

    return teams_sync.run(args)


def cmd_teams_backfill_replies(args):
    """Store the replies stored channel posts hold in their payloads; no Teams call.

    Channel replies came with every post and were dropped until #146, so they sit
    only in the posts' raw_json. One-off, on the producer: it stores them through
    the pull's own path, redaction included, and the threads that gain replies go
    back to extraction, which the next teams-sync runs under its deadline.
    """
    from src.export.teams_export import backfill_channel_replies
    from src.extract.teams_threads import bound_threads
    from src.store.schema import get_connection

    if not Path(args.db).exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        return 1
    conn = get_connection(str(args.db))
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'teams_messages'"
        ).fetchone():
            print("No Teams tables in this store: nothing to backfill.")
            return 0
        c = backfill_channel_replies(conn)
        print(f"Channel posts read: {c['posts_read']:,} ({c['unreadable']:,} unreadable)")
        print(
            f"Replies found: {c['replies_found']:,}; {c['replies_stored']:,} stored, "
            f"{c['replies_updated']:,} updated to a later version, "
            f"{c['replies_found'] - c['replies_stored'] - c['replies_updated']:,} already stored"
        )
        print(f"Posts dated by their own compose time: {c['posts_redated']:,}")
        b = bound_threads(conn)
        print(
            f"Threads: {b['threads_created']:,} new, {b['threads_updated']:,} queued for "
            "extraction (the next teams-sync extracts them)"
        )
    finally:
        conn.close()
    return 0


def cmd_teams_strip_html(args):
    """Convert Teams message text still stored as HTML; no Teams or model call.

    Ingest converted a message only when its payload declared "html", which the chat
    service never sends, so until #187 the text of every HTML message was its HTML.
    One-off, on the producer: it converts those rows from the HTML each keeps, by the
    rule ingest now uses, and the full-text index follows (see
    teams_export.strip_stored_html). A second run changes nothing. --dry-run opens the
    database read-only and counts; --limit caps the rows changed. It prints counts,
    never message text, and sends nothing back to extraction.
    """
    import sqlite3

    from src.export.teams_export import strip_stored_html
    from src.store.schema import get_connection

    if not Path(args.db).exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        return 1
    if args.dry_run:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    else:
        conn = get_connection(str(args.db))
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'teams_messages'"
        ).fetchone():
            print("No Teams tables in this store: nothing to convert.")
            return 0
        c = strip_stored_html(conn, limit=args.limit, dry_run=args.dry_run)
    finally:
        conn.close()
    print(f"teams-strip-html: {'DRY RUN (nothing written)' if args.dry_run else 'APPLIED'}")
    print(f"  {'scanned':<12}: {c['scanned']:,} (text holding '<' or '&')")
    print(f"  {'would change' if args.dry_run else 'changed':<12}: {c['changed']:,}")
    print(f"  {'unchanged':<12}: {c['unchanged']:,}")
    if c["stopped_at_limit"]:
        print(f"  stopped at --limit {args.limit:,}: run it again for the rest")
    return 0


def cmd_teams_drop_raw_json(args):
    """Drop the raw Teams payloads nothing needs any more (src/store/teams_raw_json.py).

    Once, on the producer, after teams-backfill-replies: a channel post whose payload still
    holds a reply not stored as a row keeps it, and a run after the backfill drops it.
    --dry-run opens the store read-only and changes nothing, the schema included.
    """
    import sqlite3

    from src.store.schema import get_connection, run_migrations
    from src.store.teams_raw_json import drop_raw_json

    if args.limit < 0:
        print(f"Error: --limit must be 0 or more, not {args.limit}", file=sys.stderr)
        return 2
    db_path = str(args.db)
    if not Path(db_path).exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        return 1
    if args.dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = get_connection(db_path)
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'teams_messages'"
        ).fetchone():
            print("No Teams tables in this store: nothing to drop.")
            return 0
        if not args.dry_run:
            # v34 first: under the triggers before it, each payload dropped re-indexed its
            # message's text.
            run_migrations(conn)
        c = drop_raw_json(conn, limit=args.limit, dry_run=args.dry_run)
    finally:
        conn.close()
    verb = "would drop" if args.dry_run else "dropped"
    print(f"teams-drop-raw-json: {verb} {c['dropped']:,} payloads ({c['bytes'] / 1e6:,.1f} MB)")
    if c["kept_replies"]:
        print(
            f"  kept {c['kept_replies']:,} channel posts whose payload holds a reply not stored"
            " as a row: run teams-backfill-replies, then this again"
        )
    if c["kept_unreadable"]:
        print(f"  kept {c['kept_unreadable']:,} channel posts whose payload does not parse")
    if c["left"]:
        print(f"  {c['left']:,} left past --limit {args.limit}")
    if c["dropped"] and not args.dry_run:
        print("  The file shrinks only after a VACUUM; until then SQLite reuses the pages.")
    return 0


def cmd_whatsapp_sync(args):
    """Ingest the pushed WhatsApp snapshot and its sessions (src/pipeline/whatsapp_sync.py)."""
    from src.pipeline import whatsapp_sync

    return whatsapp_sync.run(args)


def cmd_teams_search(args):
    """Deprecated: `brain tool search_teams`."""
    return _deprecated_read(
        args,
        "teams-search",
        "search_teams",
        {"query": args.query, "kind": args.kind, "limit": _limit(args.limit)},
    )


def cmd_teams_thread(args):
    """Deprecated: `brain tool teams_thread_context`."""
    return _deprecated_read(
        args, "teams-thread", "teams_thread_context", {"thread_id": args.thread_id}
    )


def cmd_teams_chat(args):
    """Deprecated: `brain tool teams_chat_summary`."""
    return _deprecated_read(
        args, "teams-chat", "teams_chat_summary", {"chat_id": args.chat_id, "days": args.days}
    )


def cmd_teams_stats(args):
    """Deprecated: `brain tool sql_query` with TEAMS_STATS_SQL; no tool counts these."""
    return _deprecated_read(args, "teams-stats", "sql_query", {"sql": TEAMS_STATS_SQL})


def cmd_calendar_sync(args):
    """Sync calendar events from Outlook into the store (src/pipeline/calendar_sync.py)."""
    from src.pipeline import calendar_sync

    return calendar_sync.run(args)


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


def cmd_classify_sources(args):
    """Give every stored email the class the rules give it today (src/store/source_class.py).

    The v33 migration and the loader set the class; this sets it again for rows stored before
    a change that moves it, such as BRAIN_USER_EMAIL_PATTERN being set after the migration ran.
    Writes only the rows whose class changes.
    """
    from src.store.schema import get_connection, run_migrations
    from src.store.source_class import CLASSES, reclassify

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found.")
        sys.exit(1)

    conn = get_connection(db_path)
    try:
        run_migrations(conn)
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            counts = reclassify(conn)
            conn.execute("COMMIT")
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    for name in CLASSES:
        print(f"{name}: {counts.get(name, 0)}")


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
    """Deprecated: `brain tool stale_threads`."""
    return _deprecated_read(
        args,
        "stale",
        "stale_threads",
        {"days": args.days, "max_days": args.max_days, "limit": _limit(args.limit)},
    )


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
    """Deprecated: `brain tool search_emails` with search_type semantic."""
    return _deprecated_read(
        args,
        "query semantic",
        "search_emails",
        {"query": args.query, "search_type": "semantic", "limit": _limit(args.limit)},
    )


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
    """Deprecated: `brain tool stats`, which adds freshness, coverage and every source."""
    return _deprecated_read(args, "stats", "stats", {})


# Subcommands that only read, and so may run on a replica. Everything else
# writes to the database or starts an export, and a new subcommand is refused
# there until it is added here.
READ_ONLY_COMMANDS = frozenset(
    {
        # The MCP tools read through the read-only connection; sharepoint_index's
        # refetch, the one that writes, refuses on a replica itself, as on the server.
        "tool",
        "query",
        "prep",
        "stale",
        "stats",
        "teams-search",
        "teams-thread",
        "teams-chat",
        "teams-stats",
        "prune-staged",
        # Its writes (--refetch, --record-aliases) refuse on a replica themselves.
        "mail-reconcile",
        "sharepoint-refused",
        # `brain maint scrub`: the dry run reads; --apply refuses on a replica itself (exit 3).
        "scrub",
        # --estimate reads; building pages refuses on a replica itself.
        "topic-pages",
        # --dry-run reads; a pass that may mail refuses on a replica itself.
        "briefs",
        "findings",
        # `brain maint figures-backfill`: the same; its dry run opens the store read-only.
        "figures-backfill",
    }
)

# One-off maintenance, grouped under `brain maint NAME` on 2026-10-11 so the jobs the
# timers run stand apart from what is run once, by hand, and can be retired on a
# schedule. NAME: (the command it runs, the flags it sets, its help). The old spelling,
# that command with those flags, works for one release with a deprecation line;
# docs/DEPLOY.md keeps it, since it also runs on code from before. `brain maint scrub`
# runs scripts/scrub_secrets.py (cmd_scrub).
MAINT_COMMANDS: dict[str, tuple[str, dict[str, bool], str]] = {
    "migrate": ("migrate", {}, "Run the database migrations"),
    "classify-sources": (
        "classify-sources",
        {},
        "Set every email's source_class again by today's rules",
    ),
    "split-html": ("split-html", {}, "Keep the HTML of emails loaded before schema v23"),
    "relabel-conversations": (
        "relabel-conversations",
        {},
        "Relabel the stored conversation turns the owner did not write as 'agent'",
    ),
    "teams-backfill-replies": (
        "teams-backfill-replies",
        {},
        "Store the channel replies stored posts hold (no Teams call)",
    ),
    "teams-strip-html": (
        "teams-strip-html",
        {},
        "Convert Teams message text stored as HTML (no Teams or model call)",
    ),
    "teams-drop-raw-json": (
        "teams-drop-raw-json",
        {},
        "Drop the raw Teams payloads nothing needs, after teams-backfill-replies",
    ),
    "ocr-regarble": (
        "ocr-regarble",
        {},
        "Find stored OCR text that is Greek read as Latin letters",
    ),
    "refetch-garbled": (
        "refetch-garbled",
        {},
        "Fetch again from the mailbox the swept originals of garbled OCR texts",
    ),
    "utc-stamps": (
        "utc-stamps",
        {},
        "Convert the attachment and sync stamps stored in local time to UTC",
    ),
    "calendar-bodies": (
        "calendar-bodies",
        {},
        "Fetch and store the text of the calendar events stored without it (no model call)",
    ),
    "topic-pages": (
        "topic-pages",
        {"build": True},
        "Write the live topics' pages again whether or not new evidence came in; --topic "
        "for one, --estimate to price it (topic-pages --build)",
    ),
    "sharepoint-shells": (
        "sharepoint-shells",
        {},
        "Forget the hollow SharePoint documents an old fetch left; queue the links not held",
    ),
    "reextract": (
        "reextract",
        {},
        "Read and summarise again rows earlier code capped, skipped or missed",
    ),
    "retry-stubs": (
        "retry-stubs",
        {},
        "Extract again the emails loaded without an extraction",
    ),
    "transcribe-images": (
        "transcribe-images",
        {},
        "Transcribe content images whose attachments hold no text",
    ),
    "hash-attachments": (
        "hash-attachments",
        {},
        "Record a content hash for every attachment file still on disk",
    ),
    "prune-staged": (
        "prune-staged",
        {},
        "Delete the staged batches whose message ids are all in the database",
    ),
    "import-people": ("import-people", {}, "Import canonical people from JSON"),
    "calendar-backfill": (
        "calendar-sync",
        {"backfill": True},
        "List the calendar a month at a time, the months stats coverage flags, resuming "
        "where the last backfill stopped (calendar-sync --backfill)",
    ),
    "sharepoint-ingest-fetched": (
        "process-sharepoint",
        {"ingest_fetched": True},
        "Store the files earlier SharePoint fetches left on disk "
        "(process-sharepoint --ingest-fetched)",
    ),
    "sharepoint-refetch-content": (
        "process-sharepoint",
        {"refetch_content": True},
        "Read again the SharePoint links recorded ok with no text stored "
        "(process-sharepoint --refetch-content)",
    ),
    "sharepoint-repair-links": (
        "process-sharepoint",
        {"repair_links": True},
        "Queue again the SharePoint links the old scanner mangled and the pages read "
        "without their text (process-sharepoint --repair-links)",
    ),
}


def _maint_name(args) -> str | None:
    """The `brain maint` name of what `args` ran under its old spelling, or None."""
    for name, (command, implied, _) in MAINT_COMMANDS.items():
        if args.command == command and all(
            getattr(args, key, None) == value for key, value in implied.items()
        ):
            return name
    return None


def cmd_scrub(args) -> int:
    """`brain maint scrub`: scripts/scrub_secrets.py with the arguments after it."""
    import importlib.util

    from src.config import REPO_ROOT

    if Path(args.db) != DEFAULT_DB:
        print(
            "Error: scrub takes no --db: it scrubs the configured database. Point "
            "BRAIN_DATA_DIR at a copy's directory to rehearse on the copy.",
            file=sys.stderr,
        )
        return 2
    spec = importlib.util.spec_from_file_location(
        "scrub_secrets", REPO_ROOT / "scripts" / "scrub_secrets.py"
    )
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script.main(args.script_args)


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
    parser_export_conv.add_argument(
        "--include-agents",
        action="store_true",
        help="Also export agent sessions (a teammate's, a subagent's, or one with no turn "
        "the owner typed), stored tagged and left out of every read by default",
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

    # Relabel stored conversations by who wrote each turn
    parser_relabel_conv = subparsers.add_parser(
        "relabel-conversations",
        help="Relabel the stored conversation turns the owner did not write as 'agent' (once)",
    )
    parser_relabel_conv.add_argument(
        "--dry-run", action="store_true", help="Count only, read-only: nothing written"
    )
    parser_relabel_conv.add_argument(
        "--batch", type=int, default=500, help="Turns per transaction (default 500)"
    )
    parser_relabel_conv.set_defaults(func=cmd_relabel_conversations)

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
    parser_process_att.add_argument(
        "--deadline-s",
        type=float,
        default=None,
        dest="deadline_s",
        help="Wall-clock budget in seconds; remaining work is deferred to the next run",
    )
    parser_process_att.add_argument(
        "--token-budget",
        type=int,
        default=None,
        dest="token_budget",
        help=(
            "Phase 2 stops before the call that would pass this many tokens (input and output);"
            " the rest stay pending. Default BRAIN_ATTACHMENT_TOKEN_BUDGET, else no limit; 0 = none"
        ),
    )
    parser_process_att.add_argument(
        "--estimate",
        action="store_true",
        help="Print the calls, tokens and cost Phase 2's pending work would take; run nothing",
    )
    parser_process_att.add_argument(
        "--batch",
        action="store_true",
        help="With --phase 2: send the calls as a Vertex AI batch job at half price"
        " (BRAIN_BATCH_BUCKET); --batch-collect stores the replies",
    )
    parser_process_att.add_argument(
        "--batch-collect",
        action="store_true",
        help="Store the replies of every finished batch job; the rest stay pending",
    )
    parser_process_att.set_defaults(func=cmd_process_attachments)

    # Bulk attachment registration (nightly counterpart to the sync's Step 3b)
    parser_register_att = subparsers.add_parser(
        "register-attachments",
        help="Record DB rows for attachments outlook-cli already downloaded",
    )
    parser_register_att.add_argument(
        "--limit", type=int, default=0, help="Max attachments to register (0 = no limit)"
    )
    parser_register_att.set_defaults(func=cmd_register_attachments)

    parser_hash_att = subparsers.add_parser(
        "hash-attachments",
        help="Record a content hash for every attachment file still on disk",
    )
    parser_hash_att.add_argument(
        "--limit", type=int, default=0, help="Max rows to hash (0 = no limit)"
    )
    parser_hash_att.set_defaults(func=cmd_hash_attachments)

    parser_reconcile = subparsers.add_parser(
        "mail-reconcile",
        help="List Outlook messages the store lacks; --refetch stages them (producer only)",
    )
    parser_reconcile.add_argument(
        "--since",
        required=True,
        help="Mail received from then on: an ISO date (2025-03-16) or a time before now (7d, 36h)",
    )
    parser_reconcile.add_argument(
        "--until", default=None, help="ISO date or a time before now, exclusive (default: now)"
    )
    parser_reconcile.add_argument(
        "--folders", default="Archive,Sent Items,Inbox", help="Comma-separated Outlook folders"
    )
    parser_reconcile.add_argument("--json", default=None, help="Write the report here")
    parser_reconcile.add_argument(
        "--refetch", action="store_true", help="Stage the missing messages for the next sync"
    )
    parser_reconcile.add_argument(
        "--record-aliases",
        action="store_true",
        help="Note the Graph id of each copy found by its Message-ID under another id",
    )
    parser_reconcile.add_argument(
        "--limit", type=int, default=0, help="--refetch at most this many (0 = all)"
    )
    parser_reconcile.add_argument(
        "--concurrency", type=int, default=2, help="get-mail calls at once (at most 2)"
    )
    parser_reconcile.add_argument(
        "--health-json",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Write the mail-loss counters the health check reads, even when the reconcile"
        " fails (producer only; default path: <data home>/state/mail_loss.json)",
    )
    parser_reconcile.set_defaults(func=cmd_mail_reconcile)

    parser_stubs = subparsers.add_parser(
        "retry-stubs",
        help="Extract again the emails loaded without an extraction, replacing each stub",
    )
    parser_stubs.add_argument("--since", default=None, help="Only mail received from this date")
    parser_stubs.add_argument("--limit", type=int, default=0, help="At most this many (0 = all)")
    parser_stubs.add_argument("--engine", default=None, help="claude or gemini (default: config)")
    parser_stubs.add_argument("--dry-run", action="store_true", help="List them; ask nothing")
    parser_stubs.set_defaults(func=cmd_retry_stubs)

    parser_sweep = subparsers.add_parser(
        "sweep-files",
        help="Delete attachment files whose content is already stored (report-only by default)",
    )
    parser_sweep.add_argument(
        "--apply", action="store_true", help="Actually delete (default: report only)"
    )
    parser_sweep.add_argument(
        "--only-newer-than", default=None, help="ISO time: delete only files modified since"
    )
    # A flag, not a path: no path typed on the command line reaches a file read.
    parser_sweep.add_argument(
        "--policy",
        action="store_true",
        help=f"Take apply and the cutoff from {SWEEP_POLICY_FILE}",
    )
    parser_sweep.add_argument(
        "--root", default=str(ATTACHMENTS_DIR), help="Attachments root to sweep"
    )
    parser_sweep.add_argument(
        "--sharepoint-root",
        default=str(SHAREPOINT_DATA_DIR),
        help="Where earlier SharePoint fetches left files, swept by content",
    )
    parser_sweep.set_defaults(func=cmd_sweep_files)

    parser_reextract = subparsers.add_parser(
        "reextract", help="Read and summarise again rows earlier code capped, skipped or missed"
    )
    parser_reextract.add_argument("--capped", action="store_true", help="text at the old 100k cap")
    parser_reextract.add_argument(
        "--long", action="store_true", help="summarised from the first 50k characters only"
    )
    parser_reextract.add_argument("--zip", action="store_true", help="zip archives once skipped")
    parser_reextract.add_argument(
        "--unread", action="store_true", help="rows Phase 1 recorded without reading the file"
    )
    parser_reextract.add_argument(
        "--partial",
        action="store_true",
        help="rows the old readers read in part: spreadsheets, scans, archives, 2M-char texts",
    )
    parser_reextract.add_argument(
        "--stale", action="store_true", help="skipped as unsupported before the sniffs landed"
    )
    parser_reextract.add_argument(
        "--formats",
        action="store_true",
        help=".mso/.wmz/.emz, failed .docx/.pptx, images that would not open",
    )
    parser_reextract.add_argument(
        "--ocr", action="store_true", help="scans and images whose OCR found too little text"
    )
    parser_reextract.add_argument(
        "--garbled", action="store_true", help="OCR text marked as Greek read as Latin letters"
    )
    parser_reextract.add_argument(
        "--wide",
        action="store_true",
        help="wide workbooks summarised from a digest that left out their newest columns",
    )
    parser_reextract.add_argument(
        "--no-spreadsheets",
        action="store_true",
        dest="no_spreadsheets",
        help="Leave workbooks (rows a spreadsheet reader read) out of the chosen rows",
    )
    parser_reextract.add_argument("--limit", type=int, default=0, help="Max rows (0 = all)")
    parser_reextract.add_argument(
        "--after-id",
        type=int,
        default=0,
        help="Only rows above this content id: the highest id a previous batch printed",
    )
    parser_reextract.add_argument(
        "--full-parts",
        type=int,
        nargs="+",
        default=None,
        dest="full_parts",
        metavar="ATTACHMENT_ID",
        help="Flag these attachments to be summarised from every part (up to 50) instead of 3,"
        " now and on every later summary, and summarise them again",
    )
    parser_reextract.add_argument(
        "--token-budget",
        type=int,
        default=None,
        dest="token_budget",
        help="Stop summarising before the call that would pass this many tokens;"
        " default BRAIN_ATTACHMENT_TOKEN_BUDGET, else no limit; 0 = none",
    )
    parser_reextract.add_argument(
        "--estimate",
        action="store_true",
        help="Print the calls, tokens and cost the chosen rows would take; change nothing",
    )
    parser_reextract.add_argument(
        "--batch",
        action="store_true",
        help="Send the summaries as a Vertex AI batch job at half price (BRAIN_BATCH_BUCKET)",
    )
    parser_reextract.add_argument(
        "--probe",
        action="store_true",
        help="With --batch: send ten requests from stored text, wait, print the replies and"
        " the cost; store nothing",
    )
    parser_reextract.add_argument(
        "--batch-collect",
        action="store_true",
        help="Store the replies of every finished batch job; the rest stay pending",
    )
    parser_reextract.add_argument("--dry-run", action="store_true", help="Count only")
    parser_reextract.add_argument("--workers", type=int, default=4, help="Phase 2 workers")
    parser_reextract.add_argument(
        "--root", default=str(ATTACHMENTS_DIR), help="Attachments root, for rows recorded elsewhere"
    )
    parser_reextract.set_defaults(func=cmd_reextract)

    parser_regarble = subparsers.add_parser(
        "ocr-regarble", help="Find stored OCR text that is Greek read as Latin letters"
    )
    regarble_mode = parser_regarble.add_mutually_exclusive_group()
    regarble_mode.add_argument("--dry-run", action="store_true", help="Count only (default)")
    regarble_mode.add_argument(
        "--apply", action="store_true", help="Mark them and clear what their summaries wrote"
    )
    parser_regarble.add_argument(
        "--root", default=str(ATTACHMENTS_DIR), help="Attachments root, for rows recorded elsewhere"
    )
    parser_regarble.set_defaults(func=cmd_ocr_regarble)

    parser_utc_stamps = subparsers.add_parser(
        "utc-stamps", help="Convert the attachment and sync stamps stored in local time to UTC"
    )
    utc_stamps_mode = parser_utc_stamps.add_mutually_exclusive_group()
    utc_stamps_mode.add_argument("--dry-run", action="store_true", help="Count only (default)")
    utc_stamps_mode.add_argument("--apply", action="store_true", help="Convert them")
    parser_utc_stamps.set_defaults(func=cmd_utc_stamps)

    parser_calendar_bodies = subparsers.add_parser(
        "calendar-bodies",
        help="Fetch and store the text of the calendar events stored without it (no model call)",
    )
    parser_calendar_bodies.add_argument(
        "--limit", type=int, default=0, help="At most this many events, newest first (0 = all)"
    )
    parser_calendar_bodies.add_argument(
        "--dry-run", action="store_true", help="Count the events only"
    )
    parser_calendar_bodies.set_defaults(func=cmd_calendar_bodies)

    parser_topic_pages = subparsers.add_parser(
        "topic-pages",
        help="Write the pages of the live topics whose evidence is new (nightly; one model "
        "call a page)",
    )
    parser_topic_pages.add_argument(
        "--build",
        action="store_true",
        help="Every page, new evidence or not (brain maint topic-pages)",
    )
    parser_topic_pages.add_argument(
        "--estimate",
        action="store_true",
        help="Count and price the calls; no model call, nothing written (runs on a replica)",
    )
    parser_topic_pages.add_argument(
        "--topic",
        action="append",
        metavar="NAME",
        help="Only this page; a topic not picked is built as BRAIN_TOPIC_PAGES would "
        "('a|b' covers both phrases). Repeatable",
    )
    parser_topic_pages.add_argument(
        "--deadline-s",
        type=float,
        default=1500.0,
        help="Start no page after this many seconds (default 1500)",
    )
    parser_topic_pages.set_defaults(func=cmd_topic_pages)

    parser_findings = subparsers.add_parser(
        "findings",
        help="The findings sessions filed back, by status, and the rejected ones with why "
        "(for review; read-only)",
    )
    parser_findings.add_argument(
        "--accepted", action="store_true", help="List the accepted ones instead"
    )
    parser_findings.add_argument("--limit", type=int, default=100, help="At most this many")
    parser_findings.set_defaults(func=cmd_findings)

    parser_briefs = subparsers.add_parser(
        "briefs",
        help="Mail the owner a brief before each qualifying meeting 30-45 minutes ahead "
        "(the pilot; every 15 minutes)",
    )
    parser_briefs.add_argument(
        "--dry-run",
        action="store_true",
        help="List what it would brief; no model call, no mail (runs on a replica)",
    )
    parser_briefs.set_defaults(func=cmd_briefs)

    parser_shells = subparsers.add_parser(
        "sharepoint-shells",
        help="Forget the hollow SharePoint documents an old fetch left; queue the links not held",
    )
    shells_mode = parser_shells.add_mutually_exclusive_group()
    shells_mode.add_argument("--dry-run", action="store_true", help="Count only (default)")
    shells_mode.add_argument("--apply", action="store_true", help="Forget them and queue the links")
    parser_shells.set_defaults(func=cmd_sharepoint_shells)

    parser_refetch_garbled = subparsers.add_parser(
        "refetch-garbled",
        help="Fetch again from the mailbox the swept originals of garbled OCR texts",
    )
    parser_refetch_garbled.add_argument(
        "--dry-run", action="store_true", help="Count only: download nothing, write nothing"
    )
    parser_refetch_garbled.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Most messages to ask the mailbox for in this run, both halves together (default 200)",
    )
    parser_refetch_garbled.add_argument(
        "--root", default=str(ATTACHMENTS_DIR), help="Attachments root, where the files go back"
    )
    parser_refetch_garbled.add_argument(
        "--no-lookup",
        action="store_true",
        help="Ask by id only: do not look up the messages with no mailbox id by folder, time"
        " and subject",
    )
    parser_refetch_garbled.set_defaults(func=cmd_refetch_garbled)

    parser_notes = subparsers.add_parser(
        "ingest-session-notes",
        help="Store what Claude sessions wrote to notes (.md, .txt, .csv) as text-only documents",
    )
    parser_notes.add_argument(
        "--all",
        action="store_true",
        help="Every transcript, not only those changed since the last scan",
    )
    parser_notes.set_defaults(func=cmd_ingest_session_notes)

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

    parser_transcribe = subparsers.add_parser(
        "transcribe-images",
        help="Transcribe the text, figures and table cells of content images whose"
        " attachments hold no text, into searchable attachment text",
    )
    parser_transcribe.add_argument(
        "--limit", type=int, default=100, help="Max images (default 100; 0 = all)"
    )
    parser_transcribe.add_argument(
        "--workers", type=int, default=1, help="Concurrent vision workers (default 1)"
    )
    parser_transcribe.add_argument(
        "--deadline-s",
        type=float,
        default=None,
        help="Start no image after this many seconds; the rest wait for the next run",
    )
    parser_transcribe.add_argument(
        "--dry-run", action="store_true", help="Count only, no vision calls"
    )
    parser_transcribe.set_defaults(func=cmd_transcribe_images)

    # Process SharePoint command
    parser_process_sp = subparsers.add_parser(
        "process-sharepoint", help="Scan emails for SharePoint URLs and fetch them"
    )
    parser_process_sp.add_argument(
        "--since",
        type=str,
        help="Rescan emails received on or after YYYY-MM-DD, ignoring and keeping the scan mark",
    )
    parser_process_sp.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max emails to rescan with --since (0 = all, default 0); the scan past the "
        "mark always reads every email",
    )
    # On by default, so any invocation stays inside the nightly unit's timeout,
    # the stale wrapper line included (see the --limit comment in the command).
    parser_process_sp.add_argument(
        "--max-fetches",
        type=int,
        default=100,
        dest="max_fetches",
        help="Max new URLs to fetch this run; the rest wait for the next (0 = no cap, default 100)",
    )
    parser_process_sp.add_argument(
        "--dry-run", action="store_true", help="Scan and count only, no fetching"
    )
    parser_process_sp.add_argument(
        "--deadline-s",
        type=float,
        default=None,
        help="Wall-clock budget in seconds: no fetch starts once it is spent (default: none)",
    )
    parser_process_sp.add_argument(
        "--ingest-fetched",
        action="store_true",
        help="Store the files earlier fetches left on disk, then exit",
    )
    parser_process_sp.add_argument(
        "--refetch-content",
        action="store_true",
        help="Read again, once per target, the links recorded ok with no text stored, then "
        "exit; resumable, bounded by --deadline-s and --max-fetches",
    )
    parser_process_sp.add_argument(
        "--repair-links",
        action="store_true",
        help="Drop the links an email no longer yields and record what it does, offer again "
        "the pages recorded ok without their text, then exit; fetches nothing (--dry-run counts)",
    )
    parser_process_sp.set_defaults(func=cmd_process_sharepoint)

    parser_sp_refused = subparsers.add_parser(
        "sharepoint-refused",
        help="Deprecated: brain tool sharepoint_index, operation list_stale",
    )
    parser_sp_refused.set_defaults(func=cmd_sharepoint_refused)

    # split-html command
    parser_split_html = subparsers.add_parser(
        "split-html",
        help="Keep the HTML of emails loaded before schema v23 beside their text",
    )
    parser_split_html.add_argument(
        "--batch", type=int, default=500, help="Emails per transaction (default 500)"
    )
    parser_split_html.add_argument(
        "--dry-run",
        action="store_true",
        help="Count and measure only, read-only: no migration, no conversion",
    )
    parser_split_html.set_defaults(func=cmd_split_html)

    # reverse-ingest command
    parser_reverse = subparsers.add_parser(
        "reverse-ingest",
        help="Scan the directories you name and ingest the latest version of each logical document",
    )
    parser_reverse.add_argument(
        "--root",
        action="append",
        type=Path,
        help="Directory to scan (repeatable; required, there is no default tree)",
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

    # Any MCP tool by name, through the server's own call (src/mcp_call.py)
    parser_tool = subparsers.add_parser(
        "tool",
        help="Call an MCP tool by name with its arguments as JSON; prints its answer as JSON",
    )
    parser_tool.add_argument("name", nargs="?", help="The tool, as `brain tool --list` names it")
    parser_tool.add_argument(
        "arguments", nargs="?", default=None, help="Its arguments as one JSON object (default {})"
    )
    parser_tool.add_argument(
        "--list",
        action="store_true",
        help="List the tools with their descriptions, input schemas and annotations",
    )
    parser_tool.set_defaults(func=cmd_tool)

    # Query subcommands
    parser_query = subparsers.add_parser(
        "query", help="Deprecated: brain tool (query_emails, search_emails, query_decisions...)"
    )
    query_subparsers = parser_query.add_subparsers(dest="query_type", help="Query type")

    # Common query options
    def add_common_query_args(p):
        p.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
        p.add_argument("-v", "--verbose", action="store_true", help="Ignored: the answer is JSON")

    # Query thread
    parser_query_thread = query_subparsers.add_parser(
        "thread", help="Deprecated: brain tool email_thread"
    )
    parser_query_thread.add_argument("email_id", type=int, help="Email ID")
    add_common_query_args(parser_query_thread)
    parser_query_thread.set_defaults(func=cmd_query_thread)

    # Query by person
    parser_query_person = query_subparsers.add_parser(
        "person", help="Deprecated: brain tool query_emails"
    )
    parser_query_person.add_argument("name", help="Person name or email")
    add_common_query_args(parser_query_person)
    parser_query_person.set_defaults(func=cmd_query_person)

    # Query by topic
    parser_query_topic = query_subparsers.add_parser(
        "topic", help="Deprecated: brain tool query_emails"
    )
    parser_query_topic.add_argument("topic", help="Topic name")
    add_common_query_args(parser_query_topic)
    parser_query_topic.set_defaults(func=cmd_query_topic)

    # Query by keyword
    parser_query_keyword = query_subparsers.add_parser(
        "keyword", help="Deprecated: brain tool search_emails"
    )
    parser_query_keyword.add_argument("keyword", help="Search keyword")
    add_common_query_args(parser_query_keyword)
    parser_query_keyword.set_defaults(func=cmd_query_keyword)

    # Query by date range
    parser_query_date = query_subparsers.add_parser(
        "date", help="Deprecated: brain tool query_emails"
    )
    parser_query_date.add_argument("start", help="Start date (YYYY-MM-DD)")
    parser_query_date.add_argument("end", help="End date (YYYY-MM-DD)")
    add_common_query_args(parser_query_date)
    parser_query_date.set_defaults(func=cmd_query_date)

    # Query decisions
    parser_query_decisions = query_subparsers.add_parser(
        "decisions", help="Deprecated: brain tool query_decisions"
    )
    parser_query_decisions.add_argument("--topic", help="Filter by topic")
    parser_query_decisions.add_argument("--person", help="Filter by person (decided_by)")
    parser_query_decisions.add_argument("--limit", type=int, default=20, help="Max results")
    parser_query_decisions.set_defaults(func=cmd_query_decisions)

    # Query action items
    parser_query_actions = query_subparsers.add_parser(
        "actions", help="Deprecated: brain tool query_actions"
    )
    parser_query_actions.add_argument("--owner", help="Filter by owner")
    parser_query_actions.add_argument(
        "--status", default="open", help="Filter by status (default: open)"
    )
    parser_query_actions.add_argument("--limit", type=int, default=20, help="Max results")
    parser_query_actions.set_defaults(func=cmd_query_actions)

    # Query combined
    parser_query_combined = query_subparsers.add_parser(
        "combined", help="Deprecated: brain tool query_emails"
    )
    parser_query_combined.add_argument("--person", help="Filter by person")
    parser_query_combined.add_argument("--topic", help="Filter by topic")
    parser_query_combined.add_argument("--keyword", help="Full-text search keyword")
    parser_query_combined.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser_query_combined.add_argument("--end", help="End date (YYYY-MM-DD)")
    add_common_query_args(parser_query_combined)
    parser_query_combined.set_defaults(func=cmd_query_combined)

    # Prep command (meeting preparation)
    parser_prep = subparsers.add_parser("prep", help="Deprecated: brain tool meeting_prep")
    parser_prep.add_argument("people", help="Comma-separated list of attendee names or emails")
    parser_prep.add_argument("--topic", help="Meeting topic for focused context")
    parser_prep.add_argument(
        "--days", type=int, default=365, help="Lookback period in days (default: 365)"
    )
    parser_prep.add_argument(
        "--limit", type=int, default=10, help="Ignored: meeting_prep takes no limit"
    )
    parser_prep.add_argument(
        "-v", "--verbose", action="store_true", help="Ignored: the answer is JSON"
    )
    parser_prep.set_defaults(func=cmd_prep)

    # Sync command (incremental)
    parser_sync = subparsers.add_parser(
        "sync", help="Incremental sync over staged mail (extract + load + the steps after)"
    )
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
        help="Accepted for the existing schedules and ignored: sync never exports "
        "mail, it loads what outlook_export staged.",
    )
    parser_sync.add_argument(
        "--lock-wait",
        type=float,
        default=0,
        metavar="SECONDS",
        help="If another sync is running, wait up to this long for it, then skip "
        "with exit 0 (default: 0, skip at once)",
    )
    parser_sync.add_argument(
        "--skip-conversations",
        action="store_true",
        help="Leave out Step 7, the Claude Code conversations (no scheduled unit passes it)",
    )
    parser_sync.add_argument(
        "--full-dedup",
        action="store_true",
        help="Deduplicate the whole people table, not only the groups of the people this "
        "run's load wrote, and do it even when no mail loaded (a whole pass also runs on its "
        "own once a day)",
    )
    parser_sync.set_defaults(func=cmd_sync)

    # whatsapp-sync command
    parser_whatsapp_sync = subparsers.add_parser(
        "whatsapp-sync", help="Ingest the pushed WhatsApp snapshot"
    )
    parser_whatsapp_sync.add_argument(
        "--snapshot", type=str, help="Snapshot path (default: BRAIN_WHATSAPP_SNAPSHOT)"
    )
    parser_whatsapp_sync.add_argument(
        "--limit", type=int, default=0, help="Max sessions to extract this run (0 = no cap)"
    )
    parser_whatsapp_sync.set_defaults(func=cmd_whatsapp_sync)

    # teams-sync command
    parser_teams_sync = subparsers.add_parser(
        "teams-sync", help="Ingest Teams channels plus 1:1, group and meeting chats"
    )
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

    parser_teams_backfill = subparsers.add_parser(
        "teams-backfill-replies",
        help="Store the channel replies stored posts hold (one-off, producer only, no Teams call)",
    )
    parser_teams_backfill.set_defaults(func=cmd_teams_backfill_replies)

    parser_teams_strip = subparsers.add_parser(
        "teams-strip-html",
        help="Convert the stored text of Teams messages still held as HTML "
        "(one-off, producer only, no Teams or model call)",
    )
    parser_teams_strip.add_argument(
        "--dry-run", action="store_true", help="Count only, read-only: nothing written"
    )
    parser_teams_strip.add_argument(
        "--limit", type=int, default=0, help="Change at most this many rows (0 = all)"
    )
    parser_teams_strip.set_defaults(func=cmd_teams_strip_html)

    parser_teams_drop_raw = subparsers.add_parser(
        "teams-drop-raw-json",
        help="Drop the raw Teams payloads nothing needs (once, producer only, after "
        "teams-backfill-replies)",
    )
    parser_teams_drop_raw.add_argument(
        "--dry-run", action="store_true", help="Count what would go; change nothing"
    )
    parser_teams_drop_raw.add_argument(
        "--limit", type=int, default=0, help="At most this many payloads, oldest first (0 = all)"
    )
    parser_teams_drop_raw.set_defaults(func=cmd_teams_drop_raw_json)

    parser_teams_search = subparsers.add_parser(
        "teams-search", help="Deprecated: brain tool search_teams"
    )
    parser_teams_search.add_argument("query", help="Search query")
    parser_teams_search.add_argument(
        "--kind", choices=["thread", "message", "both"], default="both"
    )
    parser_teams_search.add_argument("--limit", type=int, default=20)
    parser_teams_search.set_defaults(func=cmd_teams_search)

    parser_teams_thread = subparsers.add_parser(
        "teams-thread", help="Deprecated: brain tool teams_thread_context"
    )
    parser_teams_thread.add_argument("thread_id", type=int)
    parser_teams_thread.set_defaults(func=cmd_teams_thread)

    parser_teams_chat = subparsers.add_parser(
        "teams-chat", help="Deprecated: brain tool teams_chat_summary"
    )
    parser_teams_chat.add_argument("chat_id", type=int)
    parser_teams_chat.add_argument("--days", type=int, default=30)
    parser_teams_chat.set_defaults(func=cmd_teams_chat)

    parser_teams_stats = subparsers.add_parser(
        "teams-stats", help="Deprecated: brain tool sql_query (README)"
    )
    parser_teams_stats.set_defaults(func=cmd_teams_stats)

    # calendar-sync command
    parser_cal = subparsers.add_parser("calendar-sync", help="Sync calendar events from Outlook")
    parser_cal.add_argument(
        "--backfill",
        action="store_true",
        help="List a month at a time, resuming where the last backfill stopped: the months "
        "stats coverage flags as not held in full, or the last 12 when it flags none "
        "(--since/--until set the window)",
    )
    parser_cal.add_argument(
        "--dry-run",
        action="store_true",
        help="List the window and count what would be fetched; fetch, extract and write nothing",
    )
    parser_cal.add_argument(
        "--since", type=str, help="Start date ISO (e.g. 2025-05-14; default: 7 days back)"
    )
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

    parser_classify = subparsers.add_parser(
        "classify-sources",
        help="Set every email's source_class again by today's rules "
        "(after BRAIN_USER_EMAIL_PATTERN is set or changed)",
    )
    parser_classify.set_defaults(func=cmd_classify_sources)

    # Import people command
    parser_import = subparsers.add_parser("import-people", help="Import canonical people from JSON")
    parser_import.add_argument(
        "--file",
        help="Path to canonical_people.json (default: data/canonical_people.json)",
    )
    parser_import.set_defaults(func=cmd_import_people)

    # Stale command
    parser_stale = subparsers.add_parser("stale", help="Deprecated: brain tool stale_threads")
    parser_stale.add_argument(
        "--days", type=int, default=5, help="Stale threshold in days (default: 5)"
    )
    parser_stale.add_argument(
        "--max-days",
        type=int,
        default=30,
        help="Oldest thread still worth a reminder, in days (default: 30)",
    )
    parser_stale.add_argument(
        "--limit", type=int, default=20, help="Max threads to list (default: 20)"
    )
    parser_stale.set_defaults(func=cmd_stale)

    # Embed command (build embedding index)
    parser_embed = subparsers.add_parser("embed", help="Build embedding index for semantic search")
    parser_embed.add_argument(
        "--force", action="store_true", help="Rebuild all embeddings from scratch"
    )
    parser_embed.set_defaults(func=cmd_embed)

    # Query semantic
    parser_query_semantic = query_subparsers.add_parser(
        "semantic", help="Deprecated: brain tool search_emails, search_type semantic"
    )
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
    parser_stats = subparsers.add_parser("stats", help="Deprecated: brain tool stats")
    parser_stats.set_defaults(func=cmd_stats)

    # One-off maintenance (MAINT_COMMANDS), added once every other parser exists: each
    # alias copies the parser of the command it runs (argparse parents), so it takes the
    # same arguments, the ones added to that command later included.
    parser_maint = subparsers.add_parser(
        "maint", help="One-off maintenance commands, run by hand: brain maint --help"
    )
    maint_subparsers = parser_maint.add_subparsers(
        dest="maint_command", metavar="COMMAND", help="Run by hand, once or now and then"
    )
    for name, (command, implied, help_text) in MAINT_COMMANDS.items():
        alias = maint_subparsers.add_parser(
            name,
            parents=[subparsers.choices[command]],
            add_help=False,
            help=help_text,
            description=help_text,
        )
        if implied:
            # Not set_defaults(**implied): the alias shares its command's argument
            # objects, and a default set here would become the job's own, so every
            # plain calendar-sync would backfill. main() applies these after parsing.
            alias.set_defaults(_maint_implies=implied)
    # The script parses what follows. prefix_chars="+" leaves every '-' word to it.
    parser_scrub = maint_subparsers.add_parser(
        "scrub",
        add_help=False,
        prefix_chars="+",
        help="Remove credentials stored before redaction (scripts/scrub_secrets.py; "
        "brain maint scrub --help for its options)",
    )
    parser_scrub.add_argument("script_args", nargs="*", help="The script's own arguments")
    parser_scrub.set_defaults(func=cmd_scrub)
    # forget-itemless, mark-boilerplate, reextract-stacked (src/store/item_cleanup.py):
    # maint-only, with no old spelling.
    from src.store.item_cleanup import add_maint_parsers

    add_maint_parsers(maint_subparsers)
    # New with the numbers registry (schema v37): no old spelling to keep.
    parser_figures = maint_subparsers.add_parser(
        "figures-backfill",
        help="Read the figures of every report edition held into the numbers registry "
        "(no model call; a dry run unless --apply)",
    )
    figures_mode = parser_figures.add_mutually_exclusive_group()
    figures_mode.add_argument("--dry-run", action="store_true", help="Count only (default)")
    figures_mode.add_argument(
        "--apply", action="store_true", help="Read them, committing each edition; resumable"
    )
    parser_figures.add_argument(
        "--estimate",
        action="store_true",
        help="With the dry run, parse a sample of editions to project figures and time",
    )
    parser_figures.add_argument(
        "--limit", type=int, default=None, help="Read at most this many editions in this run"
    )
    parser_figures.add_argument(
        "--reset",
        action="store_true",
        help="With --apply, forget every figure first and read them all again (a parser change)",
    )
    parser_figures.set_defaults(func=cmd_figures_backfill)
    # consolidate (src/store/consolidate.py), entities (src/store/entities.py) and
    # topic-canon (src/store/topic_canon.py): maint-only, with no old spelling.
    from src.store import consolidate, entities, topic_canon

    consolidate.add_maint_parser(maint_subparsers)
    entities.add_maint_parser(maint_subparsers)
    topic_canon.add_maint_parser(maint_subparsers)

    # Parse and execute
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "query" and not args.query_type:
        parser_query.print_help()
        sys.exit(1)

    if args.command == "maint" and not args.maint_command:
        parser_maint.print_help()
        sys.exit(1)

    for key, value in getattr(args, "_maint_implies", {}).items():
        setattr(args, key, value)
    command = args.maint_command if args.command == "maint" else args.command
    maint_name = _maint_name(args)
    if maint_name:
        old, implied, _ = MAINT_COMMANDS[maint_name]
        flags = [f"--{key.replace('_', '-')}" for key in implied]
        print(
            f"brain {' '.join([old, *flags])} is deprecated (removed in the next release), "
            "with the same arguments"
            + (f" but {' '.join(flags)}" if flags else "")
            + f": use brain maint {maint_name}",
            file=sys.stderr,
        )

    from src import config

    if command not in READ_ONLY_COMMANDS and config.is_replica():
        print(config.replica_refusal(f"'{command}'"), file=sys.stderr)
        sys.exit(2)

    # Execute command
    try:
        # Before any command runs, so the shared policy's budget test reflects this
        # unit's real TimeoutStartSec instead of resolve_deadline's flat 900s default.
        # A no-op off systemd, and inside the try on purpose: a unit whose timeout
        # cannot fund one worst-case LLM call raises here, and exiting 1 with the
        # reason on stderr is the loud refusal that beats a SIGTERM later.
        install_llm_deadline_for_this_process()
        rc = args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # A command reports failure by returning a non-zero int. This used to be
    # discarded, so calendar-sync counted failed chunks, bodies and extractions
    # and still exited 0, and its dead-man switch pinged green.
    if isinstance(rc, int) and not isinstance(rc, bool) and rc != 0:
        sys.exit(rc)


if __name__ == "__main__":
    main()
