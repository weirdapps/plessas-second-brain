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
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.export.state import write_json_atomic

DEFAULT_RELEVANCE_THRESHOLD = 60
NEWS_MAILBOX = "News"
PIPELINES = ("digest", "monitor", "market", "stack")
NEWS_BATCH_START = 70000

# Synthesized sender domain, never a real one. An address here is the owner's only
# if BRAIN_USER_EMAIL_PATTERN lists it whole, or lists its local part with no domain,
# which matches at any domain (src/store/owner.py).
NEWS_DOMAIN = "news.local"


# A Google News item links through a redirect whose token Google encodes afresh for
# each feed that carries the story, and the token holds no address to decode. The
# brand monitor polls several feeds, so one article arrived under up to 13 URLs, each
# a message id of its own: 705 extra copies on the replica of 2026-10-20, 601 of 629
# of them within a day of the first. A Google News article whose source and title
# match one already held within SAME_ARTICLE_DAYS is that article. Further apart, a
# repeated title is taken for a new story ('Daily market wrap').
GOOGLE_NEWS_PREFIX = "https://news.google.com/"
SAME_ARTICLE_DAYS = 3


def is_google_news(url: str | None) -> bool:
    return (url or "").startswith(GOOGLE_NEWS_PREFIX)


def article_key(sender_address: str, title: str) -> tuple[str, str]:
    """What makes two Google News items one article: the source and the title."""
    return (sender_address or "").lower(), " ".join((title or "").split()).casefold()


class HeldArticles:
    """The Google News articles already held, by article_key, with their dates."""

    def __init__(self) -> None:
        self._dates: dict[tuple[str, str], list[datetime]] = defaultdict(list)

    def add(self, sender_address: str, title: str, date_received: str | None) -> None:
        when = _parse_dt(date_received)
        if when is not None:
            self._dates[article_key(sender_address, title)].append(when)

    def holds(self, sender_address: str, title: str, date_received: str) -> bool:
        """Whether this article is held already, under another URL."""
        when = _parse_dt(date_received)
        window = timedelta(days=SAME_ARTICLE_DAYS)
        return when is not None and any(
            abs(when - held) <= window
            for held in self._dates.get(article_key(sender_address, title), ())
        )

    def add_store(self, conn: sqlite3.Connection) -> None:
        """The Google News articles the store holds."""
        for address, title, when in conn.execute(
            "SELECT sender_address, subject, date_received FROM emails"
            " WHERE mailbox_name = ? AND message_id LIKE 'news:article:%'"
            " AND internet_message_id LIKE ?",
            (NEWS_MAILBOX, GOOGLE_NEWS_PREFIX + "%"),
        ):
            self.add(address, title, when)


def held_articles(staging_dir: str | Path | None = None) -> HeldArticles:
    """The Google News articles staged and not yet loaded; HeldArticles.add_store adds
    the store's."""
    held = HeldArticles()
    for batch_file in sorted(Path(staging_dir).glob("batch-*.json")) if staging_dir else ():
        try:
            records = json.loads(batch_file.read_text(encoding="utf-8"))["emails"]
            for r in records:
                if is_google_news(r.get("internet_message_id")):
                    held.add(
                        (r.get("sender") or {}).get("address", ""),
                        r.get("subject", ""),
                        r.get("date_received"),
                    )
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return held


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
    held: HeldArticles | None = None,
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
        held: Google News articles already held or staged (HeldArticles); the
            ones emitted here join it, so a second copy in this run is a copy too

    Returns:
        Dict with 'syntheses', 'articles', 'skipped', 'batch_files' keys.
        'skipped' counts records deliberately not emitted: already in
        exported_ids, a Google News copy of an article already held, or no
        parseable timestamp for date_received. An unreadable
        news_db (missing, not SQLite, foreign schema, a directory) returns all
        zeros rather than raising.
    """
    news_path = Path(news_db)
    result: dict = {"syntheses": 0, "articles": 0, "skipped": 0, "batch_files": []}
    pipelines = tuple(pipelines)
    if not news_path.exists() or not pipelines:
        return result

    exported_ids = exported_ids or set()
    held = held if held is not None else HeldArticles()
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
            # An article whose body is empty keeps its headline: news-reader scored it
            # on the headline, and 11 such articles above the threshold were dropped
            # whole (audit 2026-10-20). With no headline either there is nothing.
            text = _strip_html(row["content"] or "") or " ".join((row["title"] or "").split())
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
            address = record["sender"]["address"]
            if is_google_news(row["url"]):
                if held.holds(address, record["subject"], date_received):
                    result["skipped"] += 1
                    continue
                held.add(address, record["subject"], date_received)
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
        write_json_atomic(
            batch_file,
            {
                "batch_number": batch_number,
                "exported_at": exported_at,
                "source": "news-reader",
                "folder": NEWS_MAILBOX,
                "emails": records[start : start + batch_size],
            },
            redact=True,
        )
        result["batch_files"].append(str(batch_file))
        batch_number += 1

    return result


# --- the copies already stored: `brain maint news-dedup` --------------------------------


def _body_length(content: str | None) -> int:
    """How much an article says: its text without the footer news-sync appends."""
    body, sep, _ = (content or "").rpartition("\n---\n")
    return len(body if sep else content or "")


def stored_copies(conn: sqlite3.Connection) -> list[list[int]]:
    """The stored Google News copies of one article, each group [kept, copy, ...].

    Each article's fullest copy is kept (the earliest stored, of equals), and every other
    copy within SAME_ARTICLE_DAYS of it goes; copies further from it are grouped the same
    way again. A forgotten copy is then always within the window of the copy kept, so
    news-sync, which skips an article held within the window (HeldArticles), never
    stages it again.
    """
    by_article: dict[tuple[str, str], list[tuple[int, datetime, int]]] = defaultdict(list)
    for row_id, address, title, when, content in conn.execute(
        "SELECT id, sender_address, subject, date_received, content FROM emails"
        " WHERE mailbox_name = ? AND message_id LIKE 'news:article:%'"
        " AND internet_message_id LIKE ? ORDER BY id",
        (NEWS_MAILBOX, GOOGLE_NEWS_PREFIX + "%"),
    ):
        at = _parse_dt(when)
        if at is not None:
            by_article[article_key(address, title)].append((row_id, at, _body_length(content)))
    window = timedelta(days=SAME_ARTICLE_DAYS)
    groups = []
    for members in by_article.values():
        left = members
        while left:
            kept = max(left, key=lambda m: (m[2], -m[0]))
            near = [m for m in left if abs(m[1] - kept[1]) <= window]
            if len(near) > 1:
                groups.append([kept[0], *sorted(m[0] for m in near if m is not kept)])
            left = [m for m in left if abs(m[1] - kept[1]) > window]
    return groups


def dedupe_stored(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Forget the stored Google News copies, the kept copy of each article staying, through
    the forget path (src/store/forget.py), vectors included. Idempotent; a dry run counts.

    Returns {"articles", "copies", "forgotten", "vectors"}: articles held more than once,
    copies to forget, and the rows and vectors forgotten.
    """
    from src.store.forget import forget_emails

    groups = stored_copies(conn)
    copies = [row_id for group in groups for row_id in group[1:]]
    counts = {"articles": len(groups), "copies": len(copies), "forgotten": 0, "vectors": 0}
    if copies and not dry_run:
        stats = forget_emails(conn, copies)
        counts["forgotten"] = stats["emails"]
        counts["vectors"] = stats["vectors"]
    return counts


def cmd_news_dedup(args) -> int:
    """`brain maint news-dedup`."""
    import sys

    from src.store.schema import get_connection

    if not Path(args.db).exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        return 1
    conn = get_connection(str(args.db))
    try:
        c = dedupe_stored(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    if args.dry_run:
        print(
            f"news-dedup: dry run, nothing written: {c['copies']:,} copies of"
            f" {c['articles']:,} Google News articles would be forgotten, one copy of each kept"
        )
    else:
        print(
            f"news-dedup: forgot {c['forgotten']:,} copies of {c['articles']:,} Google News"
            f" articles and {c['vectors']:,} vectors; one copy of each kept"
        )
    return 0


def add_maint_parser(maint_subparsers) -> None:
    """`brain maint news-dedup`."""
    parser = maint_subparsers.add_parser(
        "news-dedup",
        help="Forget the stored copies of a Google News article held under several URLs"
        " (no model call; loads the vector index once to drop their vectors)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Count only: nothing is written")
    parser.set_defaults(func=cmd_news_dedup)
