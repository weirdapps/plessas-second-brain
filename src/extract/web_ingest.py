"""Web and markdown content ingestion — URL fetching, YouTube transcripts, frontmatter parsing."""

import html as html_mod
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

# Browser-like headers to avoid blocks
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_TEXT_CONTENT_TYPES = {"text/html", "text/plain", "text/markdown", "application/json"}


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content.

    Args:
        content: Raw markdown text, possibly with --- delimited frontmatter.

    Returns:
        Tuple of (metadata dict, body text without frontmatter).
    """
    if not content.startswith("---"):
        return {}, content

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
    if not match:
        return {}, content

    yaml_block = match.group(1)
    body = match.group(2).strip()

    # Lightweight YAML parsing — handles flat key: value and quoted strings
    meta = {}
    for line in yaml_block.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value:
            meta[key] = value

    return meta, body


def fetch_url(url: str, timeout: int = 30) -> dict:
    """Fetch content from a URL.

    Args:
        url: URL to fetch.
        timeout: Request timeout in seconds.

    Returns:
        Dict with url, content (str or bytes), content_type, is_binary.

    Raises:
        httpx.HTTPStatusError: On non-2xx response.
    """
    response = httpx.get(url, headers=_HEADERS, follow_redirects=True, timeout=timeout)
    response.raise_for_status()

    raw_ct = response.headers.get("content-type", "")
    content_type = raw_ct.split(";")[0].strip().lower()
    is_binary = content_type not in _TEXT_CONTENT_TYPES

    return {
        "url": url,
        "content": response.content if is_binary else response.text,
        "content_type": content_type,
        "is_binary": is_binary,
    }


def _extract_youtube_id(url: str) -> str | None:
    """Extract video ID from various YouTube URL formats.

    Handles: youtube.com/watch?v=ID, youtu.be/ID, youtube.com/embed/ID,
             youtube.com/v/ID, youtube.com/shorts/ID
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if host in ("youtu.be",):
        return parsed.path.lstrip("/").split("/")[0] or None

    if host in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            return qs.get("v", [None])[0]
        for prefix in ("/embed/", "/v/", "/shorts/"):
            if parsed.path.startswith(prefix):
                return parsed.path[len(prefix) :].split("/")[0] or None

    return None


def _fetch_youtube_title(url: str) -> str | None:
    """Fetch the video title from YouTube page HTML."""
    try:
        result = fetch_url(url, timeout=15)
        return _extract_html_title(result["content"])
    except Exception:
        return None


def _fetch_youtube_transcript(video_id: str) -> str | None:
    """Fetch transcript text for a YouTube video.

    Tries English first, falls back to any available language.
    Returns plain text with timestamps stripped.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    try:
        transcript = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
    except Exception:
        try:
            transcript_list = api.list(video_id)
            if not transcript_list:
                return None
            transcript = transcript_list[0].fetch()
        except Exception:
            return None

    return " ".join(segment.text for segment in transcript)


def ingest_youtube(
    url: str,
    title: str | None = None,
    source: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Fetch a YouTube video transcript and ingest it.

    Args:
        url: YouTube video URL.
        title: Optional title (fetched from page if omitted).
        source: Optional source label.
        db_path: Database path.

    Returns:
        Dict from ingest_document with additional url field.
    """
    from src.extract.attachment_pipeline import ingest_document

    video_id = _extract_youtube_id(url)
    if not video_id:
        raise ValueError(f"Could not extract video ID from: {url}")

    # Fetch transcript
    transcript_text = _fetch_youtube_transcript(video_id)
    if not transcript_text:
        raise ValueError(f"No transcript available for video: {video_id}")

    # Get title
    if not title:
        yt_title = _fetch_youtube_title(url)
        if yt_title:
            # Clean YouTube's suffix: "Title - YouTube"
            title = re.sub(r"\s*[-–—]\s*YouTube\s*$", "", yt_title).strip()
        else:
            title = f"YouTube Video {video_id}"

    label = source or title
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    # Build markdown content
    md_content = f"# {label}\n\nSource: {canonical_url}\n\n## Transcript\n\n{transcript_text}\n"

    with tempfile.TemporaryDirectory() as staging:
        temp_path = os.path.join(staging, _staged_filename(label, ".md"))
        with open(temp_path, "w") as f:
            f.write(md_content)
        doc_result = ingest_document(
            temp_path,
            db_path=db_path,
            source=label,
            sender_name="YouTube Import",
            source_url=canonical_url,
        )
        doc_result["url"] = canonical_url
        return doc_result


def ingest_url(
    url: str,
    title: str | None = None,
    source: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Fetch a URL and ingest its content into the knowledge store.

    Automatically detects YouTube URLs and extracts transcripts.

    Args:
        url: URL to fetch and ingest.
        title: Optional title (auto-detected from HTML <title> if omitted).
        source: Optional source label for the synthetic email.
        db_path: Database path.

    Returns:
        Dict from ingest_document with additional url field.
    """
    # Route YouTube URLs to transcript extraction
    if _extract_youtube_id(url):
        return ingest_youtube(url, title=title, source=source, db_path=db_path)

    from src.extract.attachment_pipeline import ingest_document

    result = fetch_url(url)
    content = result["content"]
    content_type = result["content_type"]

    # Determine file extension and title
    if content_type == "text/html":
        ext = ".html"
        if not title:
            title = _extract_html_title(content) or _title_from_url(url)
    elif content_type == "application/pdf":
        ext = ".pdf"
        if not title:
            title = _title_from_url(url)
    elif content_type in ("text/plain", "text/markdown"):
        ext = ".md"
        if not title:
            title = _title_from_url(url)
    else:
        ext = Path(url.split("?")[0]).suffix or ".html"
        if not title:
            title = _title_from_url(url)

    label = source or title

    mode = "wb" if result["is_binary"] else "w"
    with tempfile.TemporaryDirectory() as staging:
        temp_path = os.path.join(staging, _staged_filename(label, ext))
        with open(temp_path, mode) as f:
            f.write(content)
        doc_result = ingest_document(
            temp_path,
            db_path=db_path,
            source=label,
            sender_name="Web Import",
            source_url=url,
        )
        doc_result["url"] = url
        return doc_result


def _extract_html_title(html_content: str) -> str | None:
    """Extract <title> from HTML content."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html_content, re.IGNORECASE | re.DOTALL)
    if match:
        title = match.group(1).strip()
        title = re.sub(r"\s+", " ", title)
        return html_mod.unescape(title)
    return None


def _staged_filename(label: str, ext: str) -> str:
    """A readable, filesystem-safe basename for the file handed to ingest_document.

    ingest_document stores `Path(file_path).name` verbatim, so staging in a bare
    NamedTemporaryFile is what put 193 rows called tmp0986_gid.html into the
    tree. The label is page-supplied and lands in a path, so everything outside
    a conservative set is folded away rather than escaped.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._")
    return f"{slug[:80] or 'document'}{ext}"


def _title_from_url(url: str) -> str:
    """Derive a human-readable title from a URL path."""
    path = urlparse(url).path.rstrip("/")
    if path:
        name = Path(path).stem
        return name.replace("-", " ").replace("_", " ").title()
    return urlparse(url).netloc
