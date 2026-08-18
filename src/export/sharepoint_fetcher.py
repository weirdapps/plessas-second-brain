"""
SharePoint reference attachment fetcher.

Wraps `sharepoint-cli get`. Records every attempt in the sharepoint_links
table for later inspection via the sharepoint_index MCP tool.

Migrated 2026-08-08 from `outlook-cli download-sharepoint-link`, which was
removed when SharePoint moved into its own repo (~/SourceCode/sharepoint-access).
Mail, attachments and calendar still go through outlook-cli; only SharePoint
moved. The FetchStatus contract below is unchanged, so callers, the
sharepoint_links table and the sharepoint_index MCP tool are unaffected.
"""

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from src.export.sharepoint_cli import (
    SharepointCliAuthRequired,
    SharepointCliError,
    host_for_url,
    parse_error_payload,
    run_sharepoint_cli,
)

logger = logging.getLogger(__name__)


def is_managed_sharepoint_host(url: str, managed_host: str) -> bool:
    """Whether ``url`` is on the SharePoint host we hold a session for.

    An "auth-required" result on the managed host means the session expired —
    a re-login (``sharepoint-cli login --host <host>``) fixes it, so a caller
    is right to stop and prompt for it. The same result on any other host is an
    external tenant we can never authenticate to via our login, and should be
    skipped rather than aborting the whole pass.
    """
    if not managed_host:
        return False
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host == managed_host.strip().lower()


FetchStatus = Literal["ok", "stale", "auth-required", "http-error", "exception"]

# After this many consecutive failed fetch attempts, the retry pass stops trying
# a link every night. It is a throttle, not an abandonment — see the cool-off.
MAX_SHAREPOINT_ATTEMPTS = 5

# ...and after this long, a capped link is offered once more. Without it the cap
# is permanent: between 2026-07-30 and 2026-08-10 this tenant's SharePoint auth
# was broken (MCAS-gated, no bearer issued), so 43 links spent their attempts on
# a fetcher that could not have succeeded and were never retried — still
# unfetched nine days after the auth was fixed. Any outage lasting more than
# five nightly runs would otherwise mean permanent loss, and the retry is cheap
# because only exhausted links qualify.
SHAREPOINT_RETRY_COOL_OFF_DAYS = 7


def retry_candidates(conn, now: str | None = None) -> list:
    """Links seen before but never fetched OK, that are due for another attempt.

    Returns (url, message_id) rows. `attempts` throttles: past the cap a link is
    rested rather than dropped, and offered again once the cool-off expires.
    'unsupported-host' is a permanent external tenant and stays excluded.
    """
    from datetime import UTC, datetime, timedelta

    stamp = now or datetime.now(UTC).isoformat()
    cutoff = (
        datetime.fromisoformat(stamp) - timedelta(days=SHAREPOINT_RETRY_COOL_OFF_DAYS)
    ).isoformat()
    return conn.execute(
        "SELECT url, message_id FROM sharepoint_links "
        "WHERE (fetched_at IS NULL OR last_status = 'stale') "
        "AND COALESCE(last_status, '') != 'unsupported-host' "
        "AND (attempts < ? OR COALESCE(last_attempt_at, '') < ?)",
        (MAX_SHAREPOINT_ATTEMPTS, cutoff),
    ).fetchall()


@dataclass
class SharepointFetchResult:
    url: str
    status: FetchStatus
    local_path: Path | None = None
    http_status: int | None = None
    file_name: str | None = None
    file_size: int | None = None
    error_message: str | None = None


# sharepoint-cli error codes -> the FetchStatus vocabulary this module has
# always exposed. Kept as data so the mapping is auditable at a glance.
_ERROR_STATUS: dict[str, FetchStatus] = {
    "not_found": "stale",
    "auth_required": "auth-required",
    "access_denied": "http-error",
    "locked": "http-error",
    "quota_exceeded": "http-error",
    "upstream": "http-error",
    "timeout": "http-error",
}


def _name_from_url(url: str) -> str:
    """Last path segment, percent-decoded. Fallback when the server sends no
    Content-Disposition."""
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(tail) or "download.bin"


def fetch_sharepoint_link(url: str, out_dir: Path) -> SharepointFetchResult:
    """
    Fetch a SharePoint URL via sharepoint-cli. Returns a structured result;
    never raises (callers want to record every attempt).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    host = host_for_url(url)
    if not host:
        return SharepointFetchResult(
            url=url, status="exception", error_message=f"cannot derive host from URL: {url}"
        )

    # sharepoint-cli writes to a FILE path, while this function's contract is
    # "save into out_dir under the server's name". Fetch to a temp file, then
    # rename once the server has told us the name.
    tmp_fd = tempfile.NamedTemporaryFile(dir=out_dir, prefix=".sp-", delete=False)
    tmp_path = Path(tmp_fd.name)
    tmp_fd.close()

    try:
        raw = run_sharepoint_cli(["get", url, "--out", str(tmp_path)], host=host)
    except SharepointCliAuthRequired as err:
        tmp_path.unlink(missing_ok=True)
        return SharepointFetchResult(url=url, status="auth-required", error_message=str(err))
    except SharepointCliError as err:
        tmp_path.unlink(missing_ok=True)
        payload = parse_error_payload(err.stderr)
        code = str(payload.get("error", ""))
        status = _ERROR_STATUS.get(code, "exception" if not code else "http-error")
        return SharepointFetchResult(
            url=url,
            status=status,
            http_status=payload.get("status"),
            error_message=str(err),
        )
    except Exception as err:  # subprocess timeout, OSError, malformed JSON
        tmp_path.unlink(missing_ok=True)
        return SharepointFetchResult(url=url, status="exception", error_message=str(err))

    file_name = raw.get("filename") or _name_from_url(url)
    final_path = out_dir / Path(file_name).name  # basename: never escape out_dir
    try:
        shutil.move(str(tmp_path), str(final_path))
    except OSError as err:
        tmp_path.unlink(missing_ok=True)
        return SharepointFetchResult(url=url, status="exception", error_message=str(err))

    return SharepointFetchResult(
        url=url,
        status="ok",
        local_path=final_path,
        file_name=final_path.name,
        file_size=raw.get("size") or final_path.stat().st_size,
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
