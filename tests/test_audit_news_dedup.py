"""Google News copies of one article are one article; an empty article keeps its headline.

A Google News item links through a redirect token Google encodes afresh for each feed,
so the brand monitor's feeds brought one article in under up to 13 URLs, each a message
id of its own: 705 extra copies on the replica of 2026-10-20. And 11 articles above the
relevance threshold had an empty body and were dropped whole (audit 2026-10-20).

news-sync now skips a Google News article whose source and title match one already held
or staged within SAME_ARTICLE_DAYS, and `brain maint news-dedup` forgets the stored
copies through the forget path, the best copy of each kept. Synthetic articles only.
"""

import hashlib
import json
import sqlite3

import pytest

from src.export.news_export import (
    SAME_ARTICLE_DAYS,
    HeldArticles,
    dedupe_stored,
    export_news,
    held_articles,
)
from src.store.schema import create_database
from tests.brain_cli import brain

GNEWS = "https://news.google.com/rss/articles/CBMi{}?oc=5"


def _news_db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE articles (
            url TEXT PRIMARY KEY, title TEXT, source TEXT, published_at TEXT, content TEXT,
            relevance_score INTEGER, fetched_at TEXT, pipeline TEXT
        );
        CREATE TABLE digests (
            id INTEGER PRIMARY KEY, digest_type TEXT, created_at TEXT, article_count INTEGER,
            synthesis_text TEXT, pipeline TEXT
        );
        CREATE TABLE article_categories (article_url TEXT, category TEXT);
        CREATE TABLE article_tickers (article_url TEXT, ticker TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?, 'monitor')",
        [(url, title, source, at, content, 80, at) for url, title, source, at, content in rows],
    )
    conn.commit()
    conn.close()
    return path


def _copy(n, day, title="Acme Bank opens a new branch", source="Daily Wire", content=None):
    at = f"2026-10-{day:02d}T08:{n:02d}:00+00:00"
    return (GNEWS.format(f"token{n}"), title, source, at, content or f"{title} - {source}")


def _subjects(result):
    out = []
    for path in result["batch_files"]:
        out += [r["subject"] for r in json.loads(open(path).read())["emails"]]
    return out


def test_copies_under_other_google_urls_are_skipped(tmp_path):
    db = _news_db(tmp_path / "news.db", [_copy(1, 10), _copy(2, 10), _copy(3, 11)])

    result = export_news(db, tmp_path / "staging")

    assert result["articles"] == 1
    assert result["skipped"] == 2


def test_a_repeated_title_days_apart_is_another_story(tmp_path):
    db = _news_db(tmp_path / "news.db", [_copy(1, 1), _copy(2, 1 + SAME_ARTICLE_DAYS + 1)])

    assert export_news(db, tmp_path / "staging")["articles"] == 2


def test_the_same_title_from_another_source_or_off_google_is_kept(tmp_path):
    other_source = _copy(2, 10, source="Evening Post")
    direct = ("https://example.com/story", *_copy(3, 10)[1:])
    db = _news_db(tmp_path / "news.db", [_copy(1, 10), other_source, direct])

    assert export_news(db, tmp_path / "staging")["articles"] == 3


def test_a_copy_of_an_article_already_held_or_staged_is_skipped(tmp_path):
    staged = tmp_path / "staging"
    first = _news_db(tmp_path / "a.db", [_copy(1, 10)])
    export_news(first, staged)
    later = _news_db(tmp_path / "b.db", [_copy(2, 10)])

    result = export_news(later, staged, held=held_articles(staged))

    assert result["articles"] == 0
    assert result["skipped"] == 1


def test_an_empty_article_keeps_its_headline(tmp_path):
    db = _news_db(
        tmp_path / "news.db",
        [
            ("https://example.com/e1", "Card spending up 9%", "Daily Wire",
             "2026-10-10T08:00:00+00:00", "  \n "),
            ("https://example.com/e2", "", "Daily Wire", "2026-10-10T08:00:00+00:00", " "),
        ],
    )  # fmt: skip

    result = export_news(db, tmp_path / "staging")

    assert result["articles"] == 1
    (record,) = json.loads(open(result["batch_files"][0]).read())["emails"]
    assert record["content"].splitlines()[0] == "Card spending up 9%"


# --- the copies already stored -----------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _stored(conn, n, day, title="Acme Bank opens a new branch", body=None, url=None):
    url = url or GNEWS.format(f"token{n}")
    content = f"{body or title}\n\n---\nURL: {url}\nPipeline: monitor"
    cur = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, sender_name, sender_address,"
        " mailbox_name, content, internet_message_id) VALUES (?, ?, ?, 'Daily Wire',"
        " 'daily-wire@news.local', 'News', ?, ?)",
        (
            "news:article:" + hashlib.sha1(url.encode()).hexdigest(),
            f"2026-10-{day:02d}T08:{n:02d}:00+00:00",
            title,
            content,
            url,
        ),
    )
    conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'a fact')", (cur.lastrowid,))
    conn.commit()
    return cur.lastrowid


def test_the_stored_copies_go_and_the_fullest_stays(store):
    short = _stored(store, 1, 10)
    full = _stored(store, 2, 10, body="Acme Bank opens a new branch in the harbour district")
    _stored(store, 3, 11)
    other_story = _stored(store, 4, 20)
    direct = _stored(store, 5, 10, url="https://example.com/story")

    dry = dedupe_stored(store, dry_run=True)
    assert dry == {"articles": 1, "copies": 2, "forgotten": 0, "vectors": 0}
    assert store.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 5

    done = dedupe_stored(store)

    assert done["copies"] == done["forgotten"] == 2
    left = [r[0] for r in store.execute("SELECT id FROM emails ORDER BY id")]
    assert left == [full, other_story, direct]
    assert short not in left
    # Their children went with them.
    held = {r[0] for r in store.execute("SELECT email_id FROM key_facts")}
    assert held == set(left)
    assert dedupe_stored(store) == {"articles": 0, "copies": 0, "forgotten": 0, "vectors": 0}


def test_a_forgotten_copy_is_not_staged_again(store, tmp_path):
    _stored(store, 1, 10, body="Acme Bank opens a new branch in the harbour district")
    _stored(store, 2, 10)
    dedupe_stored(store)
    news = _news_db(tmp_path / "news.db", [_copy(1, 10), _copy(2, 10)])
    held = HeldArticles()
    held.add_store(store)
    ids = {r[0] for r in store.execute("SELECT message_id FROM emails")}

    result = export_news(news, tmp_path / "staging", exported_ids=ids, held=held)

    assert result["articles"] == 0


def test_news_dedup_refuses_on_a_replica(tmp_path, monkeypatch, capsys):
    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code, _, err = brain(monkeypatch, capsys, "--db", str(path), "maint", "news-dedup", "--dry-run")

    assert code == 2
    assert "replica" in err


def test_news_dedup_prints_its_counts(store, monkeypatch, capsys):
    _stored(store, 1, 10)
    _stored(store, 2, 10)
    path = store.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    code, out, _ = brain(monkeypatch, capsys, "--db", path, "maint", "news-dedup", "--dry-run")

    assert code == 0
    assert "1 copies" in out or "1 copy" in out
    assert store.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 2
