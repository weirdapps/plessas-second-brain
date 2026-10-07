"""SharePoint files and pages become text-only documents; the fetched file is never kept.

A link found in mail is fetched into a temporary directory, extracted, and stored through
ingest_text_document (src/extract/attachment_pipeline.py). The fetched bytes' hash is the
document's identity, so a file linked from many emails is one document, and each link records
it in sharepoint_links.document_message_id. An intranet page is read as text (sharepoint-cli
page); its text is its identity. A link that is not content is not fetched at all.
"""

import hashlib
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from src.export import sharepoint_fetcher
from src.export.sharepoint_fetcher import SharepointFetchResult, SharepointPageResult
from src.extract.attachment_extractors import _apply_noise_filter, extract_text_from_file
from src.extract.attachment_pipeline import (
    _guess_mime_type,
    _sha256_to_message_id,
    ingest_text_document,
)
from src.extract.html_text import html_to_text
from src.store.file_hashes import sha256_of_file


def _label(url: str | None, filename: str) -> str:
    """The site and path a link points at, readable: what the document's subject shows."""
    if not url:
        return filename
    parsed = urlparse(url)
    path = unquote(parsed.path).strip("/")
    return f"{parsed.netloc}/{path}" if path else (parsed.netloc or filename)


def _email_date(conn: sqlite3.Connection, message_id) -> str | None:
    """When the email carrying the link arrived: the date its document is filed under."""
    if message_id is None:
        return None
    row = conn.execute(
        "SELECT date_received FROM emails WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row[0] if row else None


def ingest_fetched_file(
    conn: sqlite3.Connection,
    path: Path,
    url: str | None = None,
    date: str | None = None,
    *,
    require_text: bool = False,
) -> tuple[int | None, bool]:
    """Store a fetched file as a text-only document: (its message id, whether it is new).

    The same bytes stored before are not extracted again. With `require_text`, a file that
    yields no text makes no document and (None, False) comes back: a view link often returns
    the browser page (an .aspx viewer, a sign-in or error page) instead of the file. The backlog
    pass keeps its default, because a file left on disk needs its row for the sweep to know it
    was read.
    """
    sha = sha256_of_file(path)
    message_id = _sha256_to_message_id(sha)
    if conn.execute("SELECT 1 FROM emails WHERE message_id = ?", (message_id,)).fetchone():
        return message_id, False
    mime = _guess_mime_type(str(path))
    result = extract_text_from_file(str(path), mime)
    if require_text and not result["text"]:
        return None, False
    ingest_text_document(
        conn,
        source="sharepoint",
        key=sha,
        filename=path.name,
        mime_type=mime,
        text=result["text"],
        sha256=sha,
        method=result["method"],
        status=result["status"],
        error=result["error"],
        subject=f"[SharePoint] {_label(url, path.name)}",
        sender_name="SharePoint",
        date=date or datetime.now().isoformat(),
    )
    return message_id, True


def ingest_page(
    conn: sqlite3.Connection, page: SharepointPageResult, date: str | None = None
) -> int | None:
    """Store a page's text as a text-only document: its message id, or None for no text.

    The text is the identity, as for a session note: the same page linked from many emails is
    one document, and a page edited since is a new one. The key is the page's lower-cased path.

    A page whose body holds no text is stored by its title. Intranet news pages are often a
    title banner over a picture (15 of them on 2026-09-30), and making no document for them
    left their links recorded 'ok' with nothing held. The method says the text is the title.
    Only a page with neither comes back None.
    """
    text = html_to_text(page.html or "").strip()
    method = "sharepoint-page"
    if not text:
        text = (page.title or "").strip()
        method = "sharepoint-page-title"
    if not text:
        return None
    path = page.path or unquote(urlparse(page.url).path)
    name = Path(path).name
    outcome = ingest_text_document(
        conn,
        source="sharepoint-page",
        key=path.lower(),
        filename=name,
        mime_type="text/html",
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        method=method,
        status="skipped" if _apply_noise_filter(text) else "extracted",
        error=None,
        subject=f"[SharePoint page] {page.title or _label(page.url, name)}",
        sender_name="SharePoint",
        date=date or datetime.now().isoformat(),
    )
    return outcome["message_id"]


def fetch_and_ingest(
    conn: sqlite3.Connection, url: str, message_id
) -> tuple[SharepointFetchResult, int | None]:
    """Fetch what a link points at and store its text. No file outlives the call.

    A file is fetched into a temporary directory, a page is read as text, and a link that is
    not content is recorded as such without a fetch. The fetchers are looked up on their module
    at call time, where the tests replace them.
    """
    kind = sharepoint_fetcher.link_kind(url)
    if kind == "not-content":
        return SharepointFetchResult(url=url, status="not-content"), None
    if kind == "page":
        page = sharepoint_fetcher.fetch_sharepoint_page(url)
        date = _email_date(conn, message_id)
        document = ingest_page(conn, page, date) if page.status == "ok" else None
        status, error = page.status, page.error_message
        if status == "ok" and document is None:
            # Read, but nothing to store: recorded 'ok' this would settle the link with no
            # document and never offer it again.
            status, error = "no-text", "the page came back with neither text nor a title"
        result = SharepointFetchResult(
            url=url,
            status=status,
            http_status=page.http_status,
            file_name=page.title or None,
            error_message=error,
        )
        return result, document
    with tempfile.TemporaryDirectory(prefix="sb-sharepoint-") as tmp:
        result = sharepoint_fetcher.fetch_sharepoint_link(url, Path(tmp))
        document = None
        if result.status == "ok" and result.local_path and result.local_path.is_file():
            document, _ = ingest_fetched_file(
                conn, result.local_path, url, _email_date(conn, message_id), require_text=True
            )
    return result, document


def ingest_fetched_backlog(conn: sqlite3.Connection, roots) -> dict:
    """Store the files earlier fetches left on disk, and offer again the links that lost theirs.

    A file is matched to the links that fetched it by folder name, file name and size. A link
    whose recorded file is missing, or holds another size now, lost its content to a later fetch
    of the same name: it is marked unfetched, so the retry pass fetches it into a temporary
    directory. Files are not deleted here; the sweep deletes them once their text is stored.
    """
    roots = [Path(r) for r in roots]
    links: dict[tuple[str, str], list[tuple[str, object, int | None]]] = {}
    for url, message_id, fetched_path, size in conn.execute(
        "SELECT url, message_id, fetched_path, file_size FROM sharepoint_links"
        " WHERE fetched_path IS NOT NULL AND fetched_at IS NOT NULL"
    ):
        p = Path(fetched_path)
        links.setdefault((p.parent.name, p.name), []).append((url, message_id, size))
    stats = dict.fromkeys(("files", "ingested", "already", "linked", "refetch"), 0)
    matched: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(p for p in root.iterdir() if p.is_file() and not p.name.startswith(".")):
            stats["files"] += 1
            size = path.stat().st_size
            mine = [(u, m) for u, m, s in links.get((root.name, path.name), []) if s == size]
            url, message_id = mine[0] if mine else (None, None)
            document, new = ingest_fetched_file(conn, path, url, _email_date(conn, message_id))
            stats["ingested" if new else "already"] += 1
            for u, _m in mine:
                conn.execute(
                    "UPDATE sharepoint_links SET document_message_id = ? WHERE url = ?",
                    (document, u),
                )
                matched.add(u)
                stats["linked"] += 1
            conn.commit()
    by_name = {r.name: r for r in roots}
    lost = []
    for (folder, name), rows in links.items():
        root = by_name.get(folder)
        if root is None:
            continue
        f = root / name
        actual = f.stat().st_size if f.is_file() else None
        lost += [u for u, _m, size in rows if u not in matched and actual != size]
    conn.executemany(
        "UPDATE sharepoint_links SET fetched_at = NULL, attempts = 0"
        " WHERE url = ? AND document_message_id IS NULL",
        [(u,) for u in lost],
    )
    conn.commit()
    stats["refetch"] = len(lost)
    return stats
