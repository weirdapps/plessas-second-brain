"""Undo what old faults left in sharepoint_links, and list what our tenant keeps refusing.

Mangled links. The link scan once kept HTML entities in URLs ("...-&amp;-Markets.aspx",
"...aspx&quot", "?amp%3Bat=...") and, until 2026-10-07, cut the Safe Links copy of a URL at
its apostrophe ("...Sales-Rally-Q2-"). No email holds those URLs. They 404 until the attempt
cap retires them, or read as no content (no ".aspx") and settle at once with nothing held. A
link that holds no document, whose email the fixed scan reads again without yielding it, was
made by the scanner: it is dropped, and what the email does link to is recorded in its place.

Unread pages. A page link recorded 'ok' whose document is not the page's own text, none at
all or the script shell an older fetch saved as a file, is offered again.

Neither repair fetches anything: the retry pass reads what they queue (`brain
process-sharepoint`, or the nightly run). With apply False both only count.
"""

import sqlite3
from urllib.parse import parse_qs, unquote, urlparse

from src.export.sharepoint_fetcher import link_kind, record_link_in_db, target_of
from src.extract.sharepoint_url_scanner import extract_sharepoint_urls
from src.store.email_html import markup_or_text

# A link recorded by the rescan and not fetched yet. It is unfetched work like any other:
# retry_candidates offers it, and the health check counts it among the unfetched.
QUEUED = "queued"

# Where ingest_page (src/extract/sharepoint_ingest.py) files a page's text:
# ingest_text_document's "text:<source>:<key>" with source "sharepoint-page".
PAGE_DOCUMENT_PREFIX = "text:sharepoint-page:"

# The query parameters that name the file behind a viewer or guest link. Any other parameter
# (Safe Links' xsdata and sdata, "at", "wdLOR") varies per email for the same document.
_DOCUMENT_PARAMS = ("sourcedoc", "share", "docid", "id", "file")


def _urls_in_email(conn: sqlite3.Connection, message_id) -> set[str] | None:
    """What the scan finds in an email now, or None when the email is not held."""
    rows = conn.execute(
        "SELECT e.content, h.html FROM emails e LEFT JOIN email_html h ON h.email_id = e.id"
        " WHERE e.message_id = ?",
        (message_id,),
    ).fetchall()
    if not rows:
        return None
    urls: set[str] = set()
    for content, blob in rows:
        urls.update(extract_sharepoint_urls(markup_or_text(content, blob)))
    return urls


def rescan_links(conn: sqlite3.Connection, *, apply: bool) -> dict:
    """Drop the links their email no longer yields; record what it does yield.

    Only links that hold no document are candidates, and of those only the ones never
    fetched or settled as no content: a link that once fetched was not mangled, whatever
    its spelling. A link whose email is gone is kept, since nothing can tell a mangled one
    from a real one without it. A recorded file or page link is queued for the retry pass;
    one with nothing to read is settled as the pass would settle it, without a fetch.
    """
    candidates = conn.execute(
        "SELECT url, message_id FROM sharepoint_links WHERE document_message_id IS NULL"
        " AND (fetched_at IS NULL OR last_status = 'not-content')"
    ).fetchall()
    present = {url for (url,) in conn.execute("SELECT url FROM sharepoint_links")}
    by_email: dict[str, list[str]] = {}
    for url, message_id in candidates:
        by_email.setdefault(message_id, []).append(url)
    dropped: list[str] = []
    added: dict[str, str] = {}
    for message_id, urls in by_email.items():
        yielded = _urls_in_email(conn, message_id)
        if yielded is None:
            continue
        dropped += [url for url in urls if url not in yielded]
        for url in sorted(yielded - present):
            added.setdefault(url, message_id)
    if apply:
        conn.executemany("DELETE FROM sharepoint_links WHERE url = ?", [(u,) for u in dropped])
        for url, message_id in added.items():
            if link_kind(url) == "not-content":
                record_link_in_db(conn, url=url, message_id=message_id, status="not-content")
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO sharepoint_links (url, message_id, last_status, attempts)"
                    " VALUES (?, ?, ?, 0)",
                    (url, message_id, QUEUED),
                )
        conn.commit()
    return {"emails": len(by_email), "dropped": len(dropped), "added": len(added)}


def reread_pages(conn: sqlite3.Connection, *, apply: bool) -> dict:
    """Offer again the page links recorded 'ok' whose document is not the page's text.

    The retry pass reads them through `sharepoint-cli page`, once per page. It is the same
    reset the backlog pass gives a link whose file was lost: fetched_at cleared, attempts 0.
    """
    rows = conn.execute(
        "SELECT l.url, (SELECT a.file_path FROM attachments a"
        "  WHERE a.message_id = l.document_message_id LIMIT 1)"
        " FROM sharepoint_links l WHERE l.last_status = 'ok' AND l.fetched_at IS NOT NULL"
    ).fetchall()
    chosen = [
        url
        for url, held in rows
        if link_kind(url) == "page" and not (held or "").startswith(PAGE_DOCUMENT_PREFIX)
    ]
    if apply and chosen:
        conn.executemany(
            "UPDATE sharepoint_links SET fetched_at = NULL, attempts = 0 WHERE url = ?",
            [(url,) for url in chosen],
        )
        conn.commit()
    return {"links": len(chosen), "pages": len({target_of(url) for url in chosen})}


def _document_key(url: str) -> tuple[str, str, str]:
    """The document a link names: host, path and any parameter that names the file."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return (url, "", "")
    params = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
    named = "&".join(f"{k}={params[k][0]}" for k in _DOCUMENT_PARAMS if k in params)
    return (parsed.netloc.lower(), unquote(parsed.path).lower(), named.lower())


def refused_links(conn: sqlite3.Connection, status: str = "http-error") -> list[dict]:
    """Links recorded with `status`, one entry per document they name, oldest email first.

    'http-error' is our own tenant answering no: mostly access denied, which the owner of the
    file can grant (a refusal from another tenant is 'unsupported-host'). The variants Safe
    Links makes of one URL, one per email, collapse into their document. Read only.
    """
    groups: dict[tuple[str, str, str], dict] = {}
    for url, message_id, attempts, last_attempt in conn.execute(
        "SELECT url, message_id, attempts, last_attempt_at FROM sharepoint_links"
        " WHERE last_status = ?",
        (status,),
    ):
        doc = groups.setdefault(
            _document_key(url),
            {"url": url, "links": 0, "attempts": 0, "last_attempt": "", "message_ids": set()},
        )
        if len(url) < len(doc["url"]):
            doc["url"] = url
        doc["links"] += 1
        doc["attempts"] = max(doc["attempts"], attempts or 0)
        doc["last_attempt"] = max(doc["last_attempt"], last_attempt or "")
        doc["message_ids"].add(message_id)
    out = []
    for doc in groups.values():
        emails = []
        for message_id in doc.pop("message_ids"):
            row = conn.execute(
                "SELECT date_received, sender_address, subject FROM emails WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            date, sender, subject = row if row else (None, None, None)
            emails.append(
                {"message_id": message_id, "date": date, "sender": sender, "subject": subject}
            )
        emails.sort(key=lambda e: (e["date"] or "", str(e["message_id"])))
        out.append({**doc, "emails": emails})
    out.sort(key=lambda d: (d["emails"][0]["date"] or "") if d["emails"] else "")
    return out
