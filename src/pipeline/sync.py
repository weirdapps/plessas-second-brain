"""`brain sync`: incremental sync over staged mail, its stages and their budgets.

Moved from src/cli.py on 2026-10-11 as it was: the CLI parses the arguments and calls
run(args) with the argparse namespace.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from src.config import ATTACHMENTS_DIR, DATA_ROOT, EXTRACT_ENGINE, IMAGE_CLASSIFY_BUDGET_S
from src.store.stamps import utc_now

# Attachments registered per sync run. Step 6 runs Phase 1 (local parse) and
# Phase 2 (one LLM call each) over exactly this set, inside a unit with a
# TimeoutStartSec — so this is really a bound on that downstream work. At 200/run
# the 7,387-file backlog left by the VPS cutover drains in under two days
# without any pass risking a SIGTERM.
ATTACHMENT_REGISTER_LIMIT = 50

# The hourly sync only registers mail from this window. It runs under
# TimeoutStartSec=600 and is sized for one hour of new email; carrying the
# backfill as well is what made every scheduled run on 2026-08-18 overrun and
# get SIGTERMed, with only the 90 s retry completing. The window has to clear
# the overnight gap in the timer's schedule (22:00 -> 01:00 -> 07:00, so nine
# hours) with room to spare. Bulk registration lives in `register-attachments`,
# which the nightly job runs under a one-hour budget.
ATTACHMENT_REGISTER_WINDOW_H = 48

# --- Wall-clock budget for one sync run ------------------------------------
#
# sb-outlook-sync runs under TimeoutStartSec=600. Bounding stages one at a time
# against that number failed three times over (registration, then Phase 2 by
# count, then Phase 1), because independent per-stage limits cannot sum to a
# guarantee — each fix stopped one overrun and exposed the next. These budgets
# are therefore stated together and their sum is asserted against the timeout
# in tests/test_sync_budget.py.
#
# Every one is wall-clock, not a count. Count was wrong for both slow stages
# and for opposite reasons: Phase 1 varies 1000x per item (a text part vs OCR
# vs a 30 MB workbook), and Phase 2 is network-bound, so 25 calls read as
# modest and cost 6-8 minutes. Anything deferred stays queued for the nightly
# job, which has an hour.
#
# The unit is 20 minutes since 2026-09-11 (catchup-timeout.conf: a recovery run
# needs more than the steady state). This is the same number as
# llm_deadline._UNIT_TIMEOUT_SECONDS, and a test holds the two together.
SYNC_UNIT_TIMEOUT_S = 1200.0

# The wrapper runs auth-watch, three outlook_export folder passes and
# inbox_reconcile inside the same unit before it calls `sync`, so their time is
# spent from the same timeout. p99 of 133 runs, 2026-09-16 to 09-23 (median 47 s,
# max 364 s), measured from the Inbox export to the start of `sync`: auth-watch
# runs before the first timestamp the wrapper logs and is not in the figure.
# _extract_deadline_s caps Step 2 by what is actually left of the unit, so a
# slow auth-watch comes out of the extraction slice rather than the timeout.
SYNC_WRAPPER_FETCH_OBSERVED_S = 140.0

PHASE1_SYNC_DEADLINE_S = 90.0
PHASE2_SYNC_DEADLINE_S = 90.0

# Set to 1, Step 6 reads images and zip archives in the hourly slice, as it did before
# 2026-10-11. Unset, it leaves them to the nightly pass while one runs: see Step 6.
SYNC_HEAVY_ATTACHMENTS_ENV = "BRAIN_SYNC_HEAVY_ATTACHMENTS"

# Step 8's default (IMAGE_CLASSIFY_BUDGET_S, 480 s) was sized for sb-daily-sync,
# whose timeout is 30 minutes. Inherited here it claims 80% of the unit on its
# own, which is why runs still died after Step 6 was bounded.
# Trimmed to 90 s so the stage sum keeps 20% headroom (tests/test_sync_budget.py).
# Cheapest place to take it from: the nightly job already runs
# `process-images --limit 500` under an hour, so a short hourly slice only has
# to keep fresh mail's images moving.
IMAGE_CLASSIFY_SYNC_BUDGET_S = 90.0

# Step 7 (Claude Code conversation sync) was the last stage with no bound of any
# kind: cmd_sync called run_conversation_extraction() with no arguments, so the
# default limit=0 extracted the whole pending list one conversation at a time.
# The sum in tests/test_sync_budget.py appeared to cover it, but only because
# that file asserted a 30 s allowance for a stage nothing enforced, so the
# guarantee was arithmetic over a number production was free to ignore.
# What made it bite: pending is whatever sync-tokens-to-vps.sh has rsynced since
# the last run, so it scales with how busy Claude Code has been, not with the
# mailbox. On 2026-09-05 that was 32 conversations an hour, and every run from
# 23:28 the previous evening was SIGTERMed seconds after printing "Sync
# complete". 30 s holds the previously assumed sum exactly, so the margin tests
# keep the same headroom they were written against; the remainder drains on
# sb-conversation-sync, which has 900 s.
CONVERSATION_SYNC_DEADLINE_S = 30.0

# Step 8b (consolidation, src/store/consolidate.py): the rules on the threads that received
# items since the last run, then model calls within BRAIN_CONSOLIDATE_SYNC_TOKENS. About 50
# threads change a day; a call took 3-10 s on the replica's sample. What is not reached
# waits in model_state 'pending' for the next run.
CONSOLIDATE_SYNC_DEADLINE_S = 60.0

# Step 2 (email extraction) had no bound either, and could sleep an hour
# in-process on five quota errors. Measured on 2026-09-23: 7-19 new emails an
# hour take 1.5-2.5 minutes through Steps 2-4, most of it extraction. The rest
# of a backlog stays pending for the next run.
EXTRACT_SYNC_DEADLINE_S = 300.0

# Step 2's budget in each unit that runs `sync`. The two 30-minute units are the
# ones that drain a backlog (the health check kicks sb-daily-sync to drain
# staging), so they get most of their larger budget rather than the hourly
# slice. Found by /proc/self/cgroup, as llm_deadline finds the unit: sb-noon-
# catchup and sb-outlook-sync run identical argv.
EXTRACT_DEADLINE_BY_UNIT_S = {
    "sb-outlook-sync": EXTRACT_SYNC_DEADLINE_S,
    "sb-daily-sync": 900.0,
    "sb-noon-catchup": 900.0,
}

# Replies the model could not use (refusals, unparseable JSON), with none
# extracted, from which a sync reads as the model being down. One or two are an
# ordinary quiet run: about 14 emails a day were refused in September 2026, and a
# run whose one fresh email was refused went red at 75. Three and none usable is
# no longer ordinary.
MODEL_UNUSABLE_ALARM = 3

# Step 9 cuts the long documents the run stored into chunks and embeds them (src/store/
# chunks.py), wall-clock like the other slow stages: a 100-page PDF is about 50 chunks, one
# embedding call. What does not fit waits for the next run. Only an index in a space with
# chunks (template 2, after `brain maint vectors-rebuild`) has any to embed.
CHUNK_EMBED_SYNC_DEADLINE_S = 60.0

# Observed span for the stages that have no budget of their own: load,
# registration (count-bounded), people dedup, and the incremental embeddings
# update (about 10 s), made once at the end of the run (Step 9), which rewrites
# the whole index. Mail fetch is SYNC_WRAPPER_FETCH_OBSERVED_S above.
SYNC_FIXED_WORK_S = 80.0

# What the stages after Step 2 need, held back from the unit's remaining time.
SYNC_AFTER_EXTRACTION_S = (
    SYNC_FIXED_WORK_S
    + PHASE1_SYNC_DEADLINE_S
    + PHASE2_SYNC_DEADLINE_S
    + CONVERSATION_SYNC_DEADLINE_S
    + IMAGE_CLASSIFY_SYNC_BUDGET_S
    + CHUNK_EMBED_SYNC_DEADLINE_S
    + CONSOLIDATE_SYNC_DEADLINE_S
)


def _extract_deadline_s() -> float | None:
    """Step 2's wall-clock budget for this process.

    Its unit's slice when the unit is in the table; the hourly slice for any
    other scheduler (cron, launchd or a unit off the table, all three described
    in the docs); unbounded only for a person at a terminal. Then capped by what
    is left of the unit, read from the unit-anchored PTS_LLM_DEADLINE, less what
    the later stages need: a second sync in the same unit (the wrappers retry
    once on a database lock) used to get a fresh full slice and overrun it.
    """
    import os
    import time

    from src.llm_deadline import _detect_systemd_unit

    unit = _detect_systemd_unit()
    if unit in EXTRACT_DEADLINE_BY_UNIT_S:
        slice_s = EXTRACT_DEADLINE_BY_UNIT_S[unit]
    elif sys.stdin is not None and sys.stdin.isatty():
        return None
    else:
        slice_s = EXTRACT_SYNC_DEADLINE_S
    try:
        left = float(os.environ["PTS_LLM_DEADLINE"]) - time.time() - SYNC_AFTER_EXTRACTION_S
    except (KeyError, ValueError):
        return slice_s
    return max(0.0, min(slice_s, left))


def _holding_the_sync_lock(sync):
    """Run `sync` holding DATA_ROOT/state/sync.lock, or skip if another sync has it.

    Three units run `src.cli sync` (sb-outlook-sync hourly, sb-noon-catchup and
    sb-daily-sync), and only the hourly wrapper looked for another, with a pgrep,
    once, one way and not atomically. On 2026-09-24 the noon catch-up started
    during the hourly load, and both extracted the same conversations and
    rewrote the state file each from its own snapshot, where the last writer
    drops the other's processed_ids and give-up counters.

    `--lock-wait` seconds (0 by default) is how long to wait for the holder.
    Then the run skips with exit 0: the sync holding the lock is draining the
    same staged mail. The lock is an flock, so it goes with the process holding
    it, and a killed sync leaves nothing to clear.
    """
    import fcntl
    import functools
    import time

    @functools.wraps(sync)
    def locked(args):
        wait_s = float(getattr(args, "lock_wait", 0) or 0)
        path = DATA_ROOT / "state" / "sync.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as lock:
            give_up_at = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    left = give_up_at - time.monotonic()
                    if left <= 0:
                        print(f"skip: another sync is running (waited {wait_s:.0f} s for {path})")
                        return 0
                    time.sleep(min(1.0, left))
            return sync(args)

    return locked


@_holding_the_sync_lock
def run(args):
    """Incremental sync over staged mail: extract, load, and the steps after."""
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
            print(
                "Empty database. Stage mail, then run `python -m src.extract.local` "
                "and `python -m src.cli load` first."
            )
            conn.close()
            return

    # Where the mail this run loads starts, so Step 3b finds the directories of what it loads.
    last_email_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM emails").fetchone()[0]
    conn.close()

    # Step 1: sync stages no mail. `python -m src.export.outlook_export` does,
    # through outlook-cli, on every host. On a Mac this step used to run the
    # Apple Mail exporter, which force-quit Mail.app; it was removed.
    print("\nStep 1: mail is staged by outlook_export (outlook-cli); using the staged batches.")

    # Step 2: Extract (local mode)
    engine = getattr(args, "engine", None) or EXTRACT_ENGINE
    print(f"\nStep 2: Extracting new emails (engine: {engine})...")
    from src.extract.local import run_extraction, stop_requested

    extraction_run = run_extraction(
        limit=args.limit or 0,
        engine=engine,
        workers=args.workers or 1,
        deadline_s=_extract_deadline_s(),
        db_path=db_path,
    )
    # A stop signal ends extraction at a checkpoint. The steps after it used to
    # carry on regardless, until systemd's SIGKILL 90 s later; now they do not
    # start. 143 is 128 + SIGTERM, which is what systemd sends.
    if stop_requested():
        print("Stop requested during extraction; the later steps did not run", file=sys.stderr)
        return 143
    # Every email that went to the model failed there: a retired model id, a 400
    # on every request, a refusal of everything. Emails that had already failed
    # in an earlier run are left out of the count (see run_extraction), so a run
    # that met only those is not taken for a dead model. The rest of the sync
    # still runs, but the run must not read as fresh or green, as it did with
    # rc 0 and a new last_sync_date while no mail loaded. Not keyed on
    # `extracted`, which news (extracted without the model) keeps above zero.
    # A reply the model could not use (a refusal, unparseable JSON) is an answer,
    # not an outage: a quiet run whose one fresh email was refused read as a dead
    # model. Several answers and none of them usable still reads as down.
    model_down = (
        isinstance(extraction_run, dict)
        and not extraction_run.get("model_successes", 0)
        and (
            extraction_run.get("model_failures", 0) > 0
            or extraction_run.get("model_unusable", 0) >= MODEL_UNUSABLE_ALARM
        )
    )

    # Step 3: Load into DB
    print("\nStep 3: Loading into database...")
    from src.store.loader import load_extractions
    from src.store.schema import get_connection as get_conn

    extracted_dir = DATA_ROOT / "extracted"
    staging_dir = DATA_ROOT / "staging"
    # The people rows this load adds or changes, which Step 4 takes.
    touched_people: set[int] = set()
    count = load_extractions(
        db_path, str(extracted_dir), str(staging_dir), touched_people=touched_people
    )
    print(f"Loaded {count} new emails")

    # Step 3b: record the attachments outlook-cli downloaded with the mail.
    if count > 0:
        # outlook-cli fetched the BINARIES hourly; it never recorded them. Every
        # downstream stage reads the attachments table, not the disk, so without
        # this the files are invisible — 7,387 of them accumulated unseen between
        # the 2026-06-30 VPS cutover and 2026-08-17. Bounded per run because
        # Step 6 below runs Phase 1/2 unbounded over whatever becomes visible.
        print("\nStep 3b: Registering attachments outlook-cli downloaded...")
        from datetime import UTC, timedelta

        from src.export.outlook_attachments import register_new_downloads

        window_start = (
            datetime.now(UTC) - timedelta(hours=ATTACHMENT_REGISTER_WINDOW_H)
        ).isoformat()
        conn_reg = get_conn(db_path)
        try:
            # The directories written since the last complete scan, and those of the mail
            # this run loaded, not every directory: the nightly pass walks them all.
            loaded_mail = conn_reg.execute(
                "SELECT message_id FROM emails WHERE id > ?", (last_email_id,)
            ).fetchall()
            reg = register_new_downloads(
                conn_reg,
                ATTACHMENTS_DIR,
                [str(row[0]) for row in loaded_mail],
                limit=ATTACHMENT_REGISTER_LIMIT,
                since=window_start,
            )
        finally:
            conn_reg.close()
        print(
            f"Attachments: {reg['registered']} registered from {reg['scanned']} message dirs"
            f" ({reg['deferred']} awaiting their email)"
        )

    # Step 4: Deduplicate people: the groups of the rows this load added or changed, and of
    # any added since the last pass (src/store/dedup_people.py run_dedup), not every person.
    # --full-dedup takes the whole table, and runs whether or not mail loaded.
    full_dedup = bool(getattr(args, "full_dedup", False))
    if count > 0 or full_dedup:
        print("\nStep 4: Deduplicating people" + (" (whole table)" if full_dedup else "") + "...")
        from src.store.dedup_people import run_dedup

        dedup_result = run_dedup(db_path=db_path, touched=None if full_dedup else touched_people)
        reduced = dedup_result.get("total_reduced", 0)
        print(f"Dedup: {reduced} duplicates merged")
    else:
        print("\nNo new emails — skipping dedup")

    # Step 5, the embeddings, waits for the end of the run: Step 9.

    # Step 6: Process new attachment content
    from src.extract.attachment_pipeline import (
        LONG_TEXT_CHARS,
        run_phase1,
        run_phase2,
        text_pass_is_recent,
    )

    # Ensure attachment_content table exists
    from src.store.schema import run_migrations as run_mig

    conn_mig = get_conn(db_path)
    run_mig(conn_mig)
    # Images and zip archives wait for the nightly pass while one runs: one of them can take
    # minutes (OCR_MAX_SECONDS, ZIP_MAX_SECONDS) against this 90 s slice, and they took the
    # slice from the day's documents. Where no pass reads them, as on a host that runs `sync`
    # alone, they are read here. BRAIN_SYNC_HEAVY_ATTACHMENTS=1 reads them here regardless.
    read_heavy = os.environ.get(SYNC_HEAVY_ATTACHMENTS_ENV, "").strip() == "1"
    defer_heavy = not read_heavy and text_pass_is_recent(conn_mig)
    conn_mig.close()

    print("\nStep 6: Processing new attachment content...")
    # Phase 1 runs UNSCOPED so the deadline can converge: anything deferred for time has no
    # content row, and `ac.id IS NULL` offers it again next run. Newest mail first: in
    # registration order the day's documents waited behind the backlog for the 02:00 pass, and
    # 15-63% of a day's attachments got their text more than six hours after download
    # (2026-10-05 to 10-09). The nightly pass keeps registration order.
    p1_stats = run_phase1(
        db_path,
        deadline_s=PHASE1_SYNC_DEADLINE_S,
        newest_first=True,
        defer_heavy=defer_heavy,
    )
    deferred = p1_stats.get("deferred", 0)
    heavy = p1_stats.get("heavy", 0)
    if p1_stats.get("processed", 0) or deferred or heavy:
        encrypted = p1_stats.get("encrypted", 0)
        print(
            f"  Phase 1: {p1_stats.get('extracted', 0)} text extracted, "
            f"{p1_stats.get('failed', 0)} failed, {p1_stats.get('skipped', 0)} skipped"
            + (f", {encrypted} encrypted" if encrypted else "")
            + (f", {deferred} deferred (out of time)" if deferred else "")
            + (f", {heavy} images and archives left for the nightly pass" if heavy else "")
        )
    # What Phase 1 has just read first, then any text an earlier run left awaiting its summary,
    # newest first, in Phase 2's own slice. It was scoped to this run's registrations, which
    # Phase 1 had seldom reached, and ran only when Phase 1 had read something: summaries came
    # 4-19 hours late on average (audit 2026-10-11).
    p2_stats = run_phase2(
        db_path,
        first_ids=p1_stats.get("ids"),
        newest_first=True,
        deadline_s=PHASE2_SYNC_DEADLINE_S,
        max_text_chars=LONG_TEXT_CHARS,
    )
    p2_deferred = p2_stats.get("deferred", 0)
    if p2_stats.get("processed", 0) or p2_deferred:
        print(
            f"  Phase 2: {p2_stats.get('extracted', 0)} LLM extracted,"
            f" {p2_stats.get('failed', 0)} failed"
            + (f", {p2_deferred} deferred (out of time)" if p2_deferred else "")
        )
    elif not (p1_stats.get("processed", 0) or deferred or heavy):
        print("  No new attachments to process")

    # Step 7: Sync conversations from Claude Code sessions. --skip-conversations leaves it
    # out; no scheduled unit passes it.
    print("\nStep 7: Syncing Claude Code conversations...")
    conv_loaded = 0
    if getattr(args, "skip_conversations", False):
        print("  Skipped (--skip-conversations)")
    else:
        from src.export.conversation_export import export_conversations
        from src.store.loader import load_conversations as load_convs

        conn_conv = get_conn(db_path)
        run_mig(conn_conv)
        loaded = {
            r["session_id"]: (r["ended_at"], r["turn_count"])
            for r in conn_conv.execute("SELECT session_id, ended_at, turn_count FROM conversations")
        }
        conn_conv.close()

        conv_export = export_conversations(days=7, loaded=loaded)
        # Staged before and not loaded yet: the export no longer stages those again, and
        # one sb-conversation-sync extracted waits for a load here.
        waiting = conv_export.get("pending", 0)
        if conv_export["exported"] > 0 or waiting > 0:
            print(f"  Exported {conv_export['exported']} new or grown conversations")
            if waiting:
                print(f"  {waiting} staged before and not loaded yet")
            from src.extract.local import run_conversation_extraction

            run_conversation_extraction(deadline_s=CONVERSATION_SYNC_DEADLINE_S)
            if stop_requested():  # as after Step 2
                print(
                    "Stop requested during conversation extraction; the later steps did not run",
                    file=sys.stderr,
                )
                return 143
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
        #
        # IMAGE_CLASSIFY_BUDGET_S (480 s) was sized against those 30-minute units.
        # The hourly sync only has 600 s in total, so inheriting it let Step 8
        # claim 80% of the run on its own — take the smaller of the two.
        img_stats = run_backfill(
            conn=img_conn,
            since=None,
            limit=200,
            run_vision=True,
            dry_run=False,
            workers=4,
            unprocessed_only=True,
            deadline_s=min(IMAGE_CLASSIFY_BUDGET_S, IMAGE_CLASSIFY_SYNC_BUDGET_S),
        )
        img_conn.close()
        classified = img_stats.get("classified", 0)
        missing = img_stats.get("missing", 0)
        deferred = img_stats.get("deferred", 0)
        failed = img_stats.get("failed", 0)
        # `missing` = file gone from disk; `deferred` = budget spent, work requeued.
        # The old line printed `missing` under the label "remaining", which read as
        # "backlog empty: 0" every day while the queue was 200 deep. Queue depth is
        # reported by health_check.check_images (WARN past IMAGE_QUEUE_WARN); this
        # line just says what THIS run did. `failed` too: without it a run in which
        # every image failed read "No unclassified images".
        if classified > 0 or missing > 0 or deferred > 0 or failed > 0:
            print(
                f"  Classified: {classified}, failed: {failed}, deferred: {deferred}, "
                f"missing files: {missing}"
            )
        else:
            print("  No unclassified images")
    except Exception as e:
        print(f"  Image classification skipped: {e}")

    # Step 8b: consolidation (src/store/consolidate.py): the threads with new items from any
    # source, this run's mail and the Teams and WhatsApp syncs' since the last run. Marks
    # items duplicate, superseded or done; deletes nothing. A failure here fails no sync.
    print("\nStep 8b: Consolidating the threads with new items...")
    try:
        from src.store.consolidate import run_in_sync

        consolidation = run_in_sync(db_path, CONSOLIDATE_SYNC_DEADLINE_S)
        if consolidation.started_from_scratch:
            print("  First run: recorded where consolidation starts (backfill: maint consolidate)")
        else:
            print(
                f"  {consolidation.threads} threads by rule ({consolidation.rule_labels} duplicates),"
                f" {consolidation.calls} calls, {consolidation.model_labels} labels"
                f" {dict(consolidation.by_label)}, {consolidation.review} held for review"
                + (f"; stopped: {consolidation.stopped}" if consolidation.stopped else "")
            )
    except Exception as e:
        print(f"  Consolidation skipped: {e}", file=sys.stderr)

    # Step 9: one embedding pass for everything this run stored: the mail of Step 3, the
    # attachment summaries of Step 6, the conversations of Step 7 (one loaded again has a new
    # id, which semantic search cannot find until it has a vector) and the image descriptions
    # of Step 8. Step 5 embedded after Step 4, and Step 7 again when it loaded conversations,
    # and each pass loads and rewrites the whole index (embeddings.npz, 1.7 GB on the
    # producer): an hourly run saved it twice to load one email, at its 6 GB MemoryMax. Taken
    # when mail or conversations loaded, as those two were; a run stopped before it leaves
    # its vectors to the next run's pass. The new long documents' chunks go in the same save,
    # within CHUNK_EMBED_SYNC_DEADLINE_S; while a vector rebuild runs the pass adds nothing.
    if count > 0 or conv_loaded:
        print("\nStep 9: Updating embeddings...")
        from src.store.embeddings import build_index

        conn_embed = get_conn(db_path)
        try:
            build_index(conn_embed, chunk_deadline_s=CHUNK_EMBED_SYNC_DEADLINE_S)
            print("Embeddings updated")
        except Exception as e:
            # A run that loaded mail fails here, as Step 5 did. One that loaded only
            # conversations goes on, as Step 7 did, and the next pass embeds them.
            if count > 0:
                raise
            print(f"  Embedding the conversations failed: {e}", file=sys.stderr)
        finally:
            conn_embed.close()

    # Update sync metadata
    conn = get_conn(db_path)
    migrate_add_sync_metadata(conn)
    now = utc_now()  # UTC with its offset since 2026-10-16 (src/store/stamps.py)
    if not model_down:
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('last_sync_date', ?)",
            (now,),
        )
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('last_sync_count', ?)",
        (str(count),),
    )
    # When mail last arrived, beside when this command last ran. last_sync_date
    # moves on every run, fetched mail or not, so alone it read fresh through an
    # Outlook outage. The Inbox export stamps last_sync_completed_at only when it
    # succeeds, empty runs included. Copied here because brain.db is the only
    # file a replica receives; get_freshness reports the older of the two.
    from src.export.state import load_outlook_sync_state

    try:
        export_ok_at = load_outlook_sync_state(
            DATA_ROOT / "state" / "outlook_sync.json"
        ).last_sync_completed_at
    except (OSError, ValueError, AttributeError, TypeError):  # unreadable: no stamp
        export_ok_at = None
    if export_ok_at:
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('mail_export_ok_at', ?)",
            (export_ok_at,),
        )
    conn.commit()
    conn.close()

    if model_down:
        print(f"\nSync complete. {count} emails added. Sync timestamp left at {last_sync}.")
    else:
        print(f"\nSync complete. {count} emails added. Sync timestamp: {now}")

    # Everything else ran and the rest of the mail stays pending, but extraction
    # did not finish. Before the deadline existed the same pause slept past the
    # unit timeout and read red. 75 is EX_TEMPFAIL. sb-daily-sync and
    # sb-noon-catchup pass it through; sb-outlook-sync logs the sync's code and
    # keeps it out of its own status by design, with those two units behind it.
    if isinstance(extraction_run, dict) and extraction_run.get("quota_paused"):
        print("Extraction ended on a quota pause; the rest stays pending", file=sys.stderr)
        return 75
    # The same code: the run finished and failed, and a restart would repeat it
    # (RestartPreventExitStatus=75 on both backlog units).
    if model_down:
        print(
            f"The model extracted none of the new emails sent to it "
            f"({extraction_run.get('model_failures', 0)} failed, "
            f"{extraction_run.get('model_unusable', 0)} unusable replies); "
            "last_sync_date was not advanced",
            file=sys.stderr,
        )
        return 75
    return 0
