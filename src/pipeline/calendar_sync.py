"""`brain calendar-sync`: list the Outlook calendar, then fetch, extract and load what changed.

Moved from src/cli.py on 2026-10-11 as it was: the CLI parses the arguments and calls
run(args) with the argparse namespace.
"""

import sys
from datetime import datetime
from pathlib import Path

from src.config import DATA_ROOT
from src.pipeline import EXIT_REAUTH

# Runs in a row an event may fail get-event and still fail the calendar-sync run.
# It is retried and logged on every run after that too, but one event get-event
# can never return would otherwise keep the unit red for as long as it sits in
# the window, and a check that is always red is a check nobody reads.
CALENDAR_FETCH_ALERT_RUNS = 3

# How far back a plain run lists.
LOOKBACK_DAYS = 7

# An event changed after it left the plain run's week (a series edited, notes added to a
# meeting gone by) kept its old etag and content for good: 15 of the 215 events of the 14
# days around 2026-10-20 did. Once every DEEP_REFRESH_HOURS a plain run also lists the
# DEEP_LOOKBACK_DAYS before that week, and fetches at most DEEP_FETCH_CAP changed events
# there; a run the cap cut short leaves the rest to the next one. The time of the last
# complete pass is kept in sync_metadata under DEEP_REFRESH_KEY.
DEEP_REFRESH_KEY = "calendar_deep_listed"
DEEP_REFRESH_HOURS = 24
DEEP_LOOKBACK_DAYS = 30
DEEP_FETCH_CAP = 100


def _read_json_dict(path: Path) -> dict:
    import json

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _utc_bound(moment: datetime) -> str:
    """A window bound as start_at compares with it: UTC, to the second. A naive
    bound is local time, as outlook-cli reads it."""
    from datetime import UTC

    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _whole_month(start: datetime, end: datetime, now: datetime) -> str | None:
    """YYYY-MM when [start, end) is the whole of a month already over, else None."""
    from datetime import timedelta

    first = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (first + timedelta(days=32)).replace(day=1)
    if start == first and end == following and end <= now:
        return f"{start:%Y-%m}"
    return None


def _calendar_windows(conn, args, now: datetime) -> tuple[list[tuple[datetime, datetime]], str]:
    """The windows calendar-sync lists, oldest first, and the line that says so.

    A plain run lists one window, by default the week behind and the 30 days ahead.
    A backfill lists a month at a time: with neither --since nor --until, the months
    stats coverage flags as not held in full, or the last 12 when it flags none. It
    used to list the 365 days before it ran, which on 2026-09-27 began after the
    months the first load had capped. A whole month an earlier backfill recorded is
    skipped, unless one of its events still owes an extraction: only a listing
    offers it again.
    """
    import sqlite3
    from datetime import timedelta

    from src.export.calendar_export import chunk_date_range
    from src.store.calendar_coverage import density, listed_months

    # --since is honoured on its own, as --until is. It used to count only with
    # --backfill, so the README's `calendar-sync --since 2026-01-01` listed the
    # last seven days and exited 0 with the gap it was run to fill still open.
    until = datetime.fromisoformat(args.until) if args.until else now + timedelta(days=30)
    if args.since:
        since = datetime.fromisoformat(args.since)
    elif args.backfill:
        try:
            flagged = [] if args.until else sorted(density(conn)["incomplete"])
        except sqlite3.OperationalError:  # a store with no calendar yet
            flagged = []
        if flagged:
            months = [datetime.fromisoformat(f"{month}-01") for month in flagged]
            windows = [(first, (first + timedelta(days=32)).replace(day=1)) for first in months]
            return windows, (
                f"Calendar backfill: the {len(flagged)} month(s) coverage flags, "
                f"{flagged[0]} to {flagged[-1]}"
            )
        since = now - timedelta(days=365)
    else:
        since = now - timedelta(days=LOOKBACK_DAYS)
    if not args.backfill:
        return [(since, until)], f"Calendar sync: {since.date()} to {until.date()}"

    done = listed_months(conn)
    windows, skipped = [], 0
    for start, end in chunk_date_range(since, until):
        if (
            _whole_month(start, end, now) in done
            and not conn.execute(
                "SELECT 1 FROM calendar_events WHERE llm_status = 'pending' "
                "AND start_at >= ? AND start_at < ? LIMIT 1",
                (_utc_bound(start), _utc_bound(end)),
            ).fetchone()
        ):
            skipped += 1
            continue
        windows.append((start, end))
    header = f"Calendar backfill: {since.date()} to {until.date()}, a month at a time"
    if skipped:
        header += f"; {skipped} month(s) already listed in full skipped"
    return windows, header


def _plain_run(args) -> bool:
    return not (args.backfill or args.since or args.until)


def _deep_refreshed_at(conn) -> datetime | None:
    """When the last complete deep refresh began (DEEP_REFRESH_KEY), or None: none recorded,
    or a value that does not read as a time with its zone."""
    import sqlite3

    try:
        row = conn.execute(
            "SELECT value FROM sync_metadata WHERE key = ?", (DEEP_REFRESH_KEY,)
        ).fetchone()
        last = datetime.fromisoformat(row[0]) if row else None
    except (sqlite3.OperationalError, TypeError, ValueError):  # no sync_metadata yet
        return None
    return last if last is not None and last.tzinfo is not None else None


def _deep_window(conn, args, now: datetime) -> tuple[datetime, datetime] | None:
    """The older window a plain run also lists when the deep refresh is due (see
    DEEP_REFRESH_KEY): the DEEP_LOOKBACK_DAYS before the plain run's week. None for a
    backfill, a run given --since or --until, a refresh done in the last
    DEEP_REFRESH_HOURS, and on a store that records none: the first plain run starts the
    clock, as the consolidation's high-water mark does, so a new store or a new release
    adds no listing to the run that meets it."""
    from datetime import UTC, timedelta

    last = _deep_refreshed_at(conn)
    if not _plain_run(args) or last is None:
        return None
    if datetime.now(UTC) - last < timedelta(hours=DEEP_REFRESH_HOURS):
        return None
    end = now - timedelta(days=LOOKBACK_DAYS)
    return end - timedelta(days=DEEP_LOOKBACK_DAYS), end


def _calendar_unchanged(existing, list_change_key) -> bool:
    """Whether a listed event needs no fetch: its stored etag is the listed one and
    no extraction is owed on it. No etag on either side is never unchanged."""
    return bool(
        existing
        and list_change_key
        and existing["change_key"] == list_change_key
        and existing["llm_status"] != "pending"
    )


def _calendar_owed(conn, raw_events: list[dict]) -> tuple[int, int]:
    """What a run would fetch of a listing: (new events, changed or owed ones)."""
    new = changed = 0
    for raw in raw_events:
        existing = conn.execute(
            "SELECT change_key, llm_status FROM calendar_events WHERE outlook_event_id = ?",
            (raw.get("Id", ""),),
        ).fetchone()
        if existing is None:
            new += 1
        elif not _calendar_unchanged(existing, raw.get("@odata.etag")):
            changed += 1
    return new, changed


def run(args):
    """Sync calendar events from Outlook into the knowledge store.

    With --backfill the window is listed a month at a time, oldest first, and each
    whole month listed and stored is recorded, so a run that stops is resumed by
    the next. With --dry-run the window is listed and compared with the store, and
    nothing is fetched, extracted or written.
    """
    from datetime import UTC

    from src.config import USER_EMAIL_PATTERN
    from src.export.calendar_export import (
        BACKFILL_LIST_CALLS,
        get_event_body,
        list_events,
        parse_event,
    )
    from src.export.outlook_cli import OutlookCliAuthRequired
    from src.extract.calendar_extractor import event_body_text, event_prompt_hash, extract_event
    from src.extract.policy_bridge import classify_exception, is_transient
    from src.extract.vertex_auth import touch_sentinel
    from src.llm_policy import Outcome
    from src.store.calendar_coverage import mark_month_started, record_listed_month
    from src.store.calendar_loader import (
        adopt_reissued_occurrence,
        cancel_unlisted,
        dedupe_event_children,
        load_event,
        load_proxy_emails,
        mark_series,
        refresh_self_flags,
        series_extraction,
    )
    from src.store.schema import get_connection, migrate_add_sync_metadata, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found.")
        sys.exit(1)

    # A dry run reads: it neither migrates the store nor writes to it, not even the
    # journal mode a writer's connection sets on open.
    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        from src.store.sql_readonly import connect_read_only

        conn = connect_read_only(db_path)
    else:
        conn = get_connection(db_path)
        run_migrations(conn)

    proxy_emails = None
    if not dry_run:
        proxy_emails = load_proxy_emails(str(DATA_ROOT / "canonical_people.json"))

    now = datetime.now()
    windows, header = _calendar_windows(conn, args, now)
    # Listed after the plain window, so a run short of time has done the week first.
    deep = _deep_window(conn, args, now)
    if deep is not None:
        windows.append(deep)
        header += f"; changed events from {deep[0].date()} to {deep[1].date()}"
    deep_fetched = deep_deferred = 0
    deep_done = False
    print(header + (" (dry run: nothing is fetched or written)" if dry_run else ""))

    stats = {
        "loaded": 0,
        "skipped_unchanged": 0,
        "same_input": 0,
        "series_shared": 0,
        "extracted": 0,
        "failed": 0,
        "deferred": 0,
        "fetch_failed": 0,
        "fetch_failed_known": 0,
    }

    # Consecutive runs each event has failed get-event, kept beside the database.
    # They count the hourly window's runs: a backfill or a dry run leaves them alone.
    fetch_state = Path(db_path).parent / "state" / "calendar_fetch_failures.json"
    track_fetches = not args.backfill and not dry_run
    unfetched_before = _read_json_dict(fetch_state).get("events") if track_fetches else {}
    if not isinstance(unfetched_before, dict):
        unfetched_before = {}
    unfetched_now: dict[str, int] = {}
    unchanged_ids: list[str] = []
    failed_fetch_runs: dict[str, int] = {}
    fetched_ok = False
    session_expired = False
    chunk_failures: list[str] = []
    cancelled = 0
    to_fetch = 0
    listed_until: datetime | None = None

    for start, end in windows:
        month = _whole_month(start, end, now) if args.backfill else None
        if month and not dry_run:
            # Flagged until recorded below: a month half filled when the run stopped
            # can hold enough events to pass for a full one.
            mark_month_started(conn, month, datetime.now(UTC).isoformat())
        window_failures: list[str] = []
        in_deep = (start, end) == deep
        try:
            if args.backfill or in_deep:
                # Each month on a bound of its own: a busy one splits into many calls.
                raw_events = list_events(
                    start, end, failures=window_failures, max_calls=BACKFILL_LIST_CALLS
                )
            else:
                raw_events = list_events(start, end, failures=window_failures)
        except OutlookCliAuthRequired:
            if listed_until is None:
                conn.close()
                print("  Outlook needs re-authentication; nothing listed", file=sys.stderr)
                return EXIT_REAUTH
            # The months before this one are stored and recorded; the next run resumes here.
            session_expired = True
            print(
                f"  Outlook needs re-authentication; from {start.date()} on is left to the "
                "next run",
                file=sys.stderr,
            )
            break
        # The furthest the listing reached: the deep window, listed last, ends earlier.
        listed_until = end if listed_until is None else max(listed_until, end)
        print(
            f"  Fetched {len(raw_events)} events"
            + (f" for {month or start.date()}" if args.backfill else "")
        )
        for failure in window_failures:
            print(f"  Could not fetch {failure}", file=sys.stderr)
        # A deep window listed in part is listed again on the next run (deep_done stays
        # False); it says nothing about the plain window, so it does not fail the run.
        if not in_deep:
            chunk_failures.extend(window_failures)
        listed_ids = [raw.get("Id", "") for raw in raw_events]
        listed = set(listed_ids)

        if dry_run:
            new, changed = _calendar_owed(conn, raw_events)
            to_fetch += new + changed
            print(
                f"  {month or f'{start.date()}..{end.date()}'}: {len(raw_events)} listed, "
                f"{new} new, {changed} changed or owed an extraction"
            )
            continue

        fetch_failures_before = len(failed_fetch_runs)
        for index, raw in enumerate(raw_events):
            event = parse_event(raw)
            # Stored with the row, because the next run compares its list entry to it.
            list_change_key = event.get("change_key")

            # Skip if unchanged AND we do not still owe it an extraction.
            #
            # modified_at alone was not enough. An event whose extraction failed was still
            # upserted with the new modified_at, so on the next run it matched here, scored
            # skipped_unchanged, and its body_summary, decisions and action items were gone for
            # good — no retry, and nothing in the row saying one was owed. llm_status='pending'
            # is that debt, and it is the only value that re-offers an unchanged event.
            #
            # And modified_at never matched at all: list-calendar's $select does not include
            # it, so every event was fetched and re-extracted on every run. The etag comes
            # with every list entry. No etag on either side is never "unchanged".
            existing = conn.execute(
                "SELECT change_key, llm_status, extraction_hash FROM calendar_events "
                "WHERE outlook_event_id = ?",
                (event["outlook_event_id"],),
            ).fetchone()
            if _calendar_unchanged(existing, list_change_key):
                stats["skipped_unchanged"] += 1
                unchanged_ids.append(event["outlook_event_id"])
                continue
            # The deep window fetches at most DEEP_FETCH_CAP; the rest wait for the next run.
            if in_deep:
                if deep_fetched >= DEEP_FETCH_CAP:
                    stats["deferred"] += 1
                    deep_deferred += 1
                    continue
                deep_fetched += 1

            # Fetch full event (list-calendar returns a subset without Attendees,
            # ResponseStatus, Type, SeriesMasterId etc. — get-event has everything)
            try:
                body_raw = get_event_body(event["outlook_event_id"])
            except OutlookCliAuthRequired:
                # The session expired mid-run. Every later fetch would fail the same way,
                # so the run stops asking and exits EXIT_REAUTH, as it does when the
                # session is already dead at list time. Nothing is written, so the next
                # run re-offers all of them, and their fetch-failure counts carry over.
                remaining = listed_ids[index:]
                stats["deferred"] += len(remaining)
                unfetched_now.update(
                    {eid: unfetched_before[eid] for eid in remaining if eid in unfetched_before}
                )
                session_expired = True
                print(
                    f"  Outlook needs re-authentication; {len(remaining)} event(s) deferred "
                    "to the next run",
                    file=sys.stderr,
                )
                break
            if not isinstance(body_raw, dict):
                # A FETCH FAILURE, NOT AN EMPTY BODY, and collapsing the two loses the event
                # exactly the way a blank extraction did. get_event_body swallows everything
                # and returns None, so an Outlook outage arrived here as body_html="", fell
                # past the 50-char gate and was recorded 'skipped' — the one value that says
                # nothing is owed and the one the change detector will never re-offer. Three
                # runs and the summary was gone: run one defers on a dead gcloud, run two
                # overwrites it with 'skipped' while Outlook is down, run three sees nothing
                # to do. Not contrived either: sb-auth-watch.sh fires calendar-sync on
                # `gcloud_or_outlook`, so the run dispatched to recover from an outage is
                # precisely the one that can hit step two before Outlook is back.
                #
                # WRITING NOTHING IS THE FIX, and it is stronger than writing 'pending'.
                # Leaving the row alone preserves its llm_status AND does not advance
                # modified_at, so the change detector re-offers the event on both counts; a
                # brand-new event simply has no row, which it also re-offers. It is the only
                # option that does not DEGRADE what is already stored: `event` here is still
                # the list-calendar subset, which carries no Attendees or ResponseStatus, and
                # load_event replaces the attendee rows wholesale.
                #
                # This does not contradict the ruling that the facts are stored even when the
                # extraction fails. That ruling is "a MODEL failure must not cost you API data
                # you already have". This is its mirror: an API failure must not overwrite API
                # data you already have with less of it.
                event_id = event["outlook_event_id"]
                previous_runs = unfetched_before.get(event_id, 0)
                runs = (previous_runs if isinstance(previous_runs, int) else 0) + 1
                failed_fetch_runs[event_id] = runs
                print(
                    f"  Body fetch failed for {event.get('subject', '???')} ({runs} run(s) in a "
                    "row); leaving the row untouched so the next run re-offers it",
                    file=sys.stderr,
                )
                continue

            fetched_ok = True
            event = parse_event(body_raw)
            event["change_key"] = list_change_key
            # Only get-event names the series: an occurrence Outlook re-issued under a
            # new id takes over its stored row, extraction and all, instead of a second.
            if existing is None and adopt_reissued_occurrence(conn, event, listed):
                existing = conn.execute(
                    "SELECT change_key, llm_status, extraction_hash FROM calendar_events "
                    "WHERE outlook_event_id = ?",
                    (event["outlook_event_id"],),
                ).fetchone()
            body_obj = body_raw.get("Body", {})
            body_html = body_obj.get("Content", "") if isinstance(body_obj, dict) else ""
            # Stored as a reader sees it (calendar_events.body), whatever the extraction does.
            event["body"] = event_body_text(body_html or "")

            # Extract. 'skipped' until something is actually attempted: no body, and under
            # the 50-char floor once the HTML and the online-meeting joining details are
            # out (calendar_extractor decides, and gives no prompt hash then), are "nothing
            # was owed", not "nothing came back", and the fetch above succeeded so both
            # are authoritative.
            #
            # --skip-extraction IS NOT AUTHORITATIVE. A run told not to extract has no basis
            # for discharging a debt, an earlier one or this event's own: a body worth
            # extracting stays 'pending', or with the etag stored it would never be offered
            # again. Latent today, because sb-calendar-sync.sh runs the command bare.
            extraction = {"body_summary": "", "decisions": [], "action_items": []}
            prior_status = existing["llm_status"] if existing else None
            prompt_hash = event_prompt_hash(event, body_html)
            long_enough = prompt_hash is not None
            if args.skip_extraction:
                llm_status = "pending" if long_enough or prior_status == "pending" else "skipped"
            else:
                llm_status = "skipped"
            # The etag moves on edits the model never sees (an attendee answering, a room
            # change). The same prompt hash means the extraction on record was made from
            # this very prompt, so it stands: keep it and refresh only the facts.
            keep_extraction = (
                not args.skip_extraction
                and prompt_hash is not None
                and prior_status == "extracted"
                and existing["extraction_hash"] == prompt_hash
            )
            # An occurrence whose series' invitation another occurrence was read from.
            shared = (
                None
                if keep_extraction or args.skip_extraction
                else series_extraction(conn, event, prompt_hash)
            )
            if keep_extraction:
                llm_status = "extracted"
                stats["same_input"] += 1
            elif shared is not None:
                extraction, llm_status = shared, "extracted"
                stats["series_shared"] += 1
            elif not args.skip_extraction and long_enough:
                try:
                    extraction = extract_event(event, body_html)
                    llm_status = "extracted"
                    stats["extracted"] += 1
                except Exception as e:
                    # Same split, and the same classifier, as attachment_pipeline: a failure
                    # a re-auth would cure is a debt to retry, anything else needs a human.
                    # What reaches here has already exhausted call_with_policy's retries.
                    if classify_exception(e, None) is Outcome.AUTH_REAUTH_REQUIRED:
                        llm_status = "pending"
                        touch_sentinel()
                        stats["deferred"] += 1
                    elif is_transient(e):
                        # The service failed, not the event: offered again next run. The
                        # run still fails, so an outage that lasts is seen.
                        llm_status = "pending"
                        stats["failed"] += 1
                    else:
                        llm_status = "failed"
                        # Once per event: a failure already on record was reported by the
                        # run that recorded it. Only an edited event is offered again.
                        if prior_status != "failed":
                            stats["failed"] += 1
                    print(
                        f"  Extraction {llm_status} for {event.get('subject', '???')}: {e}",
                        file=sys.stderr,
                    )

            # Load. The facts go in regardless — they come from the API, not the model, so
            # they are good even when the extraction that should have accompanied them is not.
            load_event(
                conn,
                event,
                extraction,
                user_email_pattern=USER_EMAIL_PATTERN,
                proxy_emails=proxy_emails,
                llm_status=llm_status,
                extraction_hash=prompt_hash,
                keep_extraction=keep_extraction,
            )
            stats["loaded"] += 1

        # A complete listing also says which stored events Outlook no longer holds;
        # nothing ever removed them, so deleted and re-created meetings stayed live.
        # Not after a span that failed or came back full, nor once the session
        # expired mid-run: what is missing then says nothing about Outlook. The
        # window was local time to outlook-cli, as it is to start.astimezone().
        if not window_failures and not session_expired:
            cancelled += cancel_unlisted(conn, listed, _utc_bound(start), _utc_bound(end))
            # A whole month gone by, every event of it listed and stored: the next
            # backfill skips it, and coverage stops flagging it however few it holds.
            if month and len(failed_fetch_runs) == fetch_failures_before:
                record_listed_month(conn, month, datetime.now(UTC).isoformat())
            # Every changed event of the deep window fetched: done until it is due again.
            if in_deep and not deep_deferred and len(failed_fetch_runs) == fetch_failures_before:
                deep_done = True
        if session_expired:
            break

    if dry_run:
        conn.close()
        print(f"  Dry run: {to_fetch} event(s) to fetch, nothing written")
        return 1 if chunk_failures else 0

    # All three are idempotent and cheap, and repair rows no upsert will touch
    # again: stacked duplicates from the old append-only loader, occurrences
    # stored without their series flag, and self flags written while
    # BRAIN_USER_EMAIL_PATTERN was unset.
    dup_decisions, dup_actions = dedupe_event_children(conn)
    series_marked = mark_series(conn)
    flags_changed = 0
    if USER_EMAIL_PATTERN and proxy_emails is not None:
        flags_changed = refresh_self_flags(conn, USER_EMAIL_PATTERN, proxy_emails)
    elif proxy_emails is None:
        # canonical_people.json exists but did not parse. Recomputed without it,
        # every event the PA booked would be rewritten as not-self, so the flags
        # wait for the file to be fixed.
        print(
            "  canonical_people.json is unreadable; leaving the stored self flags alone",
            file=sys.stderr,
        )
    else:
        # Without the pattern every stored flag would be rewritten to not-self.
        print(
            "  BRAIN_USER_EMAIL_PATTERN is unset; leaving the stored self flags alone",
            file=sys.stderr,
        )
    # The cap needs evidence that get-event works at all, from this run. On a
    # quiet calendar the failing events may be the only ones fetched, so fetch
    # one unchanged event as a canary. If nothing was fetchable, every failure
    # counts, known or not: that is an outage, not one bad event.
    if failed_fetch_runs and not fetched_ok and unchanged_ids and not session_expired:
        try:
            fetched_ok = isinstance(get_event_body(unchanged_ids[0]), dict)
        except OutlookCliAuthRequired:
            session_expired = True
    for runs in failed_fetch_runs.values():
        if fetched_ok and runs > CALENDAR_FETCH_ALERT_RUNS:
            stats["fetch_failed_known"] += 1
        else:
            stats["fetch_failed"] += 1

    if deep_done or (_plain_run(args) and _deep_refreshed_at(conn) is None):
        migrate_add_sync_metadata(conn)
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
            (DEEP_REFRESH_KEY, datetime.now(UTC).isoformat()),
        )
        conn.commit()

    # Not either for a listing that never reached the present, as a backfill of
    # months gone by does not: it says nothing about the hourly window.
    reached_now = listed_until is not None and listed_until >= now
    if not chunk_failures and not stats["fetch_failed"] and reached_now:
        # The run itself is the liveness signal. With a working change detector an
        # unchanged calendar writes no event row, so MAX(ingested_at) stopped
        # proving the sync ran; scripts/health_check.py reads this as well. Not on
        # a run where get-event failed for real, or an outage would read as fresh.
        from datetime import UTC

        migrate_add_sync_metadata(conn)
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('calendar_last_listed', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()
    conn.close()

    # Only events that failed this run (or were deferred before their turn) keep a
    # count, so a success or an event gone from the window clears it. The file is
    # bookkeeping: the rows are committed, so failing to write it is a warning.
    import json
    import os
    import tempfile

    if track_fetches:
        unfetched_now.update(failed_fetch_runs)
        new_state = {"events": unfetched_now}
        try:
            fetch_state.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", dir=fetch_state.parent, prefix=fetch_state.name, delete=False
            ) as tmp:
                json.dump(new_state, tmp, indent=1)
            os.replace(tmp.name, fetch_state)
        except OSError as e:
            print(f"  Could not write {fetch_state}: {e}", file=sys.stderr)

    print(f"  Loaded:     {stats['loaded']}")
    print(f"  Unchanged:  {stats['skipped_unchanged']}")
    print(f"  Same input: {stats['same_input']} (etag moved, extraction kept)")
    if stats["series_shared"]:
        print(f"  Series:     {stats['series_shared']} (read from another occurrence)")
    print(f"  Extracted:  {stats['extracted']}")
    print(f"  Failed:     {stats['failed']}")
    print(f"  Deferred:   {stats['deferred']}")
    print(f"  Fetch fail: {stats['fetch_failed']}")
    if stats["fetch_failed_known"]:
        print(
            f"  Still unfetchable after {CALENDAR_FETCH_ALERT_RUNS} runs (not failing the "
            f"run): {stats['fetch_failed_known']}"
        )
    if dup_decisions or dup_actions:
        print(f"  Removed duplicate decisions/actions: {dup_decisions}/{dup_actions}")
    if series_marked:
        print(f"  Occurrences flagged recurring: {series_marked}")
    if flags_changed:
        print(f"  Self flags corrected: {flags_changed}")
    if cancelled:
        print(f"  No longer in Outlook, marked cancelled: {cancelled}")

    # Every one of these is already recorded so the next run re-offers or
    # reports it; the exit code is what tells the scheduler this run did not do
    # its job. A dead gcloud (sentinel set) is a deferral, not a failure; an
    # expired Outlook session is EXIT_REAUTH, wherever in the run it shows.
    if session_expired:
        return EXIT_REAUTH
    if chunk_failures or stats["fetch_failed"] or stats["failed"]:
        print(
            f"  Run incomplete: {len(chunk_failures)} window chunk(s), "
            f"{stats['fetch_failed']} body fetch(es), {stats['failed']} extraction(s) failed",
            file=sys.stderr,
        )
        return 1
    return 0
