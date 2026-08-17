"""
Per-message attachment dispatch.

For each attachment in a message:
  - FileAttachment (incl. inline) → outlook-cli download-attachments
  - ItemAttachment (.eml) → SKIP (per spec decision)
  - ReferenceAttachment → sharepoint_fetcher
"""

import logging
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.export.outlook_cli import OutlookCliError, run_outlook_cli
from src.export.sharepoint_fetcher import (
    fetch_sharepoint_link,
    record_link_in_db,
)

logger = logging.getLogger(__name__)

FILE_ATTACHMENT = "#Microsoft.OutlookServices.FileAttachment"
ITEM_ATTACHMENT = "#Microsoft.OutlookServices.ItemAttachment"
REFERENCE_ATTACHMENT = "#Microsoft.OutlookServices.ReferenceAttachment"


@dataclass
class AttachmentDispatchResult:
    message_id: str
    saved_files: list[str] = field(default_factory=list)
    skipped_item_attachments: list[str] = field(default_factory=list)
    fetched_sharepoint_files: list[str] = field(default_factory=list)
    failed_sharepoint_urls: list[tuple[str, str]] = field(default_factory=list)  # (url, reason)


def process_message_attachments(
    message: dict,
    base_dir: Path,
    db_conn=None,
) -> AttachmentDispatchResult:
    """
    Process all attachments for a single message.

    base_dir is the directory under which per-message dirs are created.
    db_conn is optional — required only for SharePoint link recording.
    """
    msg_id = message["Id"]
    attachments = message.get("Attachments", [])
    result = AttachmentDispatchResult(message_id=msg_id)

    if not attachments:
        return result

    msg_dir = base_dir / msg_id
    sharepoint_dir = msg_dir / "sharepoint"

    # Bucket by type
    has_file_attach = any(a["@odata.type"] == FILE_ATTACHMENT for a in attachments)
    has_inline = any(
        a["@odata.type"] == FILE_ATTACHMENT and a.get("IsInline", False) for a in attachments
    )

    # Step 1: dispatch all FileAttachments in one CLI call
    if has_file_attach:
        msg_dir.mkdir(parents=True, exist_ok=True)
        args = ["download-attachments", msg_id, "--out", str(msg_dir)]
        if has_inline:
            args.append("--include-inline")
        try:
            raw = run_outlook_cli(args)
            for s in raw.get("saved", []):
                result.saved_files.append(s["path"])
        except OutlookCliError as e:
            logger.error("download-attachments failed for msg %s: %s", msg_id, e)

    # Step 2: process ItemAttachments (skip per spec)
    for a in attachments:
        if a["@odata.type"] == ITEM_ATTACHMENT:
            result.skipped_item_attachments.append(a.get("Name", a["Id"]))

    # Step 3: process ReferenceAttachments
    ref_attachments = [a for a in attachments if a["@odata.type"] == REFERENCE_ATTACHMENT]
    if ref_attachments:
        sharepoint_dir.mkdir(parents=True, exist_ok=True)
    for a in ref_attachments:
        url = a.get("SourceUrl") or a.get("Url") or a.get("Name")
        if not url or not url.startswith(("http://", "https://")):
            logger.warning("ReferenceAttachment %s has no URL, skipping", a.get("Id"))
            continue
        sp_result = fetch_sharepoint_link(url, sharepoint_dir)
        if db_conn:
            record_link_in_db(
                db_conn,
                url=url,
                message_id=msg_id,
                status=sp_result.status,
                fetched_path=str(sp_result.local_path) if sp_result.local_path else None,
                file_name=sp_result.file_name,
                file_size=sp_result.file_size,
            )
        if sp_result.status == "ok" and sp_result.file_name:
            result.fetched_sharepoint_files.append(sp_result.file_name)
        else:
            result.failed_sharepoint_urls.append((url, sp_result.status))

    return result


def register_downloaded_attachments(
    conn, base_dir: Path, limit: int | None = None, now: str | None = None
) -> dict:
    """Record `attachments` rows for files outlook-cli has already downloaded.

    Downloading and recording are separate halves, and only the download half
    was ever built for the outlook-cli path — `download_attachments_for_messages`
    writes binaries to <base_dir>/<message_id>/ and returns. The recording half
    lived exclusively in `export_sync_attachments`, which drives Mail.app over
    AppleScript and is skipped off macOS. So the VPS accumulated 7,387 files
    that no downstream stage could see, because every one of them reads the
    table rather than the disk.

    Idempotent and platform-independent: it diffs the tree against the table, so
    it doubles as the backfill for everything already downloaded. Runs hourly
    against a large tree, hence the single up-front query for what is already
    known rather than a lookup per file.

    `limit` caps rows registered per call. Step 6 of the sync runs Phase 1 and
    Phase 2 unbounded over whatever this makes visible, so releasing a 7k-file
    backlog in one pass would hand an unbounded LLM job to a unit with a
    TimeoutStartSec. Bounded runs let the backlog drain instead; pass None for a
    deliberate one-shot backfill outside the hourly path.
    """
    stamp = now or datetime.now().isoformat()
    base_dir = Path(base_dir)
    stats: dict = {"scanned": 0, "registered": 0, "deferred": 0, "ids": []}

    if not base_dir.is_dir():
        return stats

    # Keyed by (message_id, filename), matching the table's own dedup identity:
    # a message can gain another attachment on a later pass, so skipping whole
    # directories on "message already seen" would miss it permanently.
    known = {
        (mid, name) for mid, name in conn.execute("SELECT message_id, filename FROM attachments")
    }
    emails = dict(conn.execute("SELECT message_id, id FROM emails"))

    for msg_dir in sorted(base_dir.iterdir()):
        if not msg_dir.is_dir():
            continue
        stats["scanned"] += 1
        message_id = msg_dir.name

        files = [
            f
            for f in sorted(msg_dir.iterdir())
            if f.is_file() and (message_id, f.name) not in known
        ]
        if not files:
            continue

        # Attachments are downloaded post-commit, but a batch can still be in
        # flight. Recording email_id NULL would orphan the row for good: the
        # image and text pipelines JOIN emails, so it would never be picked up
        # even once the email lands. Leave it on disk for a later pass instead.
        email_id = emails.get(message_id)
        if email_id is None:
            stats["deferred"] += 1
            continue

        for f in files:
            if limit is not None and stats["registered"] >= limit:
                conn.commit()
                return stats
            cur = conn.execute(
                """INSERT INTO attachments
                   (email_id, message_id, filename, mime_type, file_size, file_path,
                    is_inline, exported_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (
                    email_id,
                    message_id,
                    f.name,
                    mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                    f.stat().st_size,
                    str(f),
                    stamp,
                ),
            )
            stats["registered"] += 1
            stats["ids"].append(cur.lastrowid)

    conn.commit()
    return stats
