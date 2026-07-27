"""
SharePoint reference attachment fetcher.

Wraps `outlook-cli download-sharepoint-link` (added in upstream PR).
Records every attempt in the sharepoint_links table for later inspection
via the sharepoint_index MCP tool.
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from src.export.outlook_cli import OutlookCliError, run_outlook_cli

logger = logging.getLogger(__name__)


def is_managed_sharepoint_host(url: str, managed_host: str) -> bool:
    """Whether ``url`` is on the SharePoint host we hold a session for.

    An "auth-required" result on the managed host means the session expired —
    a re-login (``outlook-cli login --sharepoint-host <host>``) fixes it, so a
    caller is right to stop and prompt for it. The same result on any other
    host is an external tenant we can never authenticate to via our login, and
    should be skipped rather than aborting the whole pass.
    """
    if not managed_host:
        return False
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host == managed_host.strip().lower()


FetchStatus = Literal["ok", "stale", "auth-required", "http-error", "exception"]

# After this many consecutive failed fetch attempts, the retry pass gives up on a
# link (treats it as permanently dead) instead of re-attempting it every night.
MAX_SHAREPOINT_ATTEMPTS = 5


@dataclass
class SharepointFetchResult:
    url: str
    status: FetchStatus
    local_path: Path | None = None
    http_status: int | None = None
    file_name: str | None = None
    file_size: int | None = None
    error_message: str | None = None


def fetch_sharepoint_link(url: str, out_dir: Path) -> SharepointFetchResult:
    """
    Fetch a SharePoint URL via outlook-cli. Returns structured result;
    never raises (callers want to record every attempt).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        raw = run_outlook_cli(["download-sharepoint-link", url, "--out", str(out_dir)])
    except OutlookCliError as err:
        return SharepointFetchResult(url=url, status="exception", error_message=str(err))

    if raw["saved"]:
        s = raw["saved"][0]
        return SharepointFetchResult(
            url=url,
            status="ok",
            local_path=Path(s["path"]),
            file_name=s["name"],
            file_size=s["size"],
        )
    if raw["skipped"]:
        sk = raw["skipped"][0]
        status_map: dict[str, FetchStatus] = {
            "not-found": "stale",
            "auth-required": "auth-required",
            "access-denied": "http-error",
            "http-error": "http-error",
        }
        reason = sk["reason"]
        return SharepointFetchResult(
            url=url,
            status=status_map[reason] if reason in status_map else "http-error",
            http_status=sk.get("status"),
        )
    return SharepointFetchResult(
        url=url,
        status="exception",
        error_message="empty saved+skipped from outlook-cli",
    )


def record_link_in_db(
    conn: sqlite3.Connection,
    url: str,
    message_id: str,
    status: str,
    fetched_path: str | None = None,
    file_name: str | None = None,
    file_size: int | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO sharepoint_links
           (url, message_id, fetched_at, fetched_path, last_status, last_attempt_at,
            file_name, file_size, attempts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET
             fetched_at = excluded.fetched_at,
             fetched_path = excluded.fetched_path,
             last_status = excluded.last_status,
             last_attempt_at = excluded.last_attempt_at,
             file_name = excluded.file_name,
             file_size = excluded.file_size,
             attempts = CASE WHEN excluded.last_status = 'ok'
                             THEN 0 ELSE sharepoint_links.attempts + 1 END""",
        (
            url,
            message_id,
            now if status == "ok" else None,
            fetched_path,
            status,
            now,
            file_name,
            file_size,
            0 if status == "ok" else 1,
        ),
    )
    conn.commit()
