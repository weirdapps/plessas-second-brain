"""SharePoint files become text-only documents; the fetched file is never kept.

A link found in mail is fetched into a temporary directory, extracted, and stored through
ingest_text_document (src/extract/attachment_pipeline.py). The fetched bytes' hash is the
document's identity, so a file linked from many emails is one document, and each link records
it in sharepoint_links.document_message_id.
"""

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from src.export import sharepoint_fetcher
from src.export.sharepoint_fetcher import SharepointFetchResult
from src.extract.attachment_extractors import extract_text_from_file
from src.extract.attachment_pipeline import (
    _guess_mime_type,
    _sha256_to_message_id,
    ingest_text_document,
)
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
    conn: sqlite3.Connection, path: Path, url: str | None = None, date: str | None = None
) -> tuple[int, bool]:
    """Store a fetched file as a text-only document: (its message id, whether it is new).

    The same bytes stored before are not extracted again.
    """
    sha = sha256_of_file(path)
    message_id = _sha256_to_message_id(sha)
    if conn.execute("SELECT 1 FROM emails WHERE message_id = ?", (message_id,)).fetchone():
        return message_id, False
    mime = _guess_mime_type(str(path))
    result = extract_text_from_file(str(path), mime)
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


def fetch_and_ingest(
    conn: sqlite3.Connection, url: str, message_id
) -> tuple[SharepointFetchResult, int | None]:
    """Fetch a link into a temporary directory and store its text. No file outlives the call.

    The fetcher is looked up on its module at call time, where the tests replace it.
    """
    with tempfile.TemporaryDirectory(prefix="sb-sharepoint-") as tmp:
        result = sharepoint_fetcher.fetch_sharepoint_link(url, Path(tmp))
        document = None
        if result.status == "ok" and result.local_path and result.local_path.is_file():
            document, _ = ingest_fetched_file(
                conn, result.local_path, url, _email_date(conn, message_id)
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
