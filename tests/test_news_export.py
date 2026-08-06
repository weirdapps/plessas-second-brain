"""Tests for the news-reader exporter."""

import hashlib
import json
import sqlite3
from datetime import datetime

import pytest

from src.export.news_export import NEWS_BATCH_START, export_news

ZEROS = {"syntheses": 0, "articles": 0, "skipped": 0, "batch_files": []}

SYNTHESIS_JSON = json.dumps(
    {
        "mention_count": 2,
        "sentiment_summary": {"positive": 1, "negative": 1, "trend": "stable"},
        "alerts": ["Επιτόκια υπό πίεση"],
        "company_mentions": [{"name": "Acme Bank", "count": 2}],
    }
)


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE articles (
            url TEXT PRIMARY KEY, title TEXT, source TEXT, author TEXT,
            published_at TEXT, content TEXT, summary TEXT, content_hash TEXT,
            language TEXT, relevance_score INTEGER, fetched_at TEXT,
            included_in_digest_id INTEGER, also_reported_by TEXT, pipeline TEXT,
            sentiment TEXT, mention_type TEXT, urgency TEXT
        );
        CREATE TABLE digests (
            id INTEGER PRIMARY KEY, digest_type TEXT, created_at TEXT,
            article_count INTEGER, synthesis_text TEXT, html_output TEXT,
            sent_at TEXT, pipeline TEXT
        );
        CREATE TABLE article_categories (article_url TEXT, category TEXT);
        CREATE TABLE article_tickers (article_url TEXT, ticker TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO digests (id, digest_type, created_at, article_count, synthesis_text, pipeline)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "scheduled", "2026-06-21T05:00:44.915282+00:00", 3, SYNTHESIS_JSON, "monitor"),
            (2, "morning", "2026-06-22T07:00:00+00:00", 5, "Plain synthesis, not JSON.", "digest"),
            (3, "scheduled", "2026-06-22T09:00:00+00:00", 0, "   ", "market"),
        ],
    )
    conn.executemany(
        "INSERT INTO articles (url, title, source, published_at, content, summary,"
        " relevance_score, fetched_at, pipeline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "https://example.com/a1",
                "Acme Bank posts record quarter",
                "Acme Wire & Co.",
                "2026-06-20T10:00:00+00:00",
                "<p>Profit rose   sharply.</p>\n<div>Shares up 4&amp;#37;</div>",
                "<a href='https://news.example.com'>RSS JUNK MARKER</a>",
                80,
                "2026-06-21T05:00:00+00:00",
                "monitor",
            ),
            (
                "https://example.com/a2",
                "Low relevance filler",
                "Acme Wire & Co.",
                "2026-06-20T11:00:00+00:00",
                "Barely related content.",
                "junk",
                30,
                "2026-06-21T05:00:00+00:00",
                "monitor",
            ),
            (
                "https://example.com/a3",
                "Future dated story",
                "Beta Times",
                "2026-10-14T00:00:00+00:00",
                "Published in the future by mistake.",
                "junk",
                70,
                "2026-06-21T06:30:00+00:00",
                "digest",
            ),
            (
                "https://example.com/a4",
                "Empty body",
                "Beta Times",
                "2026-06-20T12:00:00+00:00",
                "   \n  ",
                "junk",
                90,
                "2026-06-21T07:00:00+00:00",
                "digest",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO article_categories (article_url, category) VALUES (?, ?)",
        [("https://example.com/a1", "Banking"), ("https://example.com/a1", "Markets")],
    )
    conn.execute(
        "INSERT INTO article_tickers (article_url, ticker) VALUES (?, ?)",
        ("https://example.com/a1", "ACME"),
    )
    conn.commit()
    conn.close()
    return path


def _add_article(
    path,
    url,
    title,
    content,
    published_at="2026-06-20T10:00:00+00:00",
    fetched_at="2026-06-21T05:00:00+00:00",
    relevance_score=90,
    pipeline="monitor",
):
    """Append one article row to an existing fixture database."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO articles (url, title, source, published_at, content, summary,"
        " relevance_score, fetched_at, pipeline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            url,
            title,
            "Beta Times",
            published_at,
            content,
            "junk",
            relevance_score,
            fetched_at,
            pipeline,
        ),
    )
    conn.commit()
    conn.close()
    return "news:article:" + hashlib.sha1(url.encode()).hexdigest()


def _add_digest(path, digest_id, created_at, pipeline="monitor"):
    """Append one digest row to an existing fixture database."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO digests (id, digest_type, created_at, article_count, synthesis_text,"
        " pipeline) VALUES (?, ?, ?, ?, ?, ?)",
        (digest_id, "scheduled", created_at, 1, "Synthesis body.", pipeline),
    )
    conn.commit()
    conn.close()
    return f"news:synthesis:{pipeline}:{digest_id}"


@pytest.fixture
def news_db(tmp_path):
    return _make_db(tmp_path / "news.db")


@pytest.fixture
def staging(tmp_path):
    d = tmp_path / "staging"
    d.mkdir()
    return d


def _records(result):
    """Flatten every emitted batch file into one list of staging records."""
    out = []
    for path in result["batch_files"]:
        with open(path, encoding="utf-8") as f:
            batch = json.load(f)
        assert batch["source"] == "news-reader"
        assert batch["emails"]
        out.extend(batch["emails"])
    return out


def _by_id(result, message_id):
    matches = [r for r in _records(result) if r["message_id"] == message_id]
    assert len(matches) == 1, f"{message_id} not found exactly once"
    return matches[0]


def test_synthesis_record_shape_and_json_pretty_print(news_db, staging):
    """A digest synthesis becomes a staging record with pretty-printed JSON content."""
    result = export_news(news_db, staging)

    rec = _by_id(result, "news:synthesis:monitor:1")
    assert rec["subject"] == "[News/monitor] scheduled - 2026-06-21 05:00"
    assert rec["sender"] == {"name": "News Reader (monitor)", "address": "monitor@news.local"}
    assert rec["to_recipients"] == []
    assert rec["cc_recipients"] == []
    assert rec["mailbox_name"] == "News"
    assert rec["date_received"].startswith("2026-06-21T05:00:44")
    assert rec["conversation_id"] == "news:monitor:2026-06-21"

    lines = rec["content"].splitlines()
    assert lines[0] == "Pipeline: monitor | Type: scheduled | Articles: 3"
    body = rec["content"].split("\n", 1)[1]
    # Pretty-printed, Greek left readable (not \uXXXX escaped)
    assert '  "mention_count": 2' in body
    assert "Επιτόκια υπό πίεση" in body
    assert json.loads(body) == json.loads(SYNTHESIS_JSON)


def test_synthesis_non_json_passes_through_unchanged(news_db, staging):
    """synthesis_text that is not JSON is carried through verbatim."""
    result = export_news(news_db, staging)

    rec = _by_id(result, "news:synthesis:digest:2")
    assert rec["content"] == (
        "Pipeline: digest | Type: morning | Articles: 5\nPlain synthesis, not JSON."
    )


def test_empty_synthesis_text_excluded(news_db, staging):
    """A digest row with whitespace-only synthesis_text produces no record."""
    result = export_news(news_db, staging)

    ids = {r["message_id"] for r in _records(result)}
    assert "news:synthesis:market:3" not in ids
    assert result["syntheses"] == 2


def test_article_record_shape_html_stripped_and_footer(news_db, staging):
    """An article becomes a record with stripped HTML plus a metadata footer."""
    result = export_news(news_db, staging)

    url = "https://example.com/a1"
    expected_id = "news:article:" + hashlib.sha1(url.encode()).hexdigest()
    rec = _by_id(result, expected_id)

    assert rec["subject"] == "Acme Bank posts record quarter"
    assert rec["sender"] == {"name": "Acme Wire & Co.", "address": "acme-wire-co@news.local"}
    assert rec["to_recipients"] == []
    assert rec["cc_recipients"] == []
    assert rec["mailbox_name"] == "News"
    assert rec["date_received"].startswith("2026-06-20T10:00:00")
    assert rec["conversation_id"] == "news:monitor:2026-06-20"
    assert rec["internet_message_id"] == url

    content = rec["content"]
    assert "Profit rose sharply. Shares up 4&#37;" in content
    assert "<p>" not in content
    assert "RSS JUNK MARKER" not in content
    assert f"URL: {url}" in content
    assert "Pipeline: monitor" in content
    assert "Relevance: 80" in content
    assert "Banking" in content and "Markets" in content
    assert "Tickers: ACME" in content


def test_article_without_categories_or_tickers_omits_those_lines(news_db, staging):
    """Footer lines for categories/tickers only appear when values exist."""
    result = export_news(news_db, staging)

    url = "https://example.com/a3"
    rec = _by_id(result, "news:article:" + hashlib.sha1(url.encode()).hexdigest())
    assert "Categories:" not in rec["content"]
    assert "Tickers:" not in rec["content"]


def test_relevance_threshold_excludes_low_scores(news_db, staging):
    """Articles below the relevance threshold are not exported."""
    result = export_news(news_db, staging)

    subjects = {r["subject"] for r in _records(result)}
    assert "Low relevance filler" not in subjects

    lowered = export_news(news_db, staging, relevance_threshold=30)
    assert "Low relevance filler" in {r["subject"] for r in _records(lowered)}


def test_future_published_at_clamped_to_fetched_at(news_db, staging):
    """A published_at later than fetched_at falls back to fetched_at."""
    result = export_news(news_db, staging)

    url = "https://example.com/a3"
    rec = _by_id(result, "news:article:" + hashlib.sha1(url.encode()).hexdigest())
    assert rec["date_received"].startswith("2026-06-21T06:30:00")
    assert rec["conversation_id"] == "news:digest:2026-06-21"


def test_whitespace_only_content_skipped(news_db, staging):
    """Articles whose content is blank after stripping are not exported."""
    result = export_news(news_db, staging)

    subjects = {r["subject"] for r in _records(result)}
    assert "Empty body" not in subjects
    assert result["articles"] == 2


def test_exported_ids_are_skipped_and_counted(news_db, staging):
    """Already-exported message_ids are skipped and counted, not re-emitted."""
    url = "https://example.com/a1"
    already = {
        "news:synthesis:monitor:1",
        "news:article:" + hashlib.sha1(url.encode()).hexdigest(),
    }

    result = export_news(news_db, staging, exported_ids=already)

    ids = {r["message_id"] for r in _records(result)}
    assert not (ids & already)
    assert result["skipped"] == 2
    assert result["syntheses"] == 1
    assert result["articles"] == 1


def test_pipeline_filter(news_db, staging):
    """Only the requested pipelines are exported."""
    result = export_news(news_db, staging, pipelines=("monitor",))

    assert result["syntheses"] == 1
    assert result["articles"] == 1
    assert all("monitor" in r["conversation_id"] for r in _records(result))


def test_limit_caps_total_records(news_db, staging):
    """limit caps the combined synthesis + article record count."""
    result = export_news(news_db, staging, limit=2)

    assert result["syntheses"] + result["articles"] == 2
    assert len(_records(result)) == 2


def test_batch_numbering_starts_at_70000_and_never_overwrites(news_db, staging):
    """Batches are numbered from 70000 upward, skipping numbers already on disk."""
    fresh = export_news(news_db, staging, batch_size=2)
    assert fresh["batch_files"][0].endswith(f"batch-{NEWS_BATCH_START}.json")
    assert len(fresh["batch_files"]) == 2
    assert fresh["batch_files"][1].endswith(f"batch-{NEWS_BATCH_START + 1}.json")

    existing = json.loads((staging / f"batch-{NEWS_BATCH_START}.json").read_text())
    rerun = export_news(news_db, staging, batch_size=100)
    assert rerun["batch_files"] == [str(staging / f"batch-{NEWS_BATCH_START + 2}.json")]
    # The pre-existing batch survived untouched
    assert json.loads((staging / f"batch-{NEWS_BATCH_START}.json").read_text()) == existing


def test_missing_news_db_returns_zeros(tmp_path, staging):
    """A missing news database returns an empty result instead of raising."""
    result = export_news(tmp_path / "nope.db", staging)

    assert result == ZEROS


def test_not_a_database_returns_zeros(tmp_path, staging):
    """A file that exists but is not SQLite degrades like a missing file."""
    junk = tmp_path / "news.db"
    junk.write_bytes(b"this is plainly not a sqlite database")

    result = export_news(junk, staging)

    assert result == ZEROS
    assert list(staging.glob("batch-*.json")) == []


def test_foreign_schema_database_returns_zeros(tmp_path, staging):
    """A valid SQLite file without the news tables degrades like a missing file."""
    other = tmp_path / "news.db.pre-migration-archive"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    result = export_news(other, staging)

    assert result == ZEROS
    assert list(staging.glob("batch-*.json")) == []


def test_directory_path_returns_zeros(tmp_path, staging):
    """A directory where the database should be degrades like a missing file."""
    as_dir = tmp_path / "news.db"
    as_dir.mkdir()

    result = export_news(as_dir, staging)

    assert result == ZEROS


def test_markup_only_content_skipped(news_db, staging):
    """Content that is markup only is empty once stripped, so it is not exported."""
    _add_article(news_db, "https://example.com/a5", "Markup only", "<p>&nbsp;</p>")

    result = export_news(news_db, staging)

    assert "Markup only" not in {r["subject"] for r in _records(result)}
    assert result["articles"] == 2


def test_short_content_still_exported(news_db, staging):
    """Genuinely short (but non-empty) monitor articles are still exported."""
    message_id = _add_article(news_db, "https://example.com/a6", "Short one", "Up 2%.")

    result = export_news(news_db, staging)

    rec = _by_id(result, message_id)
    assert rec["content"].splitlines()[0] == "Up 2%."


def test_unparseable_published_at_falls_back_to_fetched_at(news_db, staging):
    """An RFC-2822 published_at still yields an ISO date via fetched_at."""
    message_id = _add_article(
        news_db,
        "https://example.com/a7",
        "Bad published date",
        "Body text.",
        published_at="Tue, 21 Jun 2026 05:00:00 GMT",
        fetched_at="2026-06-21T06:00:00+00:00",
    )

    result = export_news(news_db, staging)

    rec = _by_id(result, message_id)
    assert rec["date_received"].startswith("2026-06-21T06:00:00")
    assert rec["conversation_id"] == "news:monitor:2026-06-21"


def test_article_with_no_parseable_date_is_skipped_and_counted(news_db, staging):
    """When neither timestamp parses the article is dropped, never given a junk date."""
    _add_article(
        news_db,
        "https://example.com/a8",
        "No usable date",
        "Body text.",
        published_at="Tue, 21 Jun 2026 05:00:00 GMT",
        fetched_at="unknown",
    )

    result = export_news(news_db, staging)

    assert "No usable date" not in {r["subject"] for r in _records(result)}
    assert result["articles"] == 2
    assert result["skipped"] == 1


def test_synthesis_with_unparseable_created_at_is_skipped_and_counted(news_db, staging):
    """A digest whose created_at cannot be parsed is dropped, not dated with junk."""
    _add_digest(news_db, 4, "unknown")

    result = export_news(news_db, staging)

    assert "news:synthesis:monitor:4" not in {r["message_id"] for r in _records(result)}
    assert result["syntheses"] == 2
    assert result["skipped"] == 1


def test_every_date_received_is_iso_8601(news_db, staging):
    """date_received must always parse as ISO-8601 for the store's date maths."""
    _add_article(
        news_db,
        "https://example.com/a9",
        "No usable date",
        "Body text.",
        published_at="Tue, 21 Jun 2026 05:00:00 GMT",
        fetched_at="unknown",
    )
    _add_digest(news_db, 4, "not a timestamp")

    result = export_news(news_db, staging)

    records = _records(result)
    assert records
    for rec in records:
        assert datetime.fromisoformat(rec["date_received"])


def test_all_sender_addresses_use_news_local(news_db, staging):
    """Synthesized sender addresses can never collide with a real mail domain."""
    result = export_news(news_db, staging)

    records = _records(result)
    assert records
    assert all(r["sender"]["address"].endswith("@news.local") for r in records)


def test_news_db_is_not_modified(news_db, staging):
    """The exporter opens the news database read-only."""
    before = news_db.stat().st_mtime_ns

    export_news(news_db, staging)

    assert news_db.stat().st_mtime_ns == before
    conn = sqlite3.connect(news_db)
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 4
    conn.close()
