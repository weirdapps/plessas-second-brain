"""
Per-message attachment dispatch.

For each attachment in a message:
  - FileAttachment (incl. inline) → outlook-cli download-attachments
  - ItemAttachment (.eml) → SKIP (per spec decision)
  - ReferenceAttachment → sharepoint_fetcher
"""

import logging
from dataclasses import dataclass, field
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
