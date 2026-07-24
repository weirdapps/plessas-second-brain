# mypy: ignore-errors
"""Export email attachments from Apple Mail to local disk.

Iterates through mailbox messages via AppleScript, saves qualifying
attachments to data/attachments/<message_id>/, and records metadata
in the attachments database table.

Implements aggressive resilience against Mail.app hangs:
- Smaller batches (5 messages) and lower memory thresholds
- Automatic hang detection with force-restart and retry
- Proactive Mail.app restarts every 200 batches
- State saved before each batch (crash-safe)
- Per-attachment save timeout in AppleScript
"""

# ⚠️ FROZEN AS OF 2026-04-22 — replaced by src/export/outlook_export.py.
# This file is kept for one release cycle to allow rollback. After 14 days of
# stable operation on outlook_export.py, this module will be deleted.
# Do NOT add new code here. Bug fixes only if you absolutely need to roll back.
# The `# mypy: ignore-errors` directive at the top of file silences pre-existing
# latent type errors; this module is on a deletion track, not a refactor track.

import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from src.config import ATTACHMENTS_DIR, DEFAULT_DB
from src.export.apple_mail import (
    _APPLESCRIPT_HELPERS,
    GracefulExit,
    check_mail_memory,
    discover_mailboxes,
    ensure_mail_ready,
    run_applescript,
)
from src.export.attachment_state import (
    load_state,
    save_state,
)
from src.store.schema import get_connection

# Tuned constants for attachment export (more conservative than email export)
ATT_BATCH_SIZE = 5
ATT_BATCH_COOLDOWN = 3  # seconds between batches
ATT_LONG_COOLDOWN = 30  # seconds every 50 batches
ATT_LONG_COOLDOWN_INTERVAL = 50
ATT_TIMEOUT = 120  # per-batch AppleScript timeout
ATT_MEMORY_WARNING_GB = 2.0
ATT_MEMORY_CRITICAL_GB = 3.5
ATT_RESTART_INTERVAL = 200  # proactive restart every N batches
ATT_SAVE_TIMEOUT = 30  # per-attachment timeout in AppleScript


def _get_lock_path() -> Path:
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "data" / "state" / "attachment_export.lock"


def _acquire_lock() -> None:
    lock = _get_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            with open(lock) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            raise RuntimeError(
                f"Attachment export already running (PID {pid}). If stuck, remove {lock}"
            )
        except (ProcessLookupError, ValueError):
            print("Removing stale attachment export lock")
            lock.unlink()
    with open(lock, "w") as f:
        f.write(str(os.getpid()))


def _release_lock() -> None:
    lock = _get_lock_path()
    if lock.exists():
        lock.unlink()


def _restart_mail() -> bool:
    """Force-restart Mail.app after hang or memory pressure."""
    print("  Force-restarting Mail.app...")
    subprocess.run(["pkill", "-x", "Mail"], capture_output=True, timeout=5)
    time.sleep(10)
    subprocess.run(["open", "-g", "-a", "Mail"], timeout=10)
    time.sleep(15)
    for attempt in range(1, 4):
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Mail" to name'],
                capture_output=True,
                timeout=30,
            )
            print("  Mail.app responsive after restart")
            return True
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            wait = 10 * attempt
            print(f"  Mail.app not responsive (attempt {attempt}/3), waiting {wait}s...")
            time.sleep(wait)
    print("  WARNING: Mail.app not responsive after restart")
    return False


# Patterns for inline/signature images to skip
_INLINE_IMAGE_RE = re.compile(r"^image\d+\.(png|jpg|jpeg|gif|bmp)$", re.IGNORECASE)


def should_skip_attachment(name: str, mime_type: str, file_size: int) -> bool:
    """Check if an attachment should be skipped.

    Skips calendar files, tiny inline images, zero-byte files,
    and known Outlook inline image patterns.
    """
    if file_size == 0:
        return True
    if mime_type and mime_type.lower() == "text/calendar":
        return True
    if mime_type and mime_type.lower().startswith("image/") and file_size < 50 * 1024:
        return True
    if name and _INLINE_IMAGE_RE.match(name):
        return True
    return False


def _build_batch_script(
    start_index: int,
    end_index: int,
    mailbox: str,
    base_path: str,
) -> str:
    """Generate AppleScript to extract attachments from a batch of messages.

    For each message in the range, checks for attachments, applies filters,
    saves qualifying files to disk, and returns JSON metadata.
    """
    escaped_mailbox = mailbox.replace('"', '\\"')
    # Escape base_path for AppleScript string
    escaped_base = base_path.replace("\\", "\\\\").replace('"', '\\"')

    # AppleScript approach: save-first, read-properties-after.
    # Exchange/IMAP doesn't download attachment metadata until the attachment
    # is accessed. Property reads (name, MIME type) fail before save.
    # Saving triggers the download, after which `name` becomes readable.
    # MIME type stays inaccessible, so we detect it from the file extension.
    script = (
        f"""
    tell application "Mail"
        set q to ASCII character 34
        set targetMailbox to mailbox "{escaped_mailbox}" of account "Exchange"
        set jsonArray to "["
        set firstEntry to true
        set messagesFound to 0

        repeat with i from {start_index} to {end_index}
            try
                set msg to message i of targetMailbox
                set msgId to id of msg as text
                set messagesFound to messagesFound + 1
                set attList to every mail attachment of msg

                if (count of attList) > 0 then
                    -- Create directory for this message
                    set saveDir to "{escaped_base}/" & msgId
                    do shell script "mkdir -p " & quoted form of saveDir

                    set attIdx to 0
                    repeat with att in attList
                        set attIdx to attIdx + 1
                        try
                            -- Save to temp name first (properties may not be readable yet)
                            set tmpPath to saveDir & "/_tmp_att_" & attIdx
                            try
                                with timeout of {ATT_SAVE_TIMEOUT} seconds
                                    save att in POSIX file tmpPath
                                end timeout
                            on error
                                -- Save failed, skip this attachment
                            end try

                            -- Check if file was actually saved
                            if (do shell script "test -f " & quoted form of tmpPath & " && echo yes || echo no") is "yes" then
                                -- Read name (usually works after save)
                                set attName to "attachment_" & attIdx
                                try
                                    set attName to name of att
                                end try

                                -- Get file size
                                set fSize to (do shell script "stat -f%z " & quoted form of tmpPath) as integer

                                -- Sanitize filename
                                set cleanName to my replaceText(attName, "/", "_")
                                if cleanName starts with "." then
                                    set cleanName to "_" & cleanName
                                end if

                                -- Skip filters: zero bytes, calendar files, small inline images
                                set shouldKeep to true
                                if fSize is 0 then
                                    set shouldKeep to false
                                end if
                                if cleanName ends with ".ics" then
                                    set shouldKeep to false
                                end if
                                -- Detect inline images: small files with image extensions
                                if fSize < 51200 then
                                    if cleanName ends with ".png" or cleanName ends with ".jpg" or cleanName ends with ".jpeg" or cleanName ends with ".gif" or cleanName ends with ".bmp" then
                                        set shouldKeep to false
                                    end if
                                end if

                                if shouldKeep then
                                    -- Build final path with collision handling
                                    set finalPath to saveDir & "/" & cleanName
                                    set collision to 1
                                    set origClean to cleanName
                                    repeat while (do shell script "test -f " & quoted form of finalPath & " && echo yes || echo no") is "yes"
                                        set collision to collision + 1
                                        if origClean contains "." then
                                            set AppleScript's text item delimiters to "."
                                            set nameParts to text items of origClean
                                            set AppleScript's text item delimiters to ""
                                            set baseName to items 1 thru -2 of nameParts as text
                                            set ext to last item of nameParts
                                            set cleanName to baseName & "_" & collision & "." & ext
                                        else
                                            set cleanName to origClean & "_" & collision
                                        end if
                                        set finalPath to saveDir & "/" & cleanName
                                    end repeat

                                    -- Rename from temp to final
                                    do shell script "mv " & quoted form of tmpPath & " " & quoted form of finalPath

                                    -- Detect MIME type from extension
                                    set detectedMime to "application/octet-stream"
                                    try
                                        set detectedMime to do shell script "file --mime-type -b " & quoted form of finalPath
                                    end try

                                    -- Build JSON entry
                                    if firstEntry is false then
                                        set jsonArray to jsonArray & ", "
                                    end if
                                    set firstEntry to false

                                    set entry to "{{"
                                    set entry to entry & "\\"message_id\\": \\"" & msgId & "\\", "
                                    set entry to entry & "\\"filename\\": \\"" & my escapeJSON(cleanName) & "\\", "
                                    set entry to entry & "\\"mime_type\\": \\"" & my escapeJSON(detectedMime) & "\\", "
                                    set entry to entry & "\\"file_size\\": " & fSize & ", "
                                    set entry to entry & "\\"file_path\\": \\"" & my escapeJSON(finalPath) & "\\""
                                    set entry to entry & "}}"
                                    set jsonArray to jsonArray & entry
                                else
                                    -- Delete filtered temp file
                                    do shell script "rm -f " & quoted form of tmpPath
                                end if
                            end if
                        on error errMsg
                            -- Clean up temp file on error
                            try
                                do shell script "rm -f " & quoted form of (saveDir & "/_tmp_att_" & attIdx)
                            end try
                        end try
                    end repeat

                    -- Remove directory if empty (all attachments filtered)
                    try
                        do shell script "rmdir " & quoted form of saveDir & " 2>/dev/null || true"
                    end try
                end if
            on error errMsg
                -- Skip problematic messages
            end try
        end repeat

        set jsonArray to jsonArray & "]"
        return "{{" & q & "messages_found" & q & ": " & messagesFound & ", " & q & "attachments" & q & ": " & jsonArray & "}}"
    end tell
    """
        + _APPLESCRIPT_HELPERS
    )

    return script


def _process_batch_with_recovery(
    start: int,
    end: int,
    mailbox: str,
    base_path: str,
    max_retries: int = 3,
) -> dict:
    """Process a batch of messages with hang detection and auto-recovery.

    On timeout: force-kills Mail.app, relaunches, retries the same batch.
    After max_retries consecutive failures, skips the batch.

    Returns dict with 'messages_found' (int) and 'attachments' (list).
    """
    for attempt in range(max_retries):
        try:
            script = _build_batch_script(start, end, mailbox, base_path)
            output = run_applescript(script, timeout=ATT_TIMEOUT)
            if not output or output.strip() == "":
                return {"messages_found": 0, "attachments": []}
            result = json.loads(output)
            if isinstance(result, dict) and "messages_found" in result:
                return result
            # Fallback for unexpected format
            return {
                "messages_found": end - start + 1,
                "attachments": result if isinstance(result, list) else [],
            }
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT on batch {start}-{end} (attempt {attempt + 1}/{max_retries})")
            if not _restart_mail():
                break
            time.sleep(5)
        except subprocess.CalledProcessError as e:
            stderr = str(e.stderr or "")
            print(f"  AppleScript error on batch {start}-{end}: {stderr[:200]}")
            if "not running" in stderr.lower() or "connection is invalid" in stderr.lower():
                if not _restart_mail():
                    break
                time.sleep(5)
            else:
                break
        except json.JSONDecodeError as e:
            print(f"  JSON parse error on batch {start}-{end}: {e}")
            break

    print(f"  SKIPPING batch {start}-{end} after {max_retries} failures")
    return {"messages_found": 0, "attachments": []}


def _record_attachments(db_path: str, attachments: list[dict]) -> tuple[int, list[int]]:
    """Record attachment metadata in the database.

    Uses INSERT OR IGNORE to handle retried batches safely.
    Attempts to link email_id from the emails table if available.

    Returns tuple of (rows_inserted, list_of_inserted_ids).
    """
    if not attachments:
        return 0, []

    conn = get_connection(db_path)
    inserted = 0
    inserted_ids = []
    now = datetime.now().isoformat()

    for att in attachments:
        message_id = att["message_id"]

        # Try to find email_id from emails table
        row = conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()
        email_id = row[0] if row else None

        # Check if this exact attachment already recorded
        existing = conn.execute(
            "SELECT id FROM attachments WHERE message_id = ? AND filename = ?",
            (message_id, att["filename"]),
        ).fetchone()
        if existing:
            continue

        cursor = conn.execute(
            """INSERT INTO attachments
               (email_id, message_id, filename, mime_type, file_size, file_path, is_inline, exported_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                email_id,
                message_id,
                att["filename"],
                att.get("mime_type"),
                att.get("file_size"),
                att["file_path"],
                now,
            ),
        )
        inserted += 1
        inserted_ids.append(cursor.lastrowid)

    conn.commit()
    conn.close()
    return inserted, inserted_ids


def _export_archive_attachments(
    state: dict,
    limit: int,
    base_path: str,
    db_path: str,
) -> tuple[int, int]:
    """Export attachments from Archive using index-based resume.

    Returns (attachments_saved, messages_scanned).
    """
    mailbox = "Archive"
    current_index = state["last_processed_index"] + 1
    total_saved = 0
    total_scanned = 0
    batch_count = 0
    consecutive_empty = 0

    if limit > 0:
        max_index = state["last_processed_index"] + limit
    else:
        max_index = float("inf")

    print(f"\n--- Mailbox: {mailbox} (attachment export) ---")
    print(f"  Resuming from index {current_index}...")

    while current_index <= max_index:
        try:
            # Memory check
            memory_gb = check_mail_memory()
            if memory_gb >= ATT_MEMORY_CRITICAL_GB:
                print(f"  CRITICAL: Mail.app at {memory_gb:.1f}GB, force-restarting...")
                if not _restart_mail():
                    print("  Cannot recover Mail.app, stopping")
                    break
                time.sleep(5)
                continue
            elif memory_gb >= ATT_MEMORY_WARNING_GB:
                print(f"  WARNING: Mail.app at {memory_gb:.1f}GB, pausing 60s...")
                time.sleep(60)

            # Proactive restart every N batches
            if batch_count > 0 and batch_count % ATT_RESTART_INTERVAL == 0:
                print(f"  Proactive restart after {batch_count} batches...")
                _restart_mail()
                time.sleep(5)

            end_index = current_index + ATT_BATCH_SIZE - 1
            if limit > 0:
                end_index = min(end_index, max_index)

            # Save state BEFORE processing (crash-safe)
            state["last_processed_index"] = end_index
            state["total_messages_scanned"] = state.get("total_messages_scanned", 0) + (
                end_index - current_index + 1
            )
            save_state(state)

            print(f"  Batch: messages {current_index}-{end_index}...", end=" ", flush=True)

            result = _process_batch_with_recovery(current_index, end_index, mailbox, base_path)
            messages_found = result.get("messages_found", 0)
            attachments = result.get("attachments", [])

            # End-of-mailbox detection: based on messages found, NOT attachments
            if messages_found == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    print("END (reached end of mailbox)")
                    break
                print("no messages found (probing next batch...)")
            else:
                consecutive_empty = 0
                if not attachments:
                    print(f"{messages_found} messages, no attachments")
                else:
                    # Record in DB
                    inserted, _ = _record_attachments(db_path, attachments)
                    total_saved += inserted
                    total_bytes = sum(a.get("file_size", 0) for a in attachments)
                    state["total_attachments_saved"] = state.get(
                        "total_attachments_saved", 0
                    ) + len(attachments)
                    state["total_bytes_saved"] = state.get("total_bytes_saved", 0) + total_bytes
                    save_state(state)

                    size_str = _format_bytes(total_bytes)
                    print(f"{messages_found} messages, {len(attachments)} saved ({size_str})")

            total_scanned += end_index - current_index + 1
            current_index = end_index + 1
            batch_count += 1

            time.sleep(ATT_BATCH_COOLDOWN)

            if batch_count % ATT_LONG_COOLDOWN_INTERVAL == 0:
                print(f"  Long cooldown ({ATT_LONG_COOLDOWN}s)...")
                time.sleep(ATT_LONG_COOLDOWN)

        except GracefulExit:
            print("\n  Graceful shutdown...")
            raise
        except Exception as e:
            print(f"  ERROR: {e}")
            break

    print(f"  Scanned {total_scanned} messages, saved {total_saved} attachments")
    return total_saved, total_scanned


def _export_mailbox_attachments(
    mailbox: str,
    limit: int,
    base_path: str,
    db_path: str,
) -> tuple[int, int, list[int]]:
    """Export attachments from a volatile mailbox (Inbox, Sent Items).

    Full scan — checks all messages regardless of previous runs.
    Returns (attachments_saved, messages_scanned, new_attachment_ids).
    """
    from src.export.apple_mail import get_message_count

    print(f"\n--- Mailbox: {mailbox} (attachment export, full scan) ---")

    try:
        total_messages = get_message_count(mailbox)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  Could not access mailbox: {e}")
        return 0, 0, []

    if total_messages == 0:
        print("  Empty mailbox, skipping")
        return 0, 0, []

    if limit > 0:
        total_messages = min(total_messages, limit)

    print(f"  Found {total_messages} messages, scanning for attachments...")

    current_index = 1
    total_saved = 0
    all_new_ids = []
    batch_count = 0

    while current_index <= total_messages:
        try:
            memory_gb = check_mail_memory()
            if memory_gb >= ATT_MEMORY_CRITICAL_GB:
                print(f"  CRITICAL: Mail.app at {memory_gb:.1f}GB, force-restarting...")
                if not _restart_mail():
                    break
                time.sleep(5)
                continue

            if batch_count > 0 and batch_count % ATT_RESTART_INTERVAL == 0:
                print(f"  Proactive restart after {batch_count} batches...")
                _restart_mail()
                time.sleep(5)

            end_index = min(current_index + ATT_BATCH_SIZE - 1, total_messages)

            print(f"  Batch: messages {current_index}-{end_index}...", end=" ", flush=True)

            result = _process_batch_with_recovery(current_index, end_index, mailbox, base_path)
            attachments = result.get("attachments", [])

            if not attachments:
                print("no attachments")
            else:
                inserted, new_ids = _record_attachments(db_path, attachments)
                total_saved += inserted
                all_new_ids.extend(new_ids)
                total_bytes = sum(a.get("file_size", 0) for a in attachments)
                size_str = _format_bytes(total_bytes)
                print(f"{inserted} saved ({size_str})")

            current_index = end_index + 1
            batch_count += 1
            time.sleep(ATT_BATCH_COOLDOWN)

            if batch_count % ATT_LONG_COOLDOWN_INTERVAL == 0:
                print(f"  Long cooldown ({ATT_LONG_COOLDOWN}s)...")
                time.sleep(ATT_LONG_COOLDOWN)

        except GracefulExit:
            print("\n  Graceful shutdown...")
            raise
        except Exception as e:
            print(f"  ERROR: {e}")
            break

    scanned = min(total_messages, current_index - 1)
    print(f"  Scanned {scanned} messages, saved {total_saved} attachments")
    return total_saved, scanned, all_new_ids


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}MB"
    else:
        return f"{n / (1024 * 1024 * 1024):.1f}GB"


def export_sync_attachments(db_path: str) -> dict:
    """Export attachments from Inbox and Sent Items for daily sync.

    Scans only volatile mailboxes where new emails arrive — skips Archive
    (handled by initial backfill). Uses existing dedup in _record_attachments
    so rescanning is safe.

    Returns dict with 'saved', 'scanned' counts and 'attachment_ids' list.
    """
    base_path = str(ATTACHMENTS_DIR)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

    total_saved = 0
    total_scanned = 0
    all_new_ids = []

    for mailbox in ["Inbox"]:
        try:
            saved, scanned, new_ids = _export_mailbox_attachments(
                mailbox,
                limit=0,
                base_path=base_path,
                db_path=db_path,
            )
            total_saved += saved
            total_scanned += scanned
            all_new_ids.extend(new_ids)
        except GracefulExit:
            raise
        except Exception as e:
            print(f"  Warning: {mailbox} attachment export failed: {e}")

    return {
        "saved": total_saved,
        "scanned": total_scanned,
        "attachment_ids": all_new_ids,
    }


def export_attachments(limit: int = 0, dry_run: bool = False) -> None:
    """Main entry point — export attachments from all Exchange mailboxes.

    Args:
        limit: Max messages to scan per mailbox (0 = no limit)
        dry_run: If True, report what would be saved without saving
    """
    signal.signal(signal.SIGINT, lambda s, f: (_ for _ in ()).throw(GracefulExit()))
    signal.signal(signal.SIGTERM, lambda s, f: (_ for _ in ()).throw(GracefulExit()))

    base_path = str(ATTACHMENTS_DIR)
    db_path = str(DEFAULT_DB)

    try:
        ensure_mail_ready()
        _acquire_lock()
        print("Attachment export lock acquired")

        # Ensure attachments directory exists
        ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

        state = load_state()
        print(f"Resuming from index {state['last_processed_index']}")
        print(
            f"Previously saved: {state['total_attachments_saved']} attachments "
            f"({_format_bytes(state['total_bytes_saved'])})"
        )

        # Discover mailboxes
        print("Discovering Exchange mailboxes...")
        all_mailboxes = discover_mailboxes()
        print(f"Found {len(all_mailboxes)} mailboxes: {', '.join(all_mailboxes)}")

        start_time = time.time()
        grand_total_saved = 0
        grand_total_scanned = 0

        # Archive first (index-based resume)
        if "Archive" in all_mailboxes:
            try:
                saved, scanned = _export_archive_attachments(state, limit, base_path, db_path)
                grand_total_saved += saved
                grand_total_scanned += scanned
            except GracefulExit:
                elapsed = time.time() - start_time
                print(
                    f"\nExport interrupted. Saved: {grand_total_saved}, Time: {elapsed / 60:.1f}min"
                )
                return

        # Other mailboxes (full scan)
        for mailbox in all_mailboxes:
            if mailbox == "Archive":
                continue
            try:
                saved, scanned = _export_mailbox_attachments(mailbox, limit, base_path, db_path)
                grand_total_saved += saved
                grand_total_scanned += scanned
            except GracefulExit:
                elapsed = time.time() - start_time
                print(
                    f"\nExport interrupted. Saved: {grand_total_saved}, Time: {elapsed / 60:.1f}min"
                )
                return

        elapsed = time.time() - start_time
        print("\nAttachment export summary:")
        print(f"  Mailboxes scanned: {len(all_mailboxes)}")
        print(f"  Messages scanned: {grand_total_scanned}")
        print(f"  Attachments saved: {grand_total_saved}")
        print(
            f"  Total on disk: {state['total_attachments_saved']} attachments "
            f"({_format_bytes(state['total_bytes_saved'])})"
        )
        print(f"  Time: {elapsed / 60:.1f} minutes")

    finally:
        _release_lock()
        print("Attachment export lock released")
