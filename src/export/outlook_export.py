"""
Hourly Outlook ingestion entry point.

Replaces apple_mail.py for the live ingestion path. Forward-only —
historical data stays in the existing DB untouched.
"""

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.export.outlook_cli import (
    OutlookCliAuthRequired,
    OutlookCliError,
    run_outlook_cli,
)
from src.export.state import (
    OutlookSyncState,
    load_outlook_sync_state,
    save_outlook_sync_state,
)

logger = logging.getLogger(__name__)

DEFAULT_SELECT_FIELDS = (
    "Id,Subject,From,ToRecipients,CcRecipients,ReceivedDateTime,"
    "HasAttachments,IsRead,WebLink,ConversationId,InternetMessageId"
)


@dataclass
class MessageSummary:
    id: str
    subject: str
    received_at: str
    conversation_id: str | None
    has_attachments: bool


def parse_received_dt(iso: str) -> datetime:
    """Outlook returns ISO timestamps with Z suffix. Normalize for comparison."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def list_new_messages(
    state: OutlookSyncState,
    folder: str = "Inbox",
    max_results: int = 10000,
    select: str = DEFAULT_SELECT_FIELDS,
    allow_bootstrap: bool = False,
) -> list[dict]:
    """
    Fetch all message metadata received since the cursor in `state`.

    Bootstrap (no cursor) is gated by `allow_bootstrap` because silent partial
    sync is a footgun: a missing/corrupt state file would otherwise silently
    discard everything older than the most-recent 100 messages. Caller must
    explicitly opt in.
    """
    args = ["list-mail", "--folder", folder, "--select", select]
    if state.last_seen_received_at:
        args.extend(["--since", state.last_seen_received_at, "--all", "--max", str(max_results)])
    else:
        if not allow_bootstrap:
            raise BootstrapRequired(
                f"No sync cursor for folder={folder!r} and --bootstrap not passed. "
                "Refusing to silently fetch only the most-recent 100 messages."
            )
        args.extend(["--top", "100"])
        logger.warning(
            "Bootstrapping folder=%s: fetching top 100 most recent. "
            "All older messages are intentionally outside this sync; backfill separately if needed.",
            folder,
        )
    return run_outlook_cli(args)


class BootstrapRequired(RuntimeError):
    """Raised when no cursor exists and --bootstrap was not passed."""


class TruncatedRunWarning(RuntimeError):
    """Raised when a run hits max_results — silent advance would lose data."""


def get_one_message_body(message_id: str, body_mode: str = "html") -> dict | None:
    """Fetch a single message's full body via outlook-cli get-mail.

    Returns None (logged) when the response can't be parsed or is missing the
    fields the sync relies on. A single malformed message must not crash the
    whole run — otherwise it wedges the cursor and the mailbox goes stale.
    OutlookCliAuthRequired is left to propagate (the batch aborts on auth loss).
    """
    try:
        msg = run_outlook_cli(["get-mail", message_id, "--body", body_mode])
    except ValueError as e:  # json.JSONDecodeError is a ValueError subclass
        logger.warning("get-mail unparseable for %s — skipping: %s", message_id, e)
        return None
    if not isinstance(msg, dict) or not msg.get("Id") or not msg.get("ReceivedDateTime"):
        keys = list(msg)[:6] if isinstance(msg, dict) else type(msg).__name__
        logger.warning(
            "get-mail returned incomplete message for %s — skipping (got=%s)",
            message_id,
            keys,
        )
        return None
    return msg


def fetch_bodies_concurrent(
    message_ids: list[str],
    concurrency: int = 5,
    body_mode: str = "html",
) -> Iterator[dict | None]:
    """
    Fetch full bodies for a batch of message Ids in parallel.

    Yields each message dict as it becomes available. On
    OutlookCliAuthRequired, aborts the whole batch (token expired
    mid-stream — caller should restart on next sync interval).
    """
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(get_one_message_body, mid, body_mode): mid for mid in message_ids}
        try:
            for fut in as_completed(futures):
                yield fut.result()
        except OutlookCliAuthRequired:
            pool.shutdown(wait=False, cancel_futures=True)
            raise


def _outlook_to_staging_email(msg: dict, folder: str) -> dict:
    """
    Transform an Outlook v2.0 REST Message into the staging-batch email
    shape that the existing LLM extractor + loader read. The shape is:
    {
        message_id, date_received, subject, sender,
        to_recipients, cc_recipients, mailbox_name, content
    }
    where sender/to_recipients/cc_recipients are dicts with name/address.
    """
    from_field = msg.get("From", {}).get("EmailAddress", {})
    sender = {
        "name": from_field.get("Name", ""),
        "address": from_field.get("Address", ""),
    }
    to_recipients = [
        {
            "name": r.get("EmailAddress", {}).get("Name", ""),
            "address": r.get("EmailAddress", {}).get("Address", ""),
        }
        for r in msg.get("ToRecipients", []) or []
    ]
    cc_recipients = [
        {
            "name": r.get("EmailAddress", {}).get("Name", ""),
            "address": r.get("EmailAddress", {}).get("Address", ""),
        }
        for r in msg.get("CcRecipients", []) or []
    ]
    body = msg.get("Body", {}) or {}
    return {
        "message_id": msg.get("Id", ""),
        "date_received": msg.get("ReceivedDateTime", ""),
        "subject": msg.get("Subject", ""),
        "sender": sender,
        "to_recipients": to_recipients,
        "cc_recipients": cc_recipients,
        "mailbox_name": folder,
        "content": body.get("Content", ""),
        "conversation_id": msg.get("ConversationId"),
        "internet_message_id": msg.get("InternetMessageId"),
        "web_link": msg.get("WebLink"),
        "has_attachments": msg.get("HasAttachments", False),
    }


def _next_outlook_batch_number(staging_dir: Path) -> int:
    """
    Pick the next batch-NNNNN number, starting at 50000 to leave a wide
    margin above any apple_mail batches (which are <10000 in practice).
    """
    OUTLOOK_BATCH_FLOOR = 50000
    existing = [p.name for p in staging_dir.glob("batch-*.json")]
    max_existing = OUTLOOK_BATCH_FLOOR - 1
    for name in existing:
        try:
            n = int(name.removeprefix("batch-").removesuffix(".json"))
            if n >= OUTLOOK_BATCH_FLOOR and n > max_existing:
                max_existing = n
        except ValueError:
            continue
    return max_existing + 1


def commit_messages_to_db(messages: list[dict], folder: str = "Inbox") -> Path:
    """
    Write messages as a staging batch the existing LLM extractor + loader
    will pick up via its sorted glob over data/staging/batch-*.json.

    Outlook fields are mapped to the apple_mail staging shape so the
    downstream pipeline doesn't need to learn a new format.
    """
    import json

    staging_dir = Path(__file__).parent.parent.parent / "data" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    batch_number = _next_outlook_batch_number(staging_dir)
    batch_file = staging_dir / f"batch-{batch_number:05d}.json"

    batch_data = {
        "batch_number": batch_number,
        "exported_at": datetime.now(UTC).isoformat(),
        "source": "outlook-cli",
        "folder": folder,
        "emails": [_outlook_to_staging_email(m, folder) for m in messages],
    }
    with open(batch_file, "w", encoding="utf-8") as f:
        json.dump(batch_data, f, indent=2, ensure_ascii=False)
    logger.info("Wrote %d messages to %s", len(messages), batch_file)
    return batch_file


def download_attachments_for_messages(
    messages: list[dict],
    base_dir: Path | None = None,
    include_inline: bool = True,
) -> dict:
    """
    For each message with HasAttachments=True, download the file payloads
    via `outlook-cli download-attachments` into base_dir/<message_id>/.

    Returns a summary dict: {scanned, downloaded_messages, failed_messages}.
    Failures are logged but do NOT raise — attachment availability is best-
    effort, not part of the cursor-advance contract.
    """
    if base_dir is None:
        base_dir = Path(__file__).parent.parent.parent / "data" / "attachments"
    base_dir.mkdir(parents=True, exist_ok=True)

    targets = [m for m in messages if m.get("HasAttachments")]
    downloaded = 0
    failed = 0
    for msg in targets:
        msg_id = msg.get("Id")
        if not msg_id:
            continue
        msg_dir = base_dir / msg_id
        msg_dir.mkdir(parents=True, exist_ok=True)
        args = ["download-attachments", msg_id, "--out", str(msg_dir)]
        if include_inline:
            args.append("--include-inline")
        try:
            run_outlook_cli(args)
            downloaded += 1
        except OutlookCliAuthRequired:
            # Bubble up — caller decides whether to abort the whole sync.
            raise
        except OutlookCliError as e:
            logger.warning("download-attachments failed for %s: %s", msg_id, e)
            failed += 1
    return {
        "scanned": len(targets),
        "downloaded_messages": downloaded,
        "failed_messages": failed,
    }


def run_hourly_sync(
    state_path: Path,
    folder: str = "Inbox",
    max_results: int = 10000,
    concurrency: int = 5,
    fetch_attachments: bool = True,
    allow_bootstrap: bool = False,
) -> dict:
    """
    Top-level entry. Idempotent: state cursor only advances after DB commit.
    Returns a summary dict for logging.
    """
    state = load_outlook_sync_state(state_path)
    state.last_sync_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    try:
        # 1. Pre-flight auth check
        run_outlook_cli(["auth-check"])

        # 2. List new metadata
        summaries = list_new_messages(
            state,
            folder=folder,
            max_results=max_results,
            allow_bootstrap=allow_bootstrap,
        )

        # 2b. Saturation check — if we hit the cap, the cursor would skip past
        # any messages we didn't fetch. Refuse to advance and let the user
        # rerun with a bigger cap or a backfill strategy.
        if len(summaries) >= max_results:
            raise TruncatedRunWarning(
                f"folder={folder!r}: list-mail returned exactly --max ({max_results}) messages. "
                "Cannot safely advance cursor — older messages would be silently lost. "
                "Re-run with --max larger than the actual backlog, or backfill explicitly."
            )
        if not summaries:
            state.last_sync_completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            state.messages_in_last_run = 0
            state.consecutive_failures = 0
            save_outlook_sync_state(state_path, state)
            return {"messages": 0, "status": "ok"}

        # 3. Fetch full bodies concurrently. Malformed/incomplete responses come
        # back as None (logged) and are dropped — one bad message must not crash
        # the run, or the cursor wedges and the whole mailbox goes stale.
        ids = [m["Id"] for m in summaries]
        full_messages = [
            m for m in fetch_bodies_concurrent(ids, concurrency=concurrency) if m is not None
        ]
        skipped = len(ids) - len(full_messages)
        if skipped:
            logger.warning(
                "%s: skipped %d/%d messages with unparseable get-mail output",
                folder,
                skipped,
                len(ids),
            )

        # 4. Commit to DB (staging). Skip when everything in the window was dropped.
        if full_messages:
            commit_messages_to_db(full_messages, folder=folder)

        # 4b. Download attachments for messages that have them.
        # This is post-commit because attachment availability is best-effort —
        # if it fails, we still want the bodies in staging. Cursor still advances
        # in step 5; missed attachments can be backfilled by message_id later.
        attachment_summary = {
            "scanned": 0,
            "downloaded_messages": 0,
            "failed_messages": 0,
        }
        if fetch_attachments and full_messages:
            try:
                attachment_summary = download_attachments_for_messages(full_messages)
            except OutlookCliAuthRequired:
                # Auth died mid-attachment-fetch. Cursor hasn't advanced yet,
                # so re-raise and let the caller record the failure. We accept
                # that the bodies in staging will be re-extracted on retry.
                raise

        # 5. Update state cursor — advance over the full SEEN window (summaries
        # always carry ReceivedDateTime), not just the bodies we fetched, so a
        # skipped/malformed message can't wedge the cursor and stall the mailbox.
        latest_received = max(parse_received_dt(s["ReceivedDateTime"]) for s in summaries)
        latest_id = next(
            s["Id"]
            for s in summaries
            if parse_received_dt(s["ReceivedDateTime"]) == latest_received
        )
        state.last_seen_received_at = latest_received.isoformat().replace("+00:00", "Z")
        state.last_seen_message_id = latest_id
        state.messages_in_last_run = len(full_messages)
        state.last_sync_completed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        state.consecutive_failures = 0
        save_outlook_sync_state(state_path, state)
        return {
            "messages": len(full_messages),
            "attachments": attachment_summary,
            "status": "ok",
        }

    except OutlookCliAuthRequired:
        state.consecutive_failures += 1
        save_outlook_sync_state(state_path, state)
        raise
    except (OSError, OutlookCliError, RuntimeError):
        state.consecutive_failures += 1
        save_outlook_sync_state(state_path, state)
        raise


def _default_state_path() -> Path:
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "data" / "state" / "outlook_sync.json"


def main() -> int:
    """CLI entry point for `python -m src.export.outlook_export`."""
    import argparse

    parser = argparse.ArgumentParser(description="Outlook ingestion sync")
    parser.add_argument("--mode", choices=["hourly", "attachments-only"], default="hourly")
    parser.add_argument(
        "--state-path", type=Path, default=None, help="Override sync state file path"
    )
    parser.add_argument("--folder", default="Inbox")
    parser.add_argument("--max-results", type=int, default=10000)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Allow first-run bootstrap (top 100 most recent) when no cursor "
        "exists. Without this flag, missing-cursor errors fast.",
    )
    args = parser.parse_args()

    state_path = args.state_path or _default_state_path()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.mode == "hourly":
        try:
            result = run_hourly_sync(
                state_path=state_path,
                folder=args.folder,
                max_results=args.max_results,
                concurrency=args.concurrency,
                allow_bootstrap=args.bootstrap,
            )
            logger.info("Sync complete: %s", result)
            return 0
        except OutlookCliAuthRequired:
            logger.error(
                "Auth required — run `outlook-cli login` and clear ~/.second-brain/needs_reauth"
            )
            return 4
        except OutlookCliError as e:
            logger.error("outlook-cli error (exit %d): %s", e.exit_code, e.stderr)
            return 5
        except BootstrapRequired as e:
            logger.error("Bootstrap required: %s", e)
            return 7
        except TruncatedRunWarning as e:
            logger.error("Truncated run, refusing to advance cursor: %s", e)
            return 8
        except Exception:
            logger.exception("Sync failed with unexpected error")
            return 1

    # attachments-only mode is a placeholder; the nightly attachment-pass
    # script can be extended later to implement a separate pass.
    logger.warning(
        "attachments-only mode not yet implemented; nightly attachment "
        "fetch is folded into the hourly sync's get-mail step."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
