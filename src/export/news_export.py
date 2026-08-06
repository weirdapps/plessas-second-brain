"""Export the news-reader database into second-brain staging format.

Reads the news-reader SQLite database (articles + digest syntheses) read-only
and writes source-agnostic staging batches the existing extractor + loader pick
up from their sorted glob over ``batch-*.json``.

Two record kinds are emitted: one per digest synthesis (the high-signal
artifact) and one per article scoring at or above the relevance threshold.
``articles.summary`` is deliberately never used — it holds raw RSS/Google-News
markup, not a summary.
"""

import hashlib
import html as html_mod
import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_RELEVANCE_THRESHOLD = 60
NEWS_MAILBOX = "News"
PIPELINES = ("digest", "monitor", "market", "stack")
NEWS_BATCH_START = 70000

# Synthesized sender domain. Must never collide with a real address, because
# BRAIN_USER_EMAIL_PATTERN is substring-matched against sender addresses.
NEWS_DOMAIN = "news.local"


def _slug(value: str) -> str:
    """Lowercase a source name into an address-safe local part."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-") or "unknown"


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are assumed UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _strip_html(raw: str) -> str:
    """Cheap HTML→text, whitespace collapsed onto a single line."""
    text = re.sub(r"<(?=[a-zA-Z/!?])[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html_mod.unescape(text)).strip()


def _resolve_article_date(published_at: str | None, fetched_at: str | None) -> str | None:
    """ISO-8601 article timestamp, or None when neither value parses.

    Guards against the future-dated published_at bug by clamping to fetched_at.
    When nothing parses there is no honest date to record: rather than writing
    the raw string (which breaks MAX(date_received), julianday() and the
    date-prefix comparisons downstream) the caller drops the article.
    """
    published = _parse_dt(published_at)
    fetched = _parse_dt(fetched_at)
    if published is None or (fetched is not None and published > fetched):
        published = fetched
    if published is None:
        return None
    return published.isoformat()


def _synthesis_record(row: sqlite3.Row, created: datetime) -> dict:
    pipeline = row["pipeline"]
    date_received = created.isoformat()
    stamp = created.strftime("%Y-%m-%d %H:%M")

    raw = row["synthesis_text"]
    try:
        body = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        body = raw
    header = f"Pipeline: {pipeline} | Type: {row['digest_type']} | Articles: {row['article_count']}"

    return {
        "message_id": f"news:synthesis:{pipeline}:{row['id']}",
        "date_received": date_received,
        "subject": f"[News/{pipeline}] {row['digest_type']} - {stamp}",
        "sender": {"name": f"News Reader ({pipeline})", "address": f"{pipeline}@{NEWS_DOMAIN}"},
        "to_recipients": [],
        "cc_recipients": [],
        "mailbox_name": NEWS_MAILBOX,
        "content": f"{header}\n{body}",
        "conversation_id": f"news:{pipeline}:{date_received[:10]}",
    }


def _article_record(row: sqlite3.Row, text: str, date_received: str) -> dict:
    url = row["url"]
    pipeline = row["pipeline"]

    lines = [
        text,
        "",
        "---",
        f"URL: {url}",
        f"Pipeline: {pipeline}",
        f"Relevance: {row['relevance_score']}",
    ]
    if row["categories"]:
        lines.append(f"Categories: {row['categories']}")
    if row["tickers"]:
        lines.append(f"Tickers: {row['tickers']}")

    return {
        "message_id": "news:article:" + hashlib.sha1(url.encode()).hexdigest(),
        "date_received": date_received,
        "subject": row["title"] or "",
        "sender": {
            "name": row["source"] or "",
            "address": f"{_slug(row['source'])}@{NEWS_DOMAIN}",
        },
        "to_recipients": [],
        "cc_recipients": [],
        "mailbox_name": NEWS_MAILBOX,
        "content": "\n".join(lines),
        "conversation_id": f"news:{pipeline}:{date_received[:10]}",
        "internet_message_id": url,
    }


def _next_batch_number(staging_dir: Path) -> int:
    """First free batch-NNNNN number at or above NEWS_BATCH_START."""
    max_existing = NEWS_BATCH_START - 1
    for path in staging_dir.glob("batch-*.json"):
        try:
            n = int(path.stem.removeprefix("batch-"))
        except ValueError:
            continue
        if n > max_existing:
            max_existing = n
    return max_existing + 1


def export_news(
    news_db: str | Path,
    staging_dir: str | Path,
    relevance_threshold: int = DEFAULT_RELEVANCE_THRESHOLD,
    pipelines: Sequence[str] = PIPELINES,
    exported_ids: set[str] | None = None,
    limit: int = 0,
    batch_size: int = 200,
) -> dict:
    """Export news syntheses + articles into staging batches.

    Args:
        news_db: Path to the news-reader SQLite database (opened read-only)
        staging_dir: Directory to write batch-NNNNN.json files into
        relevance_threshold: Minimum articles.relevance_score to export
        pipelines: Which news pipelines to include
        exported_ids: message_ids already in the brain (skip these)
        limit: Max total records to emit (0 = unlimited)
        batch_size: Records per staging batch file

    Returns:
        Dict with 'syntheses', 'articles', 'skipped', 'batch_files' keys.
        'skipped' counts records deliberately not emitted: already in
        exported_ids, or no parseable timestamp for date_received. An unreadable
        news_db (missing, not SQLite, foreign schema, a directory) returns all
        zeros rather than raising.
    """
    news_path = Path(news_db)
    result: dict = {"syntheses": 0, "articles": 0, "skipped": 0, "batch_files": []}
    pipelines = tuple(pipelines)
    if not news_path.exists() or not pipelines:
        return result

    exported_ids = exported_ids or set()
    placeholders = ",".join("?" * len(pipelines))
    records: list[dict] = []

    try:
        conn = sqlite3.connect(f"{news_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return result
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(
            f"SELECT id, digest_type, created_at, article_count, synthesis_text, pipeline"
            f" FROM digests WHERE pipeline IN ({placeholders})"
            f" AND COALESCE(synthesis_text, '') != '' ORDER BY created_at",
            pipelines,
        ):
            if limit and len(records) >= limit:
                break
            if not row["synthesis_text"].strip():
                continue
            created = _parse_dt(row["created_at"])
            if created is None:
                result["skipped"] += 1
                continue
            record = _synthesis_record(row, created)
            if record["message_id"] in exported_ids:
                result["skipped"] += 1
                continue
            records.append(record)
            result["syntheses"] += 1

        for row in conn.execute(
            f"SELECT a.url, a.title, a.source, a.published_at, a.fetched_at, a.content,"
            f" a.relevance_score, a.pipeline,"
            f" (SELECT group_concat(category, ', ') FROM article_categories"
            f"  WHERE article_url = a.url) AS categories,"
            f" (SELECT group_concat(ticker, ', ') FROM article_tickers"
            f"  WHERE article_url = a.url) AS tickers"
            f" FROM articles a WHERE a.pipeline IN ({placeholders})"
            f" AND a.relevance_score >= ? ORDER BY a.fetched_at",
            (*pipelines, relevance_threshold),
        ):
            if limit and len(records) >= limit:
                break
            text = _strip_html(row["content"] or "")
            if not text:
                continue
            date_received = _resolve_article_date(row["published_at"], row["fetched_at"])
            if date_received is None:
                result["skipped"] += 1
                continue
            record = _article_record(row, text, date_received)
            if record["message_id"] in exported_ids:
                result["skipped"] += 1
                continue
            records.append(record)
            result["articles"] += 1
    except sqlite3.Error:
        # Existing but unusable (not SQLite, foreign schema, a directory):
        # degrade exactly like a missing file instead of raising at the CLI.
        return {"syntheses": 0, "articles": 0, "skipped": 0, "batch_files": []}
    finally:
        conn.close()

    if not records:
        return result

    staging_path = Path(staging_dir)
    staging_path.mkdir(parents=True, exist_ok=True)
    batch_number = _next_batch_number(staging_path)
    exported_at = datetime.now(UTC).isoformat()

    for start in range(0, len(records), batch_size):
        batch_file = staging_path / f"batch-{batch_number:05d}.json"
        batch_file.write_text(
            json.dumps(
                {
                    "batch_number": batch_number,
                    "exported_at": exported_at,
                    "source": "news-reader",
                    "folder": NEWS_MAILBOX,
                    "emails": records[start : start + batch_size],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result["batch_files"].append(str(batch_file))
        batch_number += 1

    return result
