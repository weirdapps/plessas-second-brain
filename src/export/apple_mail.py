# mypy: ignore-errors
"""Apple Mail export via AppleScript — batch extraction with safety measures.

Exports emails from Apple Mail Exchange mailboxes to JSON files.
Supports multi-mailbox scanning: Archive (index-based resume) and all other
mailboxes (message_id-based deduplication for volatile folders like Inbox).

Implements comprehensive safety measures to prevent Mail.app crashes and timeouts.
"""

# ⚠️ FROZEN AS OF 2026-04-22 — replaced by src/export/outlook_export.py.
# This file is kept for one release cycle to allow rollback. After 14 days of
# stable operation on outlook_export.py, this module will be deleted.
# Do NOT add new code here. Bug fixes only if you absolutely need to roll back.
# The `# mypy: ignore-errors` directive at the top of file silences pre-existing
# latent type errors; this module is on a deletion track, not a refactor track.

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.store.schema import get_connection

from .state import load_state

# Constants
BATCH_SIZE = 10
BATCH_COOLDOWN = 1  # seconds between batches
LONG_COOLDOWN = 15  # seconds every 50 batches
LONG_COOLDOWN_INTERVAL = 50  # batches
MEMORY_THRESHOLD_GB = 3.0
MEMORY_CRITICAL_GB = 5.0
MEMORY_PAUSE = 60  # seconds
TIMEOUT_SECONDS = 60
TIMEOUT_RETRY_WAIT = 60  # seconds
MAIL_WAKE_TIMEOUT = 120  # seconds to wait for Mail to become responsive
MAIL_WAKE_RETRIES = 3  # number of wake attempts

# Shared AppleScript helper handlers for JSON escaping
_APPLESCRIPT_HELPERS = """
    on escapeJSON(txt)
        if txt is missing value then return ""
        set txt to txt as text
        set txt to my replaceText(txt, "\\\\", "\\\\\\\\")
        set txt to my replaceText(txt, "\\"", "\\\\\\"")
        set txt to my replaceText(txt, return & linefeed, "\\\\n")
        set txt to my replaceText(txt, return, "\\\\n")
        set txt to my replaceText(txt, linefeed, "\\\\n")
        set txt to my replaceText(txt, tab, "\\\\t")
        set txt to my replaceText(txt, ASCII character 8, "")
        set txt to my replaceText(txt, ASCII character 12, "\\\\n")
        return txt
    end escapeJSON

    on replaceText(sourceText, findText, replaceWith)
        set AppleScript's text item delimiters to findText
        set textItems to text items of sourceText
        set AppleScript's text item delimiters to replaceWith
        set sourceText to textItems as text
        set AppleScript's text item delimiters to ""
        return sourceText
    end replaceText
"""


class GracefulExit(Exception):
    """Raised to trigger graceful shutdown."""

    pass


def signal_handler(signum: int, frame: Any) -> None:
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    print(f"\nReceived signal {signum}, finishing current batch...")
    raise GracefulExit()


def get_lock_file_path() -> Path:
    """Get path to the export lock file."""
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "data" / "state" / "export.lock"


def acquire_lock() -> None:
    """Acquire export lock, preventing concurrent runs."""
    lock_file = get_lock_file_path()
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    if lock_file.exists():
        try:
            with open(lock_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            raise RuntimeError(f"Export already running (PID {pid}). If stuck, remove {lock_file}")
        except (ProcessLookupError, ValueError):
            print("Removing stale lock file")
            lock_file.unlink()

    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))


def release_lock() -> None:
    """Release export lock."""
    lock_file = get_lock_file_path()
    if lock_file.exists():
        lock_file.unlink()


def check_mail_memory() -> float:
    """Check Mail.app memory usage in GB.

    Returns RSS memory in GB, or 0 if Mail.app not running.
    """
    try:
        pgrep_result = subprocess.run(
            ["pgrep", "-x", "Mail"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if pgrep_result.returncode != 0 or not pgrep_result.stdout.strip():
            return 0.0

        pid = pgrep_result.stdout.strip()

        ps_result = subprocess.run(
            ["ps", "-o", "rss=", "-p", pid],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ps_result.returncode == 0 and ps_result.stdout.strip():
            rss_kb = int(ps_result.stdout.strip())
            return rss_kb / (1024 * 1024)
    except (subprocess.TimeoutExpired, ValueError):
        pass
    return 0.0


def ensure_mail_ready() -> None:
    """Restart Mail.app to ensure a clean state before export.

    A full restart prevents hangs caused by accumulated AppleScript sessions
    or stale Exchange connections — especially important for the 41K+ Archive.
    """
    # Quit Mail if running
    pgrep = subprocess.run(["pgrep", "-x", "Mail"], capture_output=True, timeout=5)
    if pgrep.returncode == 0:
        print("Restarting Mail.app for clean state...")
        subprocess.run(
            ["osascript", "-e", 'tell application "Mail" to quit'],
            capture_output=True,
            timeout=15,
        )
        time.sleep(5)
        # Force-kill if still running
        subprocess.run(["pkill", "-x", "Mail"], capture_output=True, timeout=5)
        time.sleep(3)

    # Launch Mail in background
    subprocess.run(["open", "-g", "-a", "Mail"], timeout=10)
    time.sleep(10)  # give it time to start and connect to Exchange

    # Verify Mail responds to AppleScript
    test_script = 'tell application "Mail" to name'
    for attempt in range(1, MAIL_WAKE_RETRIES + 1):
        try:
            subprocess.run(
                ["osascript", "-e", test_script],
                capture_output=True,
                text=True,
                timeout=MAIL_WAKE_TIMEOUT,
            )
            print("Mail.app is responsive")
            return
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            wait = 15 * attempt
            print(
                f"Mail.app not responsive (attempt {attempt}/{MAIL_WAKE_RETRIES}), waiting {wait}s..."
            )
            time.sleep(wait)

    print("WARNING: Mail.app may not be fully responsive, proceeding anyway")


def run_applescript(script: str, timeout: int = TIMEOUT_SECONDS) -> str:
    """Run AppleScript with timeout.

    Args:
        script: AppleScript code to execute
        timeout: Timeout in seconds

    Returns:
        Script output

    Raises:
        subprocess.TimeoutExpired: If script times out
        subprocess.CalledProcessError: If script fails
    """
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            result.stdout,
            result.stderr,
        )
    return result.stdout.strip()


# Mailboxes to scan — order matters (Archive first for index-based resume)
# User CCs self on all outgoing mail, so Archive + Inbox captures everything.
TARGET_MAILBOXES = {"Archive", "Inbox"}

# Mailboxes to always skip (system/internal folders)
SKIP_MAILBOXES = {
    "Drafts",
    "Πρόχειρα",  # Greek drafts
    "Quarantine",
    "Junk Email",
    "Deleted Items",
    "Outbox",
    "Conflicts",
    "Local Failures",
    "Server Failures",
    "Sync Issues",
    "Conversation History",
    "Journal",
    "RSS Feeds",
    "Snoozed",
    "Tasks",
    "Notes",
}


def discover_mailboxes() -> list[str]:
    """Discover mailboxes in the Exchange account, filtered to target set.

    Returns only mailboxes in TARGET_MAILBOXES, with Archive first.
    Retries until all target mailboxes are found (Exchange may be slow to sync).
    """
    script = """
    tell application "Mail"
        set mbNames to {}
        set acct to account "Exchange"
        repeat with mb in mailboxes of acct
            set end of mbNames to name of mb
        end repeat
        set AppleScript's text item delimiters to linefeed
        return mbNames as text
    end tell
    """
    for attempt in range(1, 6):
        try:
            output = run_applescript(script, timeout=60)
            all_mailboxes = [name.strip() for name in output.split("\n") if name.strip()]
            # Filter to target mailboxes, Archive first
            result = [mb for mb in all_mailboxes if mb in TARGET_MAILBOXES]
            if "Archive" in result:
                result.remove("Archive")
                result.insert(0, "Archive")

            if set(result) == TARGET_MAILBOXES:
                return result

            # Not all target mailboxes found — Exchange may still be syncing
            missing = TARGET_MAILBOXES - set(result)
            if attempt < 5:
                wait = 15 * attempt
                print(
                    f"Missing mailboxes {missing} (attempt {attempt}/5), "
                    f"waiting {wait}s for Exchange sync..."
                )
                time.sleep(wait)
            else:
                print(
                    f"Warning: could not find {missing} after 5 attempts, "
                    f"proceeding with {result or ['Archive']}"
                )
                return result if result else ["Archive"]
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            if attempt < 5:
                wait = 15 * attempt
                print(f"Mailbox discovery timeout (attempt {attempt}/5), retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"Error discovering mailboxes after 5 attempts: {e}")
                return ["Archive"]  # fallback


def _get_seen_ids_cache_path() -> Path:
    """Get path to the seen-IDs cache file."""
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "data" / "state" / "seen_ids.json"


def load_seen_ids() -> set[str]:
    """Load all already-exported message_ids, using a cache for speed.

    On first run, scans all staging files (~865MB) and saves a cache.
    On subsequent runs, only scans batch files newer than the cache.

    Returns:
        Set of message_id strings already in staging.
    """
    repo_root = Path(__file__).parent.parent.parent
    staging_path = repo_root / "data" / "staging"
    cache_path = _get_seen_ids_cache_path()

    if not staging_path.exists():
        return set()

    # Try loading from cache
    seen = set()
    cache_mtime = 0.0
    if cache_path.exists():
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
                seen = set(cached.get("ids", []))
                cache_mtime = cache_path.stat().st_mtime
                print(f"  Loaded {len(seen)} IDs from cache")
        except (json.JSONDecodeError, KeyError):
            seen = set()
            cache_mtime = 0.0

    # Scan only batch files newer than cache (or all if no cache)
    new_files = 0
    for batch_file in staging_path.glob("batch-*.json"):
        if batch_file.stat().st_mtime <= cache_mtime:
            continue
        new_files += 1
        try:
            with open(batch_file, encoding="utf-8") as f:
                batch = json.load(f)
                emails = batch.get("emails", batch) if isinstance(batch, dict) else batch
                for email in emails:
                    seen.add(str(email["message_id"]))
        except (json.JSONDecodeError, KeyError):
            continue

    if new_files > 0:
        print(f"  Scanned {new_files} new batch files, total {len(seen)} IDs")

    # Save updated cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"ids": list(seen), "updated": datetime.now().isoformat()}, f)

    return seen


def get_message_count(mailbox: str = "Archive") -> int:
    """Get total count of messages in a mailbox, with retry on timeout."""
    script = f"""
    tell application "Mail"
        set targetMailbox to mailbox "{mailbox}" of account "Exchange"
        count messages of targetMailbox
    end tell
    """
    for attempt in range(1, 4):
        try:
            count_str = run_applescript(script, timeout=90)
            return int(count_str)
        except subprocess.TimeoutExpired:
            if attempt < 3:
                wait = 15 * attempt
                print(
                    f"  Message count timeout for {mailbox} (attempt {attempt}/3), retrying in {wait}s..."
                )
                time.sleep(wait)
            else:
                raise


def extract_single_email(index: int, mailbox: str = "Archive") -> dict | None:
    """Extract a single email by index with retry on timeout.

    Args:
        index: 1-based message index
        mailbox: Mailbox name to extract from

    Returns:
        Email dictionary or None if extraction fails after retries
    """
    escaped_mailbox = mailbox.replace('"', '\\"')
    script = (
        f"""
    tell application "Mail"
        set targetMailbox to mailbox "{escaped_mailbox}" of account "Exchange"
        set msg to message {index} of targetMailbox

        set msgId to id of msg as text
        set msgSubject to subject of msg as text
        set msgContent to content of msg as text

        set senderName to extract name from sender of msg
        set senderAddress to extract address from sender of msg

        -- Build ISO 8601 date from components
        set d to date received of msg
        set y to year of d as text
        set m to month of d as integer
        set dy to day of d
        set h to hours of d
        set mi to minutes of d
        set s to seconds of d
        if m < 10 then set m to "0" & m
        if dy < 10 then set dy to "0" & dy
        if h < 10 then set h to "0" & h
        if mi < 10 then set mi to "0" & mi
        if s < 10 then set s to "0" & s
        set msgDate to y & "-" & m & "-" & dy & "T" & h & ":" & mi & ":" & s

        -- Extract to recipients
        set toList to ""
        try
            set toCount to count of to recipients of msg
            repeat with r from 1 to toCount
                set recip to to recipient r of msg
                set recipName to name of recip
                if recipName is missing value then set recipName to ""
                set recipAddr to address of recip
                if recipAddr is missing value then set recipAddr to ""
                if r > 1 then set toList to toList & ", "
                set toList to toList & "{{\\"name\\": \\"" & my escapeJSON(recipName) & "\\", \\"address\\": \\"" & my escapeJSON(recipAddr) & "\\"}}"
            end repeat
        end try

        -- Extract cc recipients
        set ccList to ""
        try
            set ccCount to count of cc recipients of msg
            repeat with r from 1 to ccCount
                set recip to cc recipient r of msg
                set recipName to name of recip
                if recipName is missing value then set recipName to ""
                set recipAddr to address of recip
                if recipAddr is missing value then set recipAddr to ""
                if r > 1 then set ccList to ccList & ", "
                set ccList to ccList & "{{\\"name\\": \\"" & my escapeJSON(recipName) & "\\", \\"address\\": \\"" & my escapeJSON(recipAddr) & "\\"}}"
            end repeat
        end try

        -- Extract In-Reply-To, References, and Message-ID headers.
        -- Message-ID is the RFC822 universal identifier; storing it lets us
        -- dedup against outlook-cli ingest of the same message.
        set inReplyTo to ""
        set msgRefs to ""
        set internetMsgId to ""
        try
            repeat with h in (every header of msg)
                set hName to name of h
                if hName is "In-Reply-To" then set inReplyTo to content of h
                if hName is "References" then set msgRefs to content of h
                if hName is "Message-ID" then set internetMsgId to content of h
                if hName is "Message-Id" then set internetMsgId to content of h
            end repeat
        end try

        -- Build JSON output
        set output to "{{"
        set output to output & "\\"message_id\\": \\"" & msgId & "\\", "
        set output to output & "\\"internet_message_id\\": \\"" & my escapeJSON(internetMsgId) & "\\", "
        set output to output & "\\"date_received\\": \\"" & msgDate & "\\", "
        set output to output & "\\"subject\\": \\"" & my escapeJSON(msgSubject) & "\\", "
        set output to output & "\\"sender\\": {{\\"name\\": \\"" & my escapeJSON(senderName) & "\\", \\"address\\": \\"" & my escapeJSON(senderAddress) & "\\"}}, "
        set output to output & "\\"to_recipients\\": [" & toList & "], "
        set output to output & "\\"cc_recipients\\": [" & ccList & "], "
        set output to output & "\\"in_reply_to\\": \\"" & my escapeJSON(inReplyTo) & "\\", "
        set output to output & "\\"references\\": \\"" & my escapeJSON(msgRefs) & "\\", "
        set output to output & "\\"mailbox_name\\": \\"" & my escapeJSON("{escaped_mailbox}") & "\\", "
        set output to output & "\\"content\\": \\"" & my escapeJSON(msgContent) & "\\""
        set output to output & "}}"

        return output
    end tell
    """
        + _APPLESCRIPT_HELPERS
    )

    retry_count = 0
    max_retries = 1

    while retry_count <= max_retries:
        try:
            output = run_applescript(script, timeout=TIMEOUT_SECONDS)
            return json.loads(output)
        except subprocess.TimeoutExpired:
            retry_count += 1
            if retry_count > max_retries:
                print(f"Timeout extracting message {index} after {max_retries + 1} attempts")
                return None
            print(f"Timeout on message {index}, waiting {TIMEOUT_RETRY_WAIT}s before retry...")
            time.sleep(TIMEOUT_RETRY_WAIT)
        except subprocess.CalledProcessError as e:
            print(f"AppleScript error on message {index}: {e.stderr}")
            return None
        except json.JSONDecodeError as e:
            print(f"JSON parse error on message {index}: {e}")
            return None

    return None


def extract_email_batch(start_index: int, end_index: int, mailbox: str = "Archive") -> list[dict]:
    """Extract a batch of emails in a single AppleScript call.

    Falls back to individual extraction if batch call fails.

    Args:
        start_index: 1-based start index (inclusive)
        end_index: 1-based end index (inclusive)
        mailbox: Mailbox name to extract from

    Returns:
        List of email dictionaries
    """
    escaped_mailbox = mailbox.replace('"', '\\"')
    script = (
        f"""
    tell application "Mail"
        set targetMailbox to mailbox "{escaped_mailbox}" of account "Exchange"
        set jsonArray to "["

        repeat with i from {start_index} to {end_index}
            try
                set msg to message i of targetMailbox
                set msgId to id of msg as text
                set msgSubject to subject of msg as text
                set msgContent to content of msg as text
                set senderName to extract name from sender of msg
                set senderAddress to extract address from sender of msg

                -- Date components
                set d to date received of msg
                set y to year of d as text
                set m to month of d as integer
                set dy to day of d
                set h to hours of d
                set mi to minutes of d
                set s to seconds of d
                if m < 10 then set m to "0" & m
                if dy < 10 then set dy to "0" & dy
                if h < 10 then set h to "0" & h
                if mi < 10 then set mi to "0" & mi
                if s < 10 then set s to "0" & s
                set msgDate to y & "-" & m & "-" & dy & "T" & h & ":" & mi & ":" & s

                -- Recipients
                set toList to ""
                try
                    set toCount to count of to recipients of msg
                    repeat with r from 1 to toCount
                        set recip to to recipient r of msg
                        set recipName to name of recip
                        if recipName is missing value then set recipName to ""
                        set recipAddr to address of recip
                        if recipAddr is missing value then set recipAddr to ""
                        if r > 1 then set toList to toList & ", "
                        set toList to toList & "{{\\"name\\": \\"" & my escapeJSON(recipName) & "\\", \\"address\\": \\"" & my escapeJSON(recipAddr) & "\\"}}"
                    end repeat
                end try

                set ccList to ""
                try
                    set ccCount to count of cc recipients of msg
                    repeat with r from 1 to ccCount
                        set recip to cc recipient r of msg
                        set recipName to name of recip
                        if recipName is missing value then set recipName to ""
                        set recipAddr to address of recip
                        if recipAddr is missing value then set recipAddr to ""
                        if r > 1 then set ccList to ccList & ", "
                        set ccList to ccList & "{{\\"name\\": \\"" & my escapeJSON(recipName) & "\\", \\"address\\": \\"" & my escapeJSON(recipAddr) & "\\"}}"
                    end repeat
                end try

                -- Extract In-Reply-To, References, and Message-ID headers
                -- (Message-ID is the RFC822 universal id; used for cross-source dedup).
                set inReplyTo to ""
                set msgRefs to ""
                set internetMsgId to ""
                try
                    repeat with h in (every header of msg)
                        set hName to name of h
                        if hName is "In-Reply-To" then set inReplyTo to content of h
                        if hName is "References" then set msgRefs to content of h
                        if hName is "Message-ID" then set internetMsgId to content of h
                        if hName is "Message-Id" then set internetMsgId to content of h
                    end repeat
                end try

                -- Build JSON object
                set emailJson to "{{"
                set emailJson to emailJson & "\\"message_id\\": \\"" & msgId & "\\", "
                set emailJson to emailJson & "\\"internet_message_id\\": \\"" & my escapeJSON(internetMsgId) & "\\", "
                set emailJson to emailJson & "\\"date_received\\": \\"" & msgDate & "\\", "
                set emailJson to emailJson & "\\"subject\\": \\"" & my escapeJSON(msgSubject) & "\\", "
                set emailJson to emailJson & "\\"sender\\": {{\\"name\\": \\"" & my escapeJSON(senderName) & "\\", \\"address\\": \\"" & my escapeJSON(senderAddress) & "\\"}}, "
                set emailJson to emailJson & "\\"to_recipients\\": [" & toList & "], "
                set emailJson to emailJson & "\\"cc_recipients\\": [" & ccList & "], "
                set emailJson to emailJson & "\\"in_reply_to\\": \\"" & my escapeJSON(inReplyTo) & "\\", "
                set emailJson to emailJson & "\\"references\\": \\"" & my escapeJSON(msgRefs) & "\\", "
                set emailJson to emailJson & "\\"mailbox_name\\": \\"" & my escapeJSON("{escaped_mailbox}") & "\\", "
                set emailJson to emailJson & "\\"content\\": \\"" & my escapeJSON(msgContent) & "\\""
                set emailJson to emailJson & "}}"

                if i > {start_index} then set jsonArray to jsonArray & ", "
                set jsonArray to jsonArray & emailJson
            on error errMsg
                -- Skip problematic messages silently
            end try
        end repeat

        set jsonArray to jsonArray & "]"
        return jsonArray
    end tell
    """
        + _APPLESCRIPT_HELPERS
    )

    # Try batch extraction first (much faster — one osascript call)
    try:
        output = run_applescript(script, timeout=TIMEOUT_SECONDS * 3)  # longer timeout for batch
        emails = json.loads(output)
        if isinstance(emails, list):
            return emails
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as e:
        print(f"Batch extraction failed ({e}), falling back to individual...")

    # Fallback: extract individually
    emails = []
    for i in range(start_index, end_index + 1):
        email = extract_single_email(i, mailbox)
        if email:
            emails.append(email)
    return emails


def save_batch(batch_number: int, emails: list[dict]) -> None:
    """Save batch to JSON file in staging directory.

    Args:
        batch_number: Batch number (1-based)
        emails: List of email dictionaries
    """
    repo_root = Path(__file__).parent.parent.parent
    staging_dir = repo_root / "data" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    batch_file = staging_dir / f"batch-{batch_number:05d}.json"

    batch_data = {
        "batch_number": batch_number,
        "exported_at": datetime.now().isoformat(),
        "emails": emails,
    }

    with open(batch_file, "w") as f:
        json.dump(batch_data, f, indent=2)


def _get_archive_max_date_from_db() -> str | None:
    """Get the most recent date_received we have for Archive in the DB.

    Used as the stop cutoff for newest-first Archive scans — ensures we
    backfill any gap between the last successful Archive ingest and now.
    Returns None if DB is missing or has no Archive emails.
    """
    import sqlite3

    repo_root = Path(__file__).parent.parent.parent
    db_path = repo_root / "data" / "brain.db"
    if not db_path.exists():
        return None
    try:
        conn = get_connection(str(db_path))
        try:
            row = conn.execute(
                "SELECT MAX(date_received) FROM emails WHERE mailbox_name = 'Archive'"
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _export_archive(
    state: dict,
    seen_ids: set[str],
    start_batch: int,
    limit: int,
    since_date: str | None = None,
) -> tuple[int, int, int]:
    """Export from Archive mailbox using newest-first scan with date-based stop.

    Apple Mail's Archive is ordered newest-first (index 1 = newest message).
    New mail arrives at low indices and shifts older messages down. We walk
    forward from index 1 and stop once we reach messages older than the cutoff.

    Args:
        since_date: ISO datetime — stop scanning once we hit messages older
            than this (with a 24h safety buffer for late-arriving mail).
            If None, scans the entire Archive (full bootstrap).

    Returns:
        (batch_num, new_count, failed_count) after export
    """
    mailbox = "Archive"
    print(f"\n--- Mailbox: {mailbox} (newest-first, date-bounded) ---")

    # Compute stop cutoff: use the most recent Archive email actually in DB.
    # This is more reliable than `since_date` (the wall-clock last sync time)
    # because it auto-handles backfill — if Archive sync was broken for weeks,
    # we'll scan back until we reach known territory.
    SAFETY_HOURS = 24
    stop_date: str | None = None
    archive_max_date = _get_archive_max_date_from_db()
    cutoff_source = archive_max_date or since_date
    if cutoff_source:
        try:
            cutoff = datetime.fromisoformat(cutoff_source) - timedelta(hours=SAFETY_HOURS)
            stop_date = cutoff.isoformat()
            origin = "DB Archive max" if archive_max_date else "since_date"
            print(f"  Stop cutoff: messages older than {stop_date} (from {origin})")
        except ValueError as e:
            print(f"  Warning: bad cutoff {cutoff_source!r} ({e}), full scan")

    try:
        actual_count = get_message_count(mailbox)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        print(f"  Could not get message count ({e}), aborting Archive scan")
        return start_batch, 0, 0

    if actual_count == 0:
        print("  Archive is empty")
        return start_batch, 0, 0

    print(f"  Archive has {actual_count} messages, scanning from newest...")

    if limit > 0:
        max_index = min(actual_count, limit)
    else:
        max_index = actual_count

    batch_num = start_batch
    current_index = 1
    new_count = 0
    failed_count = 0
    newest_date_seen = state.get("last_exported_date") or ""

    while current_index <= max_index:
        try:
            memory_gb = check_mail_memory()
            if memory_gb >= MEMORY_CRITICAL_GB:
                print(f"  CRITICAL: Mail.app using {memory_gb:.1f}GB, stopping")
                break
            elif memory_gb >= MEMORY_THRESHOLD_GB:
                print(f"  WARNING: Mail.app using {memory_gb:.1f}GB, pausing {MEMORY_PAUSE}s...")
                time.sleep(MEMORY_PAUSE)

            end_index = min(current_index + BATCH_SIZE - 1, max_index)
            print(f"  Batch {batch_num}: messages {current_index}-{end_index}...", end=" ")

            emails = extract_email_batch(current_index, end_index, mailbox)
            batch_failed = (end_index - current_index + 1) - len(emails)
            failed_count += batch_failed

            if not emails:
                print("EMPTY (skipping)")
                current_index = end_index + 1
                continue

            dates_in_batch = [e.get("date_received", "") for e in emails]
            batch_max_date = max(dates_in_batch)
            batch_min_date = min(dates_in_batch)

            # Stop if entire batch is older than cutoff — we've reached
            # previously-synced territory. Subsequent batches are even older.
            if stop_date and batch_max_date and batch_max_date < stop_date:
                print(f"REACHED CUTOFF (newest in batch {batch_max_date} < {stop_date})")
                break

            # Filter to truly new emails: not in seen_ids AND newer than cutoff
            new_emails = []
            for e in emails:
                msg_id = str(e["message_id"])
                if msg_id in seen_ids:
                    continue
                if stop_date and e.get("date_received", "") < stop_date:
                    continue
                new_emails.append(e)
                seen_ids.add(msg_id)

            skipped = len(emails) - len(new_emails)

            if new_emails:
                save_batch(batch_num, new_emails)
                new_count += len(new_emails)
                if batch_max_date > newest_date_seen:
                    newest_date_seen = batch_max_date

                from .state import load_state as _ls
                from .state import save_state as _ss

                st = _ls()
                st["last_batch_number"] = batch_num
                st["last_exported_date"] = newest_date_seen
                _ss(st)

                status = f"OK ({len(new_emails)} new"
                if skipped > 0:
                    status += f", {skipped} dupes/old"
                if batch_failed > 0:
                    status += f", {batch_failed} failed"
                status += ")"
                print(status)
                batch_num += 1
            else:
                print(f"NO NEW ({skipped} dupes/old)")

            # Stop if batch straddled cutoff (oldest in batch < cutoff) —
            # we processed any new ones, and everything older is already known.
            if stop_date and batch_min_date and batch_min_date < stop_date:
                print(f"  Reached cutoff (oldest in batch {batch_min_date} < {stop_date})")
                break

            current_index = end_index + 1
            time.sleep(BATCH_COOLDOWN)

            if batch_num > start_batch and (batch_num - start_batch) % LONG_COOLDOWN_INTERVAL == 0:
                print(f"  Long cooldown ({LONG_COOLDOWN}s)...")
                time.sleep(LONG_COOLDOWN)

        except GracefulExit:
            print("\n  Graceful shutdown...")
            raise
        except Exception as e:
            print(f"  ERROR: {e}")
            break

    if new_count > 0:
        print(f"  Exported {new_count} new messages from Archive (newest: {newest_date_seen})")
    else:
        print("  Archive is up to date")

    return batch_num, new_count, failed_count


def _export_mailbox(
    mailbox: str, seen_ids: set[str], start_batch: int, limit: int
) -> tuple[int, int, int]:
    """Export from a volatile mailbox using message_id dedup (full scan).

    For mailboxes like Inbox where messages come and go, we scan all messages
    and skip any whose message_id is already in the seen set.

    Returns:
        (batch_num, new_count, failed_count) after export
    """
    print(f"\n--- Mailbox: {mailbox} (full scan with dedup) ---")

    try:
        total_messages = get_message_count(mailbox)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  Could not access mailbox: {e}")
        return start_batch, 0, 0

    if total_messages == 0:
        print("  Empty mailbox, skipping")
        return start_batch, 0, 0

    print(f"  Found {total_messages} messages, scanning for new ones...")

    batch_num = start_batch
    current_index = 1
    new_count = 0
    failed_count = 0
    pending_emails = []  # accumulate new emails before saving as batch

    if limit > 0:
        total_messages = min(total_messages, limit)

    while current_index <= total_messages:
        try:
            memory_gb = check_mail_memory()
            if memory_gb >= MEMORY_CRITICAL_GB:
                print(f"  CRITICAL: Mail.app using {memory_gb:.1f}GB, stopping")
                break
            elif memory_gb >= MEMORY_THRESHOLD_GB:
                print(f"  WARNING: Mail.app using {memory_gb:.1f}GB, pausing {MEMORY_PAUSE}s...")
                time.sleep(MEMORY_PAUSE)

            end_index = min(current_index + BATCH_SIZE - 1, total_messages)

            emails = extract_email_batch(current_index, end_index, mailbox)
            batch_failed = (end_index - current_index + 1) - len(emails)
            failed_count += batch_failed

            # Filter to only new (unseen) emails
            for email in emails:
                msg_id = str(email["message_id"])
                if msg_id not in seen_ids:
                    pending_emails.append(email)
                    seen_ids.add(msg_id)

            # Save when we have a full batch worth
            while len(pending_emails) >= BATCH_SIZE:
                batch_to_save = pending_emails[:BATCH_SIZE]
                pending_emails = pending_emails[BATCH_SIZE:]
                save_batch(batch_num, batch_to_save)
                # Update Archive state with batch number only (not total_exported — that's Archive-specific)
                from .state import load_state, save_state

                st = load_state()
                st["last_batch_number"] = batch_num
                save_state(st)
                new_count += len(batch_to_save)
                print(f"  Batch {batch_num}: saved {len(batch_to_save)} new from {mailbox}")
                batch_num += 1

            current_index = end_index + 1
            time.sleep(BATCH_COOLDOWN)

        except GracefulExit:
            print("\n  Graceful shutdown...")
            # Save any pending emails before exit
            if pending_emails:
                save_batch(batch_num, pending_emails)
                from .state import load_state, save_state

                st = load_state()
                st["last_batch_number"] = batch_num
                save_state(st)
                new_count += len(pending_emails)
                batch_num += 1
            raise
        except Exception as e:
            print(f"  ERROR: {e}")
            break

    # Save remaining pending emails
    if pending_emails:
        save_batch(batch_num, pending_emails)
        from .state import load_state, save_state

        st = load_state()
        st["last_batch_number"] = batch_num
        save_state(st)
        new_count += len(pending_emails)
        print(f"  Batch {batch_num}: saved {len(pending_emails)} new from {mailbox}")
        batch_num += 1

    scanned = min(total_messages, current_index - 1)
    print(f"  Scanned {scanned} messages, found {new_count} new")

    return batch_num, new_count, failed_count


def export_emails(limit: int = 0, since_date: str | None = None) -> None:
    """Main export function — extract emails from all Exchange mailboxes.

    Scans Archive (date-filtered) first, then all other mailboxes
    (full scan with message_id dedup). Skips mailboxes that are empty or
    inaccessible.

    Args:
        limit: Maximum number of new emails to export per mailbox (0 = no limit)
        since_date: ISO date cutoff for Archive (passed from sync command)
    """
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Ensure Mail.app is running and responsive (critical for launchd runs)
        ensure_mail_ready()

        acquire_lock()
        print("Export lock acquired")

        state = load_state()
        start_batch = state["last_batch_number"] + 1

        # Load all already-exported message_ids for dedup
        print("Loading exported message IDs for dedup...")
        seen_ids = load_seen_ids()
        print(f"Found {len(seen_ids)} already-exported messages")

        # Discover all mailboxes
        print("Discovering Exchange mailboxes...")
        all_mailboxes = discover_mailboxes()
        print(f"Found {len(all_mailboxes)} mailboxes: {', '.join(all_mailboxes)}")

        start_time = time.time()
        total_new = 0
        total_failed = 0
        batch_num = start_batch

        # 1. Archive first (date-filtered, fast on large mailboxes)
        if "Archive" in all_mailboxes:
            try:
                batch_num, new, failed = _export_archive(
                    state, seen_ids, batch_num, limit, since_date=since_date
                )
                total_new += new
                total_failed += failed
            except GracefulExit:
                total_time = time.time() - start_time
                print(
                    f"\nExport interrupted. New emails: {total_new}, Time: {total_time / 60:.1f}min"
                )
                return

        # 2. All other mailboxes (full scan with dedup)
        for mailbox in all_mailboxes:
            if mailbox == "Archive":
                continue  # already handled
            try:
                batch_num, new, failed = _export_mailbox(mailbox, seen_ids, batch_num, limit)
                total_new += new
                total_failed += failed
            except GracefulExit:
                total_time = time.time() - start_time
                print(
                    f"\nExport interrupted. New emails: {total_new}, Time: {total_time / 60:.1f}min"
                )
                return

        total_time = time.time() - start_time
        print("\nExport summary:")
        print(f"  Mailboxes scanned: {len(all_mailboxes)}")
        print(f"  New emails exported: {total_new}")
        print(f"  Failures: {total_failed}")
        print(f"  Time: {total_time / 60:.1f} minutes")

    finally:
        release_lock()
        print("Export lock released")


if __name__ == "__main__":
    export_emails()
