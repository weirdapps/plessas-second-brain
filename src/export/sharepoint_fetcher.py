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
import re
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

_BARE_HOST = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")


def managed_sharepoint_hosts(managed_host: str) -> frozenset[str]:
    """The tenant host we hold a session for, plus its OneDrive twin.

    OneDrive for Business lives on the tenant's "-my" twin host (for example
    ``contoso-my`` beside ``contoso``) and is opened with the same session, so
    either spelling of the setting yields both.
    """
    host = (managed_host or "").strip().lower()
    # A bare host only. Anything else (an inline comment, a scheme, a path)
    # would become a tenant no URL can ever match, and every real link would be
    # refused and parked; an empty set makes the caller refuse to run instead.
    if not _BARE_HOST.fullmatch(host):
        return frozenset()
    label, _dot, rest = host.partition(".")
    base = label.removesuffix("-my")
    return frozenset({f"{base}.{rest}", f"{base}-my.{rest}"})


def is_managed_sharepoint_host(url: str, managed_host: str) -> bool:
    """Whether ``url`` is on the SharePoint tenant we hold a session for.

    An "auth-required" result on the managed host means the session expired —
    a re-login (``sharepoint-cli login --host <host>``) fixes it, so a caller
    is right to stop and prompt for it. The same result on any other host is an
    external tenant we can never authenticate to via our login, and should be
    skipped rather than aborting the whole pass.

    The netloc must be the bare host: a userinfo part (``host@evil``) or a port
    is refused rather than parsed around, since the netloc is what reaches
    sharepoint-cli's ``--host``.
    """
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return netloc in managed_sharepoint_hosts(managed_host)


FetchStatus = Literal[
    "ok", "not-content", "stale", "auth-required", "http-error", "exception", "unsupported-host"
]

# The statuses that settle a link: its content was read, or it has none (a home page, a
# OneDrive view, a folder). Neither is offered for retry.
_DONE = frozenset({"ok", "not-content"})

LinkKind = Literal["file", "page", "not-content"]

# The shapes of sharepoint-access src/sharepoint/links.ts classifyLink, which get and page rely on.
_FILE_LETTERS = frozenset("wxpbto")
_SHARING = re.compile(r"^/:([a-z]):/([a-z])(/.*)?$", re.IGNORECASE)
_PAGE = re.compile(r"/SitePages/[^/]+\.aspx$", re.IGNORECASE)
_VIEWER = re.compile(r"/_layouts/15/(Doc|WopiFrame2?|xlviewer|PowerPoint)\.aspx$", re.IGNORECASE)
_DOCUMENT = re.compile(
    r"\.(docx?|docm|dotx|xlsx?|xlsm|xlsb|pptx?|pptm|ppsx|pdf|txt|csv|md|rtf|odt|ods|odp|msg|eml|zip)$",
    re.IGNORECASE,
)


def link_kind(url: str) -> LinkKind:
    """What a link points at: a file, an intranet page, or nothing to read.

    Nothing to read is most of what mail links to besides pages: the SharePoint home, OneDrive
    and library views, notification settings, folders (/:f:), videos (/:v:), site roots.
    """
    try:
        path = unquote(urlparse(url).path)
    except ValueError:
        return "not-content"
    m = _SHARING.match(path)
    if m:
        letter, form, rest = m.group(1).lower(), m.group(2).lower(), m.group(3) or ""
        if letter == "u" and form == "r" and _PAGE.search(rest):
            return "page"
        return "file" if letter in _FILE_LETTERS else "not-content"
    if _VIEWER.search(path):
        return "file"
    if _PAGE.search(path):
        return "page"
    return "file" if _DOCUMENT.search(path) else "not-content"


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


def retry_candidates(conn, now: str | None = None, managed_host: str = "") -> list:
    """Links seen before but never fetched OK, that are due for another attempt.

    Returns (url, message_id) rows. `attempts` throttles: past the cap a link is
    rested rather than dropped, and offered again once the cool-off expires.
    'unsupported-host' is a permanent external tenant and stays excluded, except
    on our own tenant, which is never unsupported: while SHAREPOINT_HOST sat at
    its placeholder on the producer, session expiries there were parked under
    that status (5 links on 2026-09-03, 17 on the OneDrive twin), and this is
    what takes them back without a one-off repair.

    One class of link is never resurrected: a 404 that has spent its whole
    attempt budget without ever fetching OK. sharepoint-cli maps not_found onto
    'stale' (_ERROR_STATUS), and a link that has NEVER succeeded cannot be
    "stale" in the re-fetch sense, so never-fetched + capped + 'stale' is a
    document that no longer exists. Prod carries 23 of them; without this they
    re-enter the pool every cool-off, burn an attempt, 404 again and reset the
    clock, forever. The gate is deliberately narrow — a 404 still gets its full
    five attempts first (moved or briefly unshared files come back), and a link
    that once fetched OK keeps re-fetching however often it has failed since.
    """
    from datetime import UTC, datetime, timedelta

    stamp = now or datetime.now(UTC).isoformat()
    cutoff = (
        datetime.fromisoformat(stamp) - timedelta(days=SHAREPOINT_RETRY_COOL_OFF_DAYS)
    ).isoformat()
    # Always two slots (host and OneDrive twin), so the statement stays a
    # constant; an empty pattern matches no URL when no tenant is configured.
    own = [f"https://{h}/%" for h in sorted(managed_sharepoint_hosts(managed_host))] + ["", ""]
    return conn.execute(
        "SELECT url, message_id FROM sharepoint_links "
        "WHERE (fetched_at IS NULL OR last_status = 'stale') "
        "AND (COALESCE(last_status, '') != 'unsupported-host' OR url LIKE ? OR url LIKE ?) "
        "AND (attempts < ? OR (COALESCE(last_attempt_at, '') < ? "
        "                      AND NOT (fetched_at IS NULL "
        "                               AND COALESCE(last_status, '') = 'stale')))",
        (own[0], own[1], MAX_SHAREPOINT_ATTEMPTS, cutoff),
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
    # The link led to a web page, not a file: settled, like a link that is not content at all.
    "not_a_file": "not-content",
    "not_found": "stale",
    "auth_required": "auth-required",
    "access_denied": "http-error",
    "locked": "http-error",
    "quota_exceeded": "http-error",
    "upstream": "http-error",
    "timeout": "http-error",
}


@dataclass
class SharepointPageResult:
    url: str
    status: FetchStatus
    path: str | None = None
    title: str | None = None
    html: str | None = None
    http_status: int | None = None
    error_message: str | None = None


def _host_or_refusal(
    url: str, managed_host: str | None
) -> tuple[str, FetchStatus | None, str | None]:
    """The URL's host when it is ours to fetch; else the status and message refusing it."""
    if managed_host is None:
        from src import config

        managed_host = config.SHAREPOINT_HOST
    try:
        host = host_for_url(url)
    except ValueError as err:  # urlparse rejects e.g. an unclosed "[": never raise
        return "", "exception", str(err)
    if not host:
        return "", "exception", f"cannot derive host from URL: {url}"
    if not is_managed_sharepoint_host(url, managed_host):
        return host, "unsupported-host", f"{host} is not the managed SharePoint tenant; not fetched"
    return host, None, None


def _error_status(err: SharepointCliError) -> tuple[FetchStatus, int | None]:
    payload = parse_error_payload(err.stderr)
    code = str(payload.get("error", ""))
    return _ERROR_STATUS.get(code, "exception" if not code else "http-error"), payload.get("status")


def fetch_sharepoint_page(url: str, managed_host: str | None = None) -> SharepointPageResult:
    """An intranet page's title and HTML, via `sharepoint-cli page`. Never raises.

    Gated on our own tenant exactly as a file fetch is (fetch_sharepoint_link).
    """
    host, refused, message = _host_or_refusal(url, managed_host)
    if refused:
        return SharepointPageResult(url=url, status=refused, error_message=message)
    try:
        raw = run_sharepoint_cli(["page", url], host=host)
        return SharepointPageResult(
            url=url,
            status="ok",
            path=raw.get("path"),
            title=raw.get("title"),
            html=raw.get("html") or "",
        )
    except SharepointCliAuthRequired as err:
        return SharepointPageResult(url=url, status="auth-required", error_message=str(err))
    except SharepointCliError as err:
        status, http_status = _error_status(err)
        return SharepointPageResult(
            url=url, status=status, http_status=http_status, error_message=str(err)
        )
    except Exception as err:  # subprocess timeout, OSError, malformed JSON
        return SharepointPageResult(url=url, status="exception", error_message=str(err))


def _name_from_url(url: str) -> str:
    """Last path segment, percent-decoded. Fallback when the server sends no
    Content-Disposition."""
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(tail) or "download.bin"


def fetch_sharepoint_link(
    url: str, out_dir: Path, managed_host: str | None = None
) -> SharepointFetchResult:
    """
    Fetch a SharePoint URL via sharepoint-cli. Returns a structured result;
    never raises (callers want to record every attempt).

    Only our own tenant is ever fetched. sharepoint-cli retargets the stored
    session at whatever ``--host`` it is given and attaches its cookies, and the
    URLs reaching here come from arbitrary email bodies, so any other host gets
    'unsupported-host' with no subprocess at all. ``managed_host`` defaults to
    config.SHAREPOINT_HOST, read at call time.
    """
    host, refused, message = _host_or_refusal(url, managed_host)
    if refused:
        return SharepointFetchResult(url=url, status=refused, error_message=message)
    out_dir.mkdir(parents=True, exist_ok=True)

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
        status, http_status = _error_status(err)
        return SharepointFetchResult(
            url=url, status=status, http_status=http_status, error_message=str(err)
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
    document_message_id: int | None = None,
) -> None:
    """Record one fetch attempt. `document_message_id` names the text-only document the fetched
    file became (src/extract/sharepoint_ingest.py); a later attempt that stores none keeps it.
    'not-content' settles a link as 'ok' does: fetched_at is set and the attempts reset."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO sharepoint_links
           (url, message_id, fetched_at, fetched_path, last_status, last_attempt_at,
            file_name, file_size, attempts, document_message_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET
             fetched_at = excluded.fetched_at,
             fetched_path = excluded.fetched_path,
             last_status = excluded.last_status,
             last_attempt_at = excluded.last_attempt_at,
             file_name = excluded.file_name,
             file_size = excluded.file_size,
             attempts = CASE WHEN excluded.last_status IN ('ok', 'not-content')
                             THEN 0 ELSE sharepoint_links.attempts + 1 END,
             document_message_id = COALESCE(excluded.document_message_id,
                                            sharepoint_links.document_message_id)""",
        (
            url,
            message_id,
            now if status in _DONE else None,
            fetched_path,
            status,
            now,
            file_name,
            file_size,
            0 if status in _DONE else 1,
            document_message_id,
        ),
    )
    conn.commit()
