"""The News row fires when a digest misses its cycle, not when the lag passes 12 hours.

check_news compared the newest upstream item of any relevance with the newest ingested
one, against 12 hours. Before the 08:50 staging that gap is eight to ten healthy hours
after the quiet night, so the threshold had to sit above it, and at 23:50 it could only
catch a pipeline dead since before noon: on 2026-10-20 a batch staged at 22:51 waited for
the 01:10 load and nothing fired. Now the news run loads what it stages, and the row also
asks whether the newest digest written more than one cycle ago (NEWS_ARRIVAL_GRACE) is in
the store. Digests are written every one to two hours through the day.
"""

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"
NOW = datetime(2026, 10, 20, 20, 50, tzinfo=UTC)


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _news(path, digests, articles=()):
    """digests: (id, pipeline, created_at, synthesis_text)."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE articles (url TEXT PRIMARY KEY, published_at TEXT)")
    con.execute(
        "CREATE TABLE digests (id INTEGER PRIMARY KEY, pipeline TEXT, created_at TEXT,"
        " synthesis_text TEXT)"
    )
    con.executemany("INSERT INTO digests VALUES (?, ?, ?, ?)", digests)
    con.executemany(
        "INSERT INTO articles VALUES (?, ?)",
        [(f"https://example.invalid/{i}", ts) for i, ts in enumerate(articles)],
    )
    con.commit()
    con.close()
    return path


def _brain(message_ids, newest):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE emails (message_id TEXT, date_received TEXT, mailbox_name TEXT)")
    db.executemany("INSERT INTO emails VALUES (?, ?, 'News')", [(m, newest) for m in message_ids])
    return db


def _at(**delta):
    return (NOW - timedelta(**delta)).isoformat()


def test_a_digest_that_missed_its_cycle_is_stale_though_the_lag_is_short(hc, tmp_path):
    news = _news(
        tmp_path / "news.db",
        [
            (7, "monitor", _at(hours=3), '{"summary": "x"}'),
            (8, "monitor", _at(hours=1), '{"summary": "x"}'),
        ],
        articles=[_at(minutes=10)],
    )
    # Older material arrived an hour ago, so the 12-hour lag rule alone reads OK.
    brain = _brain(["news:synthesis:monitor:6"], _at(hours=1))

    r = hc.check_news(brain, news_db=news, now=NOW)

    assert r["status"] == "STALE"
    assert r["unarrived"] == _at(hours=3)


def test_a_digest_still_inside_its_cycle_is_not_owed_yet(hc, tmp_path):
    news = _news(
        tmp_path / "news.db",
        [
            (7, "monitor", _at(hours=3), '{"summary": "x"}'),
            (8, "digest", _at(hours=1), '{"summary": "x"}'),
        ],
    )
    brain = _brain(["news:synthesis:monitor:7"], _at(hours=3))

    assert hc.check_news(brain, news_db=news, now=NOW)["status"] == "OK"


@pytest.mark.parametrize(
    "digest",
    [
        (7, "monitor", _at(hours=3), "  \n"),  # no synthesis: news-sync stages nothing
        (7, "elsewhere", _at(hours=3), '{"summary": "x"}'),  # a pipeline news-sync skips
    ],
)
def test_a_digest_news_sync_never_stages_is_not_owed(hc, tmp_path, digest):
    news = _news(tmp_path / "news.db", [digest])
    brain = _brain(["news:synthesis:monitor:1"], _at(hours=4))

    assert hc.check_news(brain, news_db=news, now=NOW)["status"] == "OK"


def test_the_report_says_which_digest_is_missing(hc, tmp_path):
    row = {
        "name": "News",
        "total": 3,
        "age": timedelta(hours=1),
        "status": "STALE",
        "stale": True,
        "unarrived": _at(hours=3),
    }

    text, issues = hc.build_report([row], {}, {}, {}, [])

    line = next(line for line in text.splitlines() if line.strip().startswith("News"))
    assert "a digest written 2026-10-20T17:50 is not in the store" in line
    assert "News: STALE" in issues


def test_the_grace_covers_one_staging_cycle_and_its_load(hc):
    """Staging every two hours with two minutes of jitter, then at most ten minutes of
    lock wait and a few of load: 2 h 17 min. The grace sits just above it."""
    assert timedelta(hours=2, minutes=17) < hc.NEWS_ARRIVAL_GRACE <= timedelta(hours=3)
