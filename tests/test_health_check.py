"""
Tests for the nightly health check.

Regression cover for the scheduled-job resolution bug. Two platforms run this
script from the same repo:
  - macOS (Local): launchd. A bare ["launchctl", ...] raised FileNotFoundError
    when PATH lacked /bin (run outside sb-health-check.sh) → every job showed
    ERROR and auto_fix() was disabled.
  - Linux (VPS): there is no launchctl at all; jobs are systemd --user units, so
    the launchd path errored for *every* job unconditionally.
check_jobs() now dispatches per-platform; these tests guard both.
"""

import importlib.util
import os
import sys
from datetime import UTC
from pathlib import Path

import pytest

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"


@pytest.fixture
def hc():
    """Load scripts/health_check.py as a module (it has no package __init__)."""
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader, f"could not load {HEALTH_CHECK_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(sys.platform != "darwin", reason="launchctl is macOS-only")
def test_launchctl_bin_absolute_even_without_bin_in_path(hc, monkeypatch):
    """Original bug: PATH lacking /bin broke bare 'launchctl'."""
    monkeypatch.setenv("PATH", "")
    path = hc.launchctl_bin()
    assert os.path.isabs(path), f"expected absolute launchctl path, got {path!r}"
    assert os.path.exists(path), f"launchctl not found at {path!r}"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemctl is Linux-only")
def test_systemctl_bin_absolute_even_without_bin_in_path(hc, monkeypatch):
    monkeypatch.setenv("PATH", "")
    path = hc.systemctl_bin()
    assert os.path.isabs(path), f"expected absolute systemctl path, got {path!r}"
    assert os.path.exists(path), f"systemctl not found at {path!r}"


def test_check_jobs_covers_all_logical_jobs_without_filenotfound(hc, monkeypatch):
    """End-to-end regression on the active platform: every logical job is
    reported with a status, and none degrades to the bare-binary
    FileNotFoundError — even with /bin stripped from PATH."""
    monkeypatch.setenv("PATH", "")
    jobs = hc.check_jobs()
    descs = {info["desc"] for info in jobs.values()}
    assert "Hourly Outlook sync" in descs
    assert "Daily full sync" in descs
    assert len(jobs) == 9
    for label, info in jobs.items():
        assert "No such file or directory" not in str(info.get("status", "")), (
            f"{label} hit a bare-binary FileNotFoundError"
        )


def test_age_handles_utc_and_naive(hc):
    """date_received is naive-local on macOS but tz-aware UTC ('Z') on the VPS.
    Both must yield the true elapsed time — the original bug compared a UTC 'Z'
    value against local now(), inflating VPS ages by the UTC offset → false STALE."""
    from datetime import datetime, timedelta

    lo, hi = timedelta(hours=1, minutes=50), timedelta(hours=2, minutes=10)

    naive = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    assert lo < hc._age(naive) < hi

    utc_z = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert lo < hc._age(utc_z) < hi, "UTC 'Z' age must not be inflated by the offset"

    assert hc._age(None) is None


def test_loader_job_matches_platform(hc):
    """The stale-data backstop must target the right loader identifier per OS."""
    if sys.platform == "darwin":
        assert hc.IS_MACOS is True
        assert hc.LOADER_JOB == f"{hc.LABEL_PREFIX}-sync"
    else:
        assert hc.IS_MACOS is False
        assert hc.LOADER_JOB == "sb-daily-sync.service"


# --- SharePoint token expiry -------------------------------------------------
# Regression cover for a silent 3-day rot: the Mac's sharepoint-session.json
# expired (tokenExpiresAt in the past) while the file mtime stayed fresh (the
# token-sync job kept copying the dead token). Email kept working, SharePoint
# fetching failed with SHAREPOINT_SESSION_MISSING, and nothing alerted. The
# health check must now read tokenExpiresAt and surface expiry as an issue.


def _write_sp_session(dir_path, expires_at):
    import json

    p = dir_path / "sharepoint-session.json"
    p.write_text(json.dumps({"host": "contoso.sharepoint.com", "tokenExpiresAt": expires_at}))
    return p


def test_sharepoint_token_expired_is_fail(hc, tmp_path):
    from datetime import datetime

    now = datetime(2026, 7, 3, tzinfo=UTC)
    p = _write_sp_session(tmp_path, "2026-06-30T18:37:43.000Z")
    assert hc.check_sharepoint_token(path=p, now=now)["status"] == "FAIL"


def test_sharepoint_token_valid_is_ok(hc, tmp_path):
    from datetime import datetime

    now = datetime(2026, 7, 3, tzinfo=UTC)
    p = _write_sp_session(tmp_path, "2026-07-10T00:00:00.000Z")
    assert hc.check_sharepoint_token(path=p, now=now)["status"] == "OK"


def test_sharepoint_token_expiring_soon_is_warn(hc, tmp_path):
    from datetime import datetime

    now = datetime(2026, 7, 3, tzinfo=UTC)
    p = _write_sp_session(tmp_path, "2026-07-03T06:00:00.000Z")  # 6h out
    assert hc.check_sharepoint_token(path=p, now=now)["status"] == "WARN"


def test_sharepoint_token_missing_is_na(hc, tmp_path):
    assert hc.check_sharepoint_token(path=tmp_path / "nope.json")["status"] == "N/A"


# --- Migrated launchd jobs ---------------------------------------------------
# The Mac is now a pull-only host: 9 ingestion jobs were disabled (renamed to
# *.plist.disabled-migrated-to-vps) when ingestion moved to the VPS. They must
# report as MIGRATED (informational), not NOT_LOADED (which read as a fault).


def test_is_migrated_true_when_only_disabled_plist(hc, tmp_path):
    label = "com.secondbrain.sync"
    (tmp_path / f"{label}.plist.disabled-migrated-to-vps").write_text("x")
    assert hc._is_migrated(label, agents_dir=tmp_path) is True


def test_is_migrated_false_when_active_plist_present(hc, tmp_path):
    label = "com.secondbrain.db-pull"
    (tmp_path / f"{label}.plist").write_text("x")
    (tmp_path / f"{label}.plist.disabled-migrated-to-vps").write_text("x")
    assert hc._is_migrated(label, agents_dir=tmp_path) is False


def test_is_migrated_false_when_nothing(hc, tmp_path):
    assert hc._is_migrated("com.secondbrain.nope", agents_dir=tmp_path) is False


def test_health_check_is_parameterized():
    """health_check.py must read identity/paths from env/config, not hardcode them
    (it ships to the public repo). Positive checks avoid embedding PII literals here."""
    src = HEALTH_CHECK_PATH.read_text()
    assert "HEALTH_EMAIL_TO" in src, "recipient must come from env, not a literal"
    assert "DEFAULT_DB" in src, "DB path must come from config (DATA_ROOT)"
    assert "LABEL_PREFIX" in src, "launchd labels must be prefix-parameterized"


# --- Attachment "pending" is actionable-only ---------------------------------
# Rows whose text extraction was skipped (no text) or failed (corrupt/encrypted)
# also carry llm_status='pending' but can never be summarized. Counting them
# inflated the reported backlog to thousands of phantom items. "pending" must
# mean the real Phase-2 queue: extracted text still awaiting an LLM summary.


def test_check_attachments_pending_is_actionable_only(hc):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE attachment_content (id INTEGER PRIMARY KEY, "
        "extraction_status TEXT, llm_status TEXT, llm_extracted_at TEXT)"
    )
    rows = (
        [("extracted", "extracted", "2026-07-03T10:00:00")] * 3  # done
        + [("skipped", "pending", None)] * 2  # no text -> terminal
        + [("failed", "pending", None)] * 1  # corrupt -> terminal
        + [("extracted", "pending", None)] * 2  # actionable queue
        + [("extracted", "failed", None)] * 1  # llm failed
    )
    db.executemany(
        "INSERT INTO attachment_content "
        "(extraction_status, llm_status, llm_extracted_at) VALUES (?,?,?)",
        rows,
    )
    db.commit()
    r = hc.check_attachments(db)
    assert r["llm_pending"] == 2, "pending must exclude no-text/corrupt terminal rows"
    assert r["llm_no_text"] == 3
    assert r["llm_failed"] == 1
    assert r["llm_done"] == 3


def _images_db():
    """In-memory DB with the tables check_images reads."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE inline_images (id INTEGER PRIMARY KEY, vision_description TEXT)")
    db.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY)")
    db.execute(
        "CREATE TABLE attachments (id INTEGER PRIMARY KEY, email_id INTEGER, message_id TEXT, "
        "mime_type TEXT, file_path TEXT)"
    )
    db.execute("CREATE TABLE inline_image_occurrences (message_id TEXT)")
    return db


def _queue(db, pending, done):
    """Seed `pending` never-processed image attachments and `done` processed ones,
    each joined to a real email row (the pipeline only sees joinable rows)."""
    email_id = 0
    for i in range(pending):
        email_id += 1
        db.execute("INSERT INTO emails (id) VALUES (?)", (email_id,))
        db.execute(
            "INSERT INTO attachments (email_id, message_id, mime_type, file_path) VALUES (?,?,?,?)",
            (email_id, f"pending-{i}", "image/png", f"/tmp/p{i}.png"),
        )
    for i in range(done):
        email_id += 1
        db.execute("INSERT INTO emails (id) VALUES (?)", (email_id,))
        db.execute(
            "INSERT INTO attachments (email_id, message_id, mime_type, file_path) VALUES (?,?,?,?)",
            (email_id, f"done-{i}", "image/png", f"/tmp/d{i}.png"),
        )
        db.execute("INSERT INTO inline_image_occurrences (message_id) VALUES (?)", (f"done-{i}",))
    db.commit()


def test_check_images_excludes_orphaned_attachments(hc):
    """run_backfill JOINs attachments to emails, so an attachment with no email row
    can never be picked up. Counting it inflates the queue permanently and would
    eventually trip the WARN for work that cannot drain — on prod this was 91
    reported vs 2 actually reachable."""
    db = _images_db()
    _queue(db, pending=2, done=0)
    # Orphan: valid message_id and file, but no joinable email row.
    db.execute(
        "INSERT INTO attachments (email_id, message_id, mime_type, file_path) "
        "VALUES (NULL, 'orphan', 'image/png', '/tmp/o.png')"
    )
    db.commit()

    assert hc.check_images(db)["pending"] == 2


def test_check_images_reports_pending_queue_depth(hc):
    """Step 8 drains a time-boxed slice per sync run, so the queue can grow between
    runs. The report must show how much work is WAITING, not just coverage % — the
    old row showed 88% classified every day while the queue sat 200 deep."""
    db = _images_db()
    db.execute("INSERT INTO inline_images (vision_description) VALUES ('a cat')")
    _queue(db, pending=7, done=3)

    r = hc.check_images(db)

    assert r["pending"] == 7, "queue depth = image attachments never run through the pipeline"


def test_check_images_ignores_non_image_and_pathless_attachments(hc):
    """Queue depth must use the same predicate run_backfill does, or the report
    counts work the pipeline will never pick up."""
    db = _images_db()
    db.execute(
        "INSERT INTO attachments (message_id, mime_type, file_path) "
        "VALUES ('a', 'application/pdf', '/tmp/a.pdf')"
    )
    db.execute(
        "INSERT INTO attachments (message_id, mime_type, file_path) VALUES ('b', 'image/png', NULL)"
    )
    db.commit()

    assert hc.check_images(db)["pending"] == 0


def test_check_images_warns_when_queue_stops_draining(hc):
    """A queue deeper than the sync can clear is degradation, not an outage: WARN
    (visible in the report) rather than FAIL (pages as if something is broken)."""
    db = _images_db()
    db.execute("INSERT INTO inline_images (vision_description) VALUES ('a cat')")
    _queue(db, pending=hc.IMAGE_QUEUE_WARN + 1, done=0)

    assert hc.check_images(db)["status"] == "WARN"


def test_check_images_ok_while_queue_is_draining(hc):
    """A transient spike after a heavy news day self-corrects on a quiet one — it
    must not warn, or the report nags on every busy day and gets ignored."""
    db = _images_db()
    db.execute("INSERT INTO inline_images (vision_description) VALUES ('a cat')")
    _queue(db, pending=hc.IMAGE_QUEUE_WARN - 1, done=0)

    assert hc.check_images(db)["status"] == "OK"


# --- Source-side freshness ---------------------------------------------------
# The daily reverse-ingest job reported OK every day while its document roots
# were a frozen copy (newest real document two months old). Every check passed:
# the job ran, and the rows it wrote were recent. Monitoring asked "did the job
# run?" and "is the newest DB row recent?" — never "is the SOURCE still
# receiving new material?". check_document_roots and check_news ask that.


def _touch(path, days_old):
    """Create a file whose mtime is `days_old` days in the past.
    A negative `days_old` puts the mtime in the future (clock skew)."""
    from datetime import datetime, timedelta

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    ts = (datetime.now() - timedelta(days=days_old)).timestamp()
    os.utime(path, (ts, ts))
    return path


def _no_stamp(tmp_path):
    """A stamp path that does not exist → forces the mtime fallback, so these
    tests never depend on the real ~/.second-brain/document-sync.stamp."""
    return tmp_path / "absent-document-sync.stamp"


def test_source_freshness_thresholds_are_registered(hc):
    from datetime import timedelta

    assert hc.STALE_THRESHOLDS["document_roots"] == timedelta(days=14)
    assert hc.STALE_THRESHOLDS["news"] == timedelta(hours=12)


def test_check_document_roots_flags_frozen_source(hc, tmp_path):
    """The defect: newest input is two months old while the job keeps running."""
    root = tmp_path / "docs"
    _touch(root / "units" / "old.pdf", 61)
    r = hc.check_document_roots(roots=[root], stamp=_no_stamp(tmp_path))
    assert r["status"] == "STALE"
    assert r["total"] == 1
    assert 60 < r["age"].days < 62


def test_check_document_roots_fresh_is_ok(hc, tmp_path):
    root = tmp_path / "docs"
    _touch(root / "new.docx", 1)
    _touch(root / "sub" / "ancient.pdf", 400)
    r = hc.check_document_roots(roots=[root], stamp=_no_stamp(tmp_path))
    assert r["status"] == "OK"
    assert r["total"] == 2
    assert r["age"].days == 1


def test_check_document_roots_ignores_non_ingestable_files(hc, tmp_path):
    """A fresh .png/.json must not mask a frozen source — only the extensions
    cmd_reverse_ingest actually ingests count as input."""
    root = tmp_path / "docs"
    _touch(root / "old.pdf", 61)
    _touch(root / "fresh.png", 0)
    _touch(root / "fresh.json", 0)
    r = hc.check_document_roots(roots=[root], stamp=_no_stamp(tmp_path))
    assert r["total"] == 1
    assert r["status"] == "STALE"


def test_check_document_roots_ignores_future_mtimes(hc, tmp_path):
    """A clock-skewed file dated in the future must not mask a frozen source.
    Without the guard its negative age becomes the "newest input" and the check
    reports OK forever — exactly the condition it exists to detect."""
    root = tmp_path / "docs"
    _touch(root / "old.pdf", 61)
    _touch(root / "skewed.pdf", -30)  # mtime 30 days ahead
    r = hc.check_document_roots(roots=[root], stamp=_no_stamp(tmp_path))
    assert r["total"] == 2, "the skewed file is still a countable input"
    assert 60 < r["age"].days < 62, "newest usable input is still two months old"
    assert r["status"] == "STALE"


def test_check_document_roots_missing_root_is_warn(hc, tmp_path):
    present = tmp_path / "present"
    _touch(present / "fresh.md", 0)
    r = hc.check_document_roots(roots=[present, tmp_path / "gone"], stamp=_no_stamp(tmp_path))
    assert r["status"] == "WARN"
    assert r["missing"] == ["gone"]
    per_root = {x["name"]: x for x in r["roots"]}
    assert per_root["present"]["files"] == 1
    assert per_root["gone"]["files"] == 0


def test_check_document_roots_empty_root_is_warn(hc, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    r = hc.check_document_roots(roots=[root], stamp=_no_stamp(tmp_path))
    assert r["status"] == "WARN"
    assert r["total"] == 0
    assert r["age"] is None


def test_check_document_roots_defaults_match_reverse_ingest(hc):
    """Defaults must be the roots cmd_reverse_ingest scans, else the check
    watches a different source than the job it is meant to police."""
    assert [p.name for p in hc.DOCUMENT_ROOTS] == ["National", "Personal"]
    assert all(p.parent == Path.home() / "Documents" for p in hc.DOCUMENT_ROOTS)


# --- Document sync heartbeat -------------------------------------------------
# Newest-mtime alone can be manufactured by our own jobs: curate-docs writes
# files INTO the same roots, so the organic source (the laptop push) can freeze
# while the newest mtime stays fresh. The push job now writes a stamp file after
# every successful push; when present it is the authoritative liveness signal.


def _write_stamp(path, now, **delta):
    from datetime import timedelta

    path.write_text((now - timedelta(**delta)).isoformat() + "\n")
    return path


def test_document_sync_stamp_path_is_registered(hc):
    assert hc.DOCUMENT_SYNC_STAMP == Path.home() / ".second-brain" / "document-sync.stamp"


def test_check_document_roots_fresh_stamp_beats_frozen_mtimes(hc, tmp_path):
    """Not the point of the stamp, but the inverse must hold too: a genuinely
    recent push keeps the check OK even if no ingestable file changed."""
    from datetime import datetime

    root = tmp_path / "docs"
    _touch(root / "old.pdf", 61)
    now = datetime.now(UTC)  # after the touches, so no mtime lands in the future
    stamp = _write_stamp(tmp_path / "document-sync.stamp", now, hours=2)
    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp)
    assert r["status"] == "OK"
    assert r["total"] == 1, "file counts stay reported"
    assert 60 < r["age"].days < 62, "newest-input age stays reported"
    assert r["stamp_age"].total_seconds() < 3 * 3600


def test_check_document_roots_stale_stamp_beats_fresh_mtimes(hc, tmp_path):
    """The defect: curate-docs refreshes files under the roots, so a fresh mtime
    proves nothing. A 20-day-old push is STALE regardless."""
    from datetime import datetime

    root = tmp_path / "docs"
    _touch(root / "202608061200_curated.md", 0)
    now = datetime.now(UTC)  # after the touches, so no mtime lands in the future
    stamp = _write_stamp(tmp_path / "document-sync.stamp", now, days=20)
    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp)
    assert r["status"] == "STALE"
    assert r["stale"] is True
    assert r["stamp_age"].days == 20
    assert r["age"].days == 0, "newest-input age stays reported"


def test_check_document_roots_missing_stamp_falls_back_to_mtimes(hc, tmp_path):
    root = tmp_path / "docs"
    _touch(root / "old.pdf", 61)
    r = hc.check_document_roots(roots=[root], stamp=_no_stamp(tmp_path))
    assert r["status"] == "STALE"
    assert r["stamp_age"] is None
    assert "mtime" in r["note"], "the report must say the stamp is missing"

    _touch(root / "new.pdf", 0)
    r2 = hc.check_document_roots(roots=[root], stamp=_no_stamp(tmp_path))
    assert r2["status"] == "OK"


def test_check_document_roots_unparseable_stamp_falls_back_to_mtimes(hc, tmp_path):
    root = tmp_path / "docs"
    _touch(root / "old.pdf", 61)
    stamp = tmp_path / "document-sync.stamp"
    stamp.write_text("not a timestamp\n")
    r = hc.check_document_roots(roots=[root], stamp=stamp)
    assert r["status"] == "STALE"
    assert r["stamp_age"] is None
    assert "mtime" in r["note"]


def test_report_names_the_liveness_signal(hc):
    from datetime import timedelta

    stale_by_stamp = {
        "name": "Doc Roots",
        "total": 860,
        "age": timedelta(minutes=5),
        "stamp_age": timedelta(days=20),
        "stale": True,
        "missing": [],
        "roots": [{"name": "docs", "files": 860, "age": timedelta(minutes=5)}],
        "status": "STALE",
    }
    text, issues = hc.build_report([stale_by_stamp], {}, {}, {}, [])
    assert "20d" in text, "the stamp age, not the manufactured mtime, explains STALE"
    assert "source may be disconnected" in text
    assert "Doc Roots: STALE" in issues

    no_stamp = dict(stale_by_stamp, stamp_age=None, note="no sync stamp — using file mtimes")
    text2, _ = hc.build_report([no_stamp], {}, {}, {}, [])
    assert "no sync stamp" in text2


def _news_db(path, articles=(), digests=()):
    """Minimal stand-in for the upstream news DB (only the columns read)."""
    import sqlite3

    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE articles (url TEXT PRIMARY KEY, published_at TEXT)")
    con.execute("CREATE TABLE digests (id INTEGER PRIMARY KEY, created_at TEXT)")
    con.executemany(
        "INSERT INTO articles (url, published_at) VALUES (?, ?)",
        [(f"https://example.invalid/{i}", ts) for i, ts in enumerate(articles)],
    )
    con.executemany("INSERT INTO digests (created_at) VALUES (?)", [(ts,) for ts in digests])
    con.commit()
    con.close()
    return path


def _brain_db(news_rows=()):
    """Brain DB with the given News-mailbox rows plus one unrelated row."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE emails (message_id INTEGER PRIMARY KEY, "
        "date_received TEXT, mailbox_name TEXT)"
    )
    db.execute("INSERT INTO emails (date_received, mailbox_name) VALUES ('2026-08-06', 'Inbox')")
    db.executemany(
        "INSERT INTO emails (date_received, mailbox_name) VALUES (?, 'News')",
        [(ts,) for ts in news_rows],
    )
    db.commit()
    return db


def _ago(now, **delta):
    from datetime import timedelta

    return (now - timedelta(**delta)).isoformat()


def test_check_news_stale_when_upstream_moved_on(hc, tmp_path):
    from datetime import datetime, timedelta

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    news = _news_db(
        tmp_path / "news.db",
        articles=[_ago(now, hours=1), _ago(now, days=5)],
        digests=[_ago(now, hours=2)],
    )
    r = hc.check_news(_brain_db([_ago(now, days=3)]), news_db=news, now=now)
    assert r["status"] == "STALE"
    assert r["total"] == 1, "counts only mailbox_name = 'News' rows"
    assert r["lag"] > timedelta(hours=12)


def test_check_news_ignores_future_published_at(hc, tmp_path):
    """Some digest-pipeline rows carry published_at months in the future; a
    naive MAX() would pin this check to permanent STALE."""
    from datetime import datetime

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    news = _news_db(
        tmp_path / "news.db",
        articles=[_ago(now, days=-69), _ago(now, hours=2)],
        digests=[_ago(now, hours=3)],
    )
    r = hc.check_news(_brain_db([_ago(now, hours=1)]), news_db=news, now=now)
    assert r["status"] == "OK"
    assert r["upstream_latest"] == _ago(now, hours=2)


def test_check_news_within_threshold_is_ok(hc, tmp_path):
    from datetime import datetime

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    news = _news_db(tmp_path / "news.db", articles=[_ago(now, hours=1)])
    r = hc.check_news(_brain_db([_ago(now, hours=7)]), news_db=news, now=now)
    assert r["status"] == "OK"


def test_check_news_digest_alone_can_trip_stale(hc, tmp_path):
    """digests.created_at is the high-signal artifact — it counts as upstream
    material even when no newer article exists."""
    from datetime import datetime

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    news = _news_db(
        tmp_path / "news.db",
        articles=[_ago(now, days=4)],
        digests=[_ago(now, minutes=30)],
    )
    r = hc.check_news(_brain_db([_ago(now, days=4)]), news_db=news, now=now)
    assert r["status"] == "STALE"


def test_check_news_missing_db_is_informational(hc, tmp_path):
    """No upstream on this host is not a fault — N/A, like SharePoint without a
    session file. A WARN here would put every nightly report into ISSUES."""
    from datetime import datetime

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    r = hc.check_news(_brain_db([_ago(now, days=9)]), news_db=tmp_path / "nope.db", now=now)
    assert r["status"] == "N/A"
    assert not r["stale"]


def test_check_news_unreadable_db_is_warn(hc, tmp_path):
    """A corrupt upstream file IS a fault — it stays a WARN."""
    from datetime import datetime

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    corrupt = tmp_path / "news.db"
    corrupt.write_bytes(b"this is not a sqlite database")
    r = hc.check_news(_brain_db([_ago(now, hours=1)]), news_db=corrupt, now=now)
    assert r["status"] == "WARN"


def test_check_news_not_backfilled_yet_is_informational(hc, tmp_path):
    """Zero ingested News rows means the backfill has not run yet — a steady
    state, not an issue, so it must not fire --email-if-issues every night."""
    from datetime import datetime

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    news = _news_db(tmp_path / "news.db", articles=[_ago(now, minutes=10)])
    r = hc.check_news(_brain_db(), news_db=news, now=now)
    assert r["status"] == "N/A"
    assert r["total"] == 0
    assert r["note"] == "no News rows ingested yet"


def test_check_news_empty_upstream_is_informational(hc, tmp_path):
    from datetime import datetime

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    news = _news_db(tmp_path / "news.db")
    r = hc.check_news(_brain_db([_ago(now, hours=1)]), news_db=news, now=now)
    assert r["status"] == "N/A"
    assert not r["stale"]


def test_report_keeps_pending_news_backfill_out_of_issues(hc):
    """The steady state must still print ALL SYSTEMS HEALTHY."""
    checks = [
        {
            "name": "News",
            "total": 0,
            "age": None,
            "stale": False,
            "status": "N/A",
            "note": "no News rows ingested yet",
        }
    ]
    text, issues = hc.build_report(checks, {}, {}, {}, [])
    assert issues == []
    assert "no News rows ingested yet" in text
    assert "ALL SYSTEMS HEALTHY" in text


def test_report_still_flags_genuine_news_staleness(hc):
    """The other direction: rows exist and upstream moved past the threshold."""
    from datetime import timedelta

    checks = [
        {
            "name": "News",
            "total": 120,
            "age": timedelta(hours=30),
            "lag": timedelta(hours=18),
            "stale": True,
            "status": "STALE",
        }
    ]
    text, issues = hc.build_report(checks, {}, {}, {}, [])
    assert issues == ["News: STALE"]
    assert "ALL SYSTEMS HEALTHY" not in text


# --- News DB path comes from config ------------------------------------------
# health_check.py hardcoded its own NEWS_DB while src/config.py honours
# BRAIN_NEWS_DB. With the override set, news-sync wrote one file while the
# health check watched another — monitoring a path nobody writes.


def _reload_hc(monkeypatch, **env):
    """Re-import health_check (and src.config) with the given env applied."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delitem(sys.modules, "src.config", raising=False)
    spec = importlib.util.spec_from_file_location("health_check_reloaded", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_news_db_default_follows_config_override(monkeypatch, tmp_path):
    override = tmp_path / "elsewhere" / "news.db"
    reloaded = _reload_hc(monkeypatch, BRAIN_NEWS_DB=str(override))
    assert reloaded.NEWS_DB == override


def test_check_news_reads_the_configured_news_db(monkeypatch, tmp_path):
    """Without news_db= the check must follow BRAIN_NEWS_DB, not a hardcoded path."""
    from datetime import datetime

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    override = tmp_path / "elsewhere" / "news.db"
    reloaded = _reload_hc(monkeypatch, BRAIN_NEWS_DB=str(override))
    r = reloaded.check_news(_brain_db([_ago(now, hours=1)]), now=now)
    assert r["note"] == "news db not found"


def test_report_surfaces_frozen_source_and_news_lag(hc):
    from datetime import timedelta

    checks = [
        {
            "name": "Doc Roots",
            "total": 4321,
            "age": timedelta(days=61),
            "stale": True,
            "missing": [],
            "roots": [{"name": "docs", "files": 4321, "age": timedelta(days=61)}],
            "status": "STALE",
        },
        {
            "name": "News",
            "total": 120,
            "age": timedelta(hours=30),
            "lag": timedelta(hours=18),
            "stale": True,
            "status": "STALE",
        },
    ]
    text, issues = hc.build_report(checks, {}, {}, {}, [])
    assert "Doc Roots" in text
    assert "source may be disconnected" in text
    assert "61d" in text
    assert "18h" in text
    assert "Doc Roots: STALE" in issues
    assert "News: STALE" in issues


def test_new_source_checks_are_registered_in_main():
    """Both checks must run in the nightly report, not just exist."""
    src = HEALTH_CHECK_PATH.read_text()
    assert "check_document_roots()," in src
    assert "check_news(db)," in src


def _emails_db(rows):
    """Brain DB shaped like the real emails table: message_id is TEXT-affinity
    in production (49k integer ids alongside 13k text ids), so tests must be
    able to insert both kinds.

    rows: (message_id, date_received, mailbox_name) tuples.
    """
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE emails (message_id, date_received TEXT, mailbox_name TEXT)")
    db.executemany("INSERT INTO emails VALUES (?, ?, ?)", rows)
    db.commit()
    return db


def test_check_emails_ignores_news_rows(hc):
    """News rows must not silence the Emails staleness alarm.

    `message_id > 0` was written to exclude synthetic sources — reverse-ingest
    documents use NEGATIVE integer ids so they fail it. But SQLite ranks TEXT
    above INTEGER, so 'news:article:x' > 0 is TRUE: a single fresh news row
    would make stale mail look current and disable the alarm (and the loader
    auto-fix keyed off it) forever.
    """
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    stale_mail = (now - timedelta(days=3)).isoformat()
    fresh_news = now.isoformat()

    without_news = hc.check_emails(_emails_db([(1000, stale_mail, "Archive")]))
    assert without_news["status"] == "STALE", "3-day-old mail is stale by the 6h threshold"

    with_news = hc.check_emails(
        _emails_db(
            [
                (1000, stale_mail, "Archive"),
                ("news:article:deadbeef", fresh_news, "News"),
            ]
        )
    )
    assert with_news["status"] == "STALE", "a fresh news row must not mask stale mail"
    assert with_news["total"] == without_news["total"], "news rows must not inflate the count"
    assert with_news["recent_24h"] == 0


def test_check_document_roots_ignores_future_stamp(hc, tmp_path):
    """A clock-skewed stamp must not pin the check to OK.

    Same hole the mtime branch already clamps: one bad write from the push job
    would silently disable the liveness signal it exists to provide.
    """
    from datetime import datetime, timedelta

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    root = tmp_path / "National"
    root.mkdir()
    old = root / "frozen.pdf"
    old.write_text("x")
    os.utime(old, ((now - timedelta(days=90)).timestamp(),) * 2)

    stamp = tmp_path / "document-sync.stamp"
    stamp.write_text((now + timedelta(days=5)).isoformat())

    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp)
    assert r["status"] == "STALE", "future stamp is unusable — fall back to the 90d-old mtime"


def test_check_document_roots_survives_unreadable_stamp_bytes(hc, tmp_path):
    """main() has no try/except around the checks, so an exception here kills
    the whole nightly report and no email goes out."""
    from datetime import datetime, timedelta

    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    root = tmp_path / "National"
    root.mkdir()
    fresh = root / "doc.pdf"
    fresh.write_text("x")
    os.utime(fresh, ((now - timedelta(days=1)).timestamp(),) * 2)

    stamp = tmp_path / "document-sync.stamp"
    stamp.write_bytes(b"\xff\xfe not utf-8")

    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp)
    assert r["status"] == "OK", "undecodable stamp falls back to mtimes, never raises"
