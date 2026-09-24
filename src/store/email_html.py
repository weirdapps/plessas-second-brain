"""The HTML of an email body, kept compressed beside the text the body holds.

Outlook sends HTML: 19,774 emails held 964 MB of it, two thirds of the
emails.content column. The body now holds the text a reader sees (see
src.extract.html_text), which is what gets indexed and read. Two readers need
the markup itself, so it is kept here (schema v23): the SharePoint link scan,
which takes URLs from hrefs, and the inline-image positions, which look for cid:
references. zlib keeps it at about an eighth of its size.
"""

import sqlite3
import zlib

from src.extract.html_text import html_to_text, looks_like_html


def split_body(content: str | None) -> tuple[str | None, str | None]:
    """(the body to store, the HTML to keep beside it, or None for a text body)."""
    if content is None or not looks_like_html(content):
        return content, None
    return html_to_text(content), content


def pack(html: str) -> bytes:
    return zlib.compress(html.encode("utf-8"), 6)


def unpack(blob: bytes | None) -> str | None:
    return None if blob is None else zlib.decompress(blob).decode("utf-8")


def save_html(conn: sqlite3.Connection, email_id: int, html: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO email_html (email_id, html) VALUES (?, ?)", (email_id, pack(html))
    )


def markup_or_text(content: str | None, blob: bytes | None) -> str | None:
    """What a markup reader should scan: the HTML kept for the email, else its body."""
    html = unpack(blob)
    return html if html is not None else content
