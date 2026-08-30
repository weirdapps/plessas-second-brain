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
from datetime import UTC, timedelta
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
    # Derived, not hardcoded: the two registries legitimately differ in size.
    # The VPS gained news-sync, conversation-sync and the health check itself,
    # while the Mac's launchd side stayed at the nine jobs that were migrated
    # away — a literal here would fail on whichever platform it was not written
    # for, and CI runs the systemd one.
    expected = hc.LAUNCHD_JOBS if hc.IS_MACOS else hc.SYSTEMD_UNITS
    assert len(jobs) == len(expected)
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


# --- SharePoint link tally ---------------------------------------------------
# FetchStatus (src/export/sharepoint_fetcher.py) has five members, but the tally
# summed only ok+exception+stale. 'http-error' — the status every HTTP failure
# maps to — was dropped from the denominator, so prod printed "973/996" against
# 1,016 real rows and called 20 unfetched links "0 exceptions". The tally must
# be derived from the rows present, not from a hand-listed subset, so a status
# added upstream later cannot silently vanish again.


def _sp_db(attempted_days_ago=0, attempts=1, **status_counts):
    """sharepoint_links as prod shapes it.

    `fetched_at` is set only on 'ok' rows — it records a SUCCESSFUL fetch, so a
    link that has never come back clean keeps it NULL no matter how many times
    it was tried. `attempts`/`last_attempt_at` drive the retry throttle in
    src/export/sharepoint_fetcher.py.
    """
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE sharepoint_links (url TEXT, last_status TEXT, attempts INT, "
        "last_attempt_at TEXT, fetched_at TEXT)"
    )
    for status, n in status_counts.items():
        last_status = status.replace("_", "-")
        for i in range(n):
            db.execute(
                "INSERT INTO sharepoint_links (url, last_status, attempts, last_attempt_at, "
                "fetched_at) VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now',?),?)",
                (
                    f"https://x/{status}/{i}",
                    last_status,
                    attempts,
                    f"-{attempted_days_ago} days",
                    "2026-01-01T00:00:00Z" if last_status == "ok" else None,
                ),
            )
    db.commit()
    return db


def test_check_sharepoint_total_counts_every_status(hc):
    """The prod bug: http-error rows existed but were absent from the total."""
    db = _sp_db(ok=973, http_error=20, stale=23)

    r = hc.check_sharepoint(db)

    assert r["total"] == 1016, "denominator must be every row, not a listed subset"
    assert r["ok"] == 973


def test_check_sharepoint_reports_unfetched_links(hc):
    """'0 exceptions' beside 43 unfetched links is a true statement that misleads.
    The report needs the count of links that are not ok, whatever their status."""
    db = _sp_db(ok=973, http_error=20, stale=23)

    assert hc.check_sharepoint(db)["failed"] == 43


def test_check_sharepoint_counts_unknown_status_as_failed(hc):
    """Fail-closed: a status this function has never heard of is a problem until
    proven otherwise. Fail-open is what hid http-error for weeks."""
    db = _sp_db(ok=1, some_future_status=1)

    r = hc.check_sharepoint(db)

    assert r["total"] == 2
    assert r["failed"] == 1


def test_check_sharepoint_warns_when_most_links_are_unfetched(hc):
    db = _sp_db(ok=6, http_error=4)

    assert hc.check_sharepoint(db)["status"] == "WARN"


def test_check_sharepoint_ok_on_a_small_failure_tail(hc):
    """43-in-1,016 is a tail to watch, not a page at 23:50."""
    db = _sp_db(ok=973, http_error=20, stale=23)

    assert hc.check_sharepoint(db)["status"] == "OK"


def test_report_shows_true_sharepoint_denominator(hc):
    """End-to-end on the artefact actually read: the row must not print 973/996."""
    db = _sp_db(ok=973, http_error=20, stale=23)
    text, _ = hc.build_report([hc.check_sharepoint(db)], {}, {}, {}, [])

    assert "973/1016" in text
    assert "973/996" not in text


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


# --- Label prefix discovery --------------------------------------------------
# LABEL_PREFIX defaulted to a generic literal that matched no plist actually
# installed on this Mac, so _is_migrated() looked for the wrong filenames, never
# matched, and every one of the 9 migrated jobs reported NOT_LOADED — a fault
# reading on every manual run. The prefix must be discovered from what is
# installed, with the env var still winning for explicit control.


def _install_migrated_jobs(agents_dir, prefix):
    """Lay down the disabled plists a migrated Mac actually has."""
    for suffix in (
        ".sync",
        "-sync",
        ".noon-catchup",
        ".teams-sync",
        ".calendar-sync",
        ".attachments",
        ".auth-watch",
        ".curate-docs",
        ".reverse-ingest",
    ):
        (agents_dir / f"{prefix}{suffix}.plist.disabled-migrated-to-vps").write_text("x")


def test_detect_label_prefix_discovers_installed_prefix(hc, tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_LABEL_PREFIX", raising=False)
    _install_migrated_jobs(tmp_path, "com.example.brain")
    assert hc.detect_label_prefix(agents_dir=tmp_path) == "com.example.brain"


def test_detect_label_prefix_not_truncated_by_ambiguous_sync_suffix(hc, tmp_path, monkeypatch):
    """'.teams-sync' and '.calendar-sync' also end in '-sync'. Keying discovery
    on that suffix would yield 'com.example.brain.teams' as the prefix."""
    monkeypatch.delenv("BRAIN_LABEL_PREFIX", raising=False)
    _install_migrated_jobs(tmp_path, "com.example.brain")
    prefix = hc.detect_label_prefix(agents_dir=tmp_path)
    assert not prefix.endswith(".teams")
    assert not prefix.endswith(".calendar")


def test_detect_label_prefix_env_overrides_discovery(hc, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_LABEL_PREFIX", "com.override.brain")
    _install_migrated_jobs(tmp_path, "com.example.brain")
    assert hc.detect_label_prefix(agents_dir=tmp_path) == "com.override.brain"


def test_detect_label_prefix_falls_back_when_nothing_installed(hc, tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_LABEL_PREFIX", raising=False)
    assert hc.detect_label_prefix(agents_dir=tmp_path) == hc.DEFAULT_LABEL_PREFIX


def test_detect_label_prefix_finds_active_plists_too(hc, tmp_path, monkeypatch):
    """A Mac that still runs the jobs locally has plain .plist files."""
    monkeypatch.delenv("BRAIN_LABEL_PREFIX", raising=False)
    (tmp_path / "com.example.brain.noon-catchup.plist").write_text("x")
    (tmp_path / "com.example.brain.auth-watch.plist").write_text("x")
    assert hc.detect_label_prefix(agents_dir=tmp_path) == "com.example.brain"


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
    db.execute(
        "CREATE TABLE inline_images (id INTEGER PRIMARY KEY, vision_description TEXT, "
        "classification TEXT, classified_at TEXT, visioned_at TEXT)"
    )
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


# --- Vision-stage liveness ---------------------------------------------------
# Prod ran 20 days with MAX(visioned_at) frozen at 2026-07-27 while the report
# said OK every night: coverage sat at 88% because numerator and denominator
# froze together, and `pending` stayed at 3 because rows that reach
# inline_images already have an occurrence row. Neither signal has a time
# dimension, so a fully stopped vision stage was indistinguishable from a quiet
# one. The fix measures *aged unprocessed work* rather than a bare
# MAX(visioned_at) age, so a genuinely quiet stretch stays silent.


def _image_row(db, classification, days_old, visioned=False):
    db.execute(
        "INSERT INTO inline_images (vision_description, classification, classified_at, "
        "visioned_at) VALUES (?,?,datetime('now', ?),?)",
        (
            "a cat" if visioned else None,
            classification,
            f"-{days_old} days",
            "2026-07-27T23:08:09Z" if visioned else None,
        ),
    )
    db.commit()


def _visioned_baseline(db, n=9):
    """Healthy history, so coverage % is not what decides the status. Without it a
    one-row fixture sits at 0% classified and WARNs for the pre-existing reason,
    which would let these tests pass whether or not the new signal works."""
    for _ in range(n):
        _image_row(db, "content", days_old=30, visioned=True)


def test_check_images_warns_when_vision_stage_stops_producing(hc):
    """An image eligible for vision that has sat undescribed past the threshold
    means the stage is not running — the exact prod failure."""
    db = _images_db()
    _visioned_baseline(db)
    _image_row(db, "content", days_old=20)

    r = hc.check_images(db)

    assert r["stuck"] == 1
    assert r["status"] == "WARN"


def test_check_images_ok_when_unvisioned_work_is_still_fresh(hc):
    """Every run has in-flight images between arrival and description. Warning on
    those would make the row cry wolf nightly and get ignored."""
    db = _images_db()
    _visioned_baseline(db)
    _image_row(db, "content", days_old=0)

    r = hc.check_images(db)

    assert r["stuck"] == 0
    assert r["status"] == "OK"


def test_check_images_ignores_classes_vision_deliberately_skips(hc):
    """signature/noise are filtered out before vision by design, so they stay
    undescribed forever. Counting them would pin the row to WARN permanently."""
    db = _images_db()
    _visioned_baseline(db)
    _image_row(db, "signature", days_old=90)
    _image_row(db, "noise", days_old=90)

    r = hc.check_images(db)

    assert r["stuck"] == 0
    assert r["status"] == "OK"


def test_check_images_counts_unclassified_as_awaiting_vision(hc):
    """The rows prod accumulated were 'unclassified' — arrived, never triaged.
    Treating them as skippable would reproduce the blind spot exactly."""
    db = _images_db()
    _visioned_baseline(db)
    _image_row(db, "unclassified", days_old=20)

    assert hc.check_images(db)["stuck"] == 1


def test_check_images_counts_unrecognised_class_as_awaiting_vision(hc):
    """Fail-closed, same reasoning as the SharePoint tally: only the two classes
    known to be skipped are excluded, so a class added later is visible work."""
    db = _images_db()
    _visioned_baseline(db)
    _image_row(db, "some_new_class", days_old=20)

    assert hc.check_images(db)["stuck"] == 1


def test_check_images_reports_last_vision_output(hc):
    """The report needs the timestamp to say *how long* the stage has been dark."""
    db = _images_db()
    _image_row(db, "content", days_old=20, visioned=True)

    assert hc.check_images(db)["latest_vision"] == "2026-07-27T23:08:09Z"


# --- Coverage is a ceiling, not a shortfall ----------------------------------
# `pct` divided descriptions by EVERY inline image, including the signature and
# noise rows stage 1 short-circuits before vision is ever invoked. On prod that
# printed "87% classified" against 937 rows that were never owed, reading as a
# permanent 13% backlog that could only worsen as more signatures arrived. Worse,
# it drove a `pct < 50` WARN: the exact "alarm on work that can never drain"
# mistake already fixed for `pending`. Counts replace it.


def test_check_images_reports_skipped_rows_as_policy_not_backlog(hc):
    """The eligible population is fully described; the remainder is not owed."""
    db = _images_db()
    _image_row(db, "content", days_old=20, visioned=True)
    _image_row(db, "content", days_old=20, visioned=True)
    for _ in range(8):
        _image_row(db, "signature", days_old=90)

    r = hc.check_images(db)

    assert r["skipped"] == 8
    assert r["eligible"] == 2
    assert r["described_eligible"] == 2
    assert r["owed"] == 0, "nothing is owed when every eligible image is described"
    assert r["status"] == "OK"


def test_check_images_does_not_warn_on_a_corpus_that_is_mostly_signatures(hc):
    """The regression this replaces: enough signatures drag `pct` under 50 and the
    row goes WARN for ever, with a healthy pipeline and an empty queue."""
    db = _images_db()
    _image_row(db, "content", days_old=20, visioned=True)
    for _ in range(20):
        _image_row(db, "signature", days_old=90)

    r = hc.check_images(db)

    assert r["pct"] < 50, "fixture must reproduce the condition that used to trip the alarm"
    assert r["status"] == "OK", "coverage below 50% is not a fault when the shortfall cannot drain"


def test_check_images_still_counts_eligible_work_as_owed(hc):
    """The counts must not be a softer alarm: eligible-but-undescribed is real."""
    db = _images_db()
    _image_row(db, "content", days_old=20, visioned=True)
    _image_row(db, "unclassified", days_old=1)

    r = hc.check_images(db)

    assert r["owed"] == 1
    assert r["skipped"] == 0


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


def _no_fail(tmp_path):
    """Same isolation for the failure marker: a real ~/.second-brain/
    document-sync.fail on the developer's machine would otherwise turn every
    stamp test STALE."""
    return tmp_path / "absent-document-sync.fail"


def test_source_freshness_thresholds_are_registered(hc):
    from datetime import timedelta

    assert hc.STALE_THRESHOLDS["document_roots"] == timedelta(days=14)
    assert hc.STALE_THRESHOLDS["news"] == timedelta(hours=12)


def test_check_document_roots_flags_frozen_source(hc, tmp_path):
    """The defect: newest input is two months old while the job keeps running."""
    root = tmp_path / "docs"
    _touch(root / "units" / "old.pdf", 61)
    r = hc.check_document_roots(
        roots=[root], stamp=_no_stamp(tmp_path), fail_marker=_no_fail(tmp_path)
    )
    assert r["status"] == "STALE"
    assert r["total"] == 1
    assert 60 < r["age"].days < 62


def test_check_document_roots_fresh_is_ok(hc, tmp_path):
    root = tmp_path / "docs"
    _touch(root / "new.docx", 1)
    _touch(root / "sub" / "ancient.pdf", 400)
    r = hc.check_document_roots(
        roots=[root], stamp=_no_stamp(tmp_path), fail_marker=_no_fail(tmp_path)
    )
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
    r = hc.check_document_roots(
        roots=[root], stamp=_no_stamp(tmp_path), fail_marker=_no_fail(tmp_path)
    )
    assert r["total"] == 1
    assert r["status"] == "STALE"


def test_check_document_roots_ignores_future_mtimes(hc, tmp_path):
    """A clock-skewed file dated in the future must not mask a frozen source.
    Without the guard its negative age becomes the "newest input" and the check
    reports OK forever — exactly the condition it exists to detect."""
    root = tmp_path / "docs"
    _touch(root / "old.pdf", 61)
    _touch(root / "skewed.pdf", -30)  # mtime 30 days ahead
    r = hc.check_document_roots(
        roots=[root], stamp=_no_stamp(tmp_path), fail_marker=_no_fail(tmp_path)
    )
    assert r["total"] == 2, "the skewed file is still a countable input"
    assert 60 < r["age"].days < 62, "newest usable input is still two months old"
    assert r["status"] == "STALE"


def test_check_document_roots_missing_root_is_warn(hc, tmp_path):
    present = tmp_path / "present"
    _touch(present / "fresh.md", 0)
    r = hc.check_document_roots(
        roots=[present, tmp_path / "gone"],
        stamp=_no_stamp(tmp_path),
        fail_marker=_no_fail(tmp_path),
    )
    assert r["status"] == "WARN"
    assert r["missing"] == ["gone"]
    per_root = {x["name"]: x for x in r["roots"]}
    assert per_root["present"]["files"] == 1
    assert per_root["gone"]["files"] == 0


def test_check_document_roots_empty_root_is_warn(hc, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    r = hc.check_document_roots(
        roots=[root], stamp=_no_stamp(tmp_path), fail_marker=_no_fail(tmp_path)
    )
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
    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp, fail_marker=_no_fail(tmp_path))
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
    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp, fail_marker=_no_fail(tmp_path))
    assert r["status"] == "STALE"
    assert r["stale"] is True
    assert r["stamp_age"].days == 20
    assert r["age"].days == 0, "newest-input age stays reported"


def test_check_document_roots_missing_stamp_falls_back_to_mtimes(hc, tmp_path):
    root = tmp_path / "docs"
    _touch(root / "old.pdf", 61)
    r = hc.check_document_roots(
        roots=[root], stamp=_no_stamp(tmp_path), fail_marker=_no_fail(tmp_path)
    )
    assert r["status"] == "STALE"
    assert r["stamp_age"] is None
    assert "mtime" in r["note"], "the report must say the stamp is missing"

    _touch(root / "new.pdf", 0)
    r2 = hc.check_document_roots(
        roots=[root], stamp=_no_stamp(tmp_path), fail_marker=_no_fail(tmp_path)
    )
    assert r2["status"] == "OK"


def test_check_document_roots_unparseable_stamp_falls_back_to_mtimes(hc, tmp_path):
    root = tmp_path / "docs"
    _touch(root / "old.pdf", 61)
    stamp = tmp_path / "document-sync.stamp"
    stamp.write_text("not a timestamp\n")
    r = hc.check_document_roots(roots=[root], stamp=stamp, fail_marker=_no_fail(tmp_path))
    assert r["status"] == "STALE"
    assert r["stamp_age"] is None
    assert "mtime" in r["note"]


# --- Failing push is visible immediately -------------------------------------
# The stamp only proves when the push last SUCCEEDED, and it is judged against a
# 14-day window. On 2026-08-06 the push started failing every run (macOS TCC
# denied the launchd-spawned bash read access to ~/Documents) and 56 consecutive
# failures produced no signal at all: the stamp simply sat there aging, and the
# check reported OK for 8 days. A run that fails must say so on the next report,
# not a fortnight later. The push job records failures alongside the heartbeat.


def _write_fail(path, now, reason="DENIED reading roots", **delta):
    from datetime import timedelta

    path.write_text((now - timedelta(**delta)).isoformat() + "\n" + reason + "\n")
    return path


def test_document_sync_fail_path_is_registered(hc):
    assert hc.DOCUMENT_SYNC_FAIL == Path.home() / ".second-brain" / "document-sync.fail"


def test_check_document_roots_failing_push_caught_inside_the_stamp_window(hc, tmp_path):
    """The 8-day blind spot: stamp still inside the 14-day window, push dead."""
    from datetime import datetime

    root = tmp_path / "docs"
    _touch(root / "202608061200_curated.md", 0)
    now = datetime.now(UTC)
    stamp = _write_stamp(tmp_path / "document-sync.stamp", now, days=8)
    fail = _write_fail(tmp_path / "document-sync.fail", now, hours=2)

    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp, fail_marker=fail)

    assert r["status"] != "OK", "a push failing every run must not report OK"
    assert r["stale"] is True
    assert r["push_failing"] is True


def test_check_document_roots_failure_reason_is_reported(hc, tmp_path):
    from datetime import datetime

    root = tmp_path / "docs"
    _touch(root / "doc.pdf", 0)
    now = datetime.now(UTC)
    stamp = _write_stamp(tmp_path / "document-sync.stamp", now, days=1)
    fail = _write_fail(
        tmp_path / "document-sync.fail", now, reason="DENIED reading National", hours=1
    )

    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp, fail_marker=fail)

    assert "DENIED reading National" in r["note"], "the report must name the cause"


def test_check_document_roots_failure_superseded_by_later_success_is_ignored(hc, tmp_path):
    """A stale marker from a failure that a later run fixed must not stick."""
    from datetime import datetime

    root = tmp_path / "docs"
    _touch(root / "doc.pdf", 0)
    now = datetime.now(UTC)
    stamp = _write_stamp(tmp_path / "document-sync.stamp", now, hours=1)
    fail = _write_fail(tmp_path / "document-sync.fail", now, days=3)

    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp, fail_marker=fail)

    assert r["status"] == "OK"
    assert r["push_failing"] is False


def test_check_document_roots_absent_failure_marker_keeps_stamp_behaviour(hc, tmp_path):
    from datetime import datetime

    root = tmp_path / "docs"
    _touch(root / "doc.pdf", 0)
    now = datetime.now(UTC)
    stamp = _write_stamp(tmp_path / "document-sync.stamp", now, days=2)

    r = hc.check_document_roots(
        roots=[root], now=now, stamp=stamp, fail_marker=tmp_path / "absent.fail"
    )

    assert r["status"] == "OK"
    assert r["push_failing"] is False


def test_check_document_roots_unparseable_failure_marker_still_flags(hc, tmp_path):
    """A clobbered marker is evidence a run failed; it must not be swallowed."""
    from datetime import datetime

    root = tmp_path / "docs"
    _touch(root / "doc.pdf", 0)
    now = datetime.now(UTC)
    stamp = _write_stamp(tmp_path / "document-sync.stamp", now, days=2)
    fail = tmp_path / "document-sync.fail"
    fail.write_text("\x00not a timestamp\n")

    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp, fail_marker=fail)

    assert r["push_failing"] is True


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


def test_report_states_a_failing_push_instead_of_speculating(hc):
    """When the marker says the push failed we know the cause, so the report
    must say it — not hedge with 'source may be disconnected'."""
    from datetime import timedelta

    failing = {
        "name": "Doc Roots",
        "total": 1463,
        "age": timedelta(minutes=5),
        "stamp_age": timedelta(days=8),
        "stale": True,
        "push_failing": True,
        "note": "push FAILING: DENIED reading National",
        "missing": [],
        "roots": [{"name": "docs", "files": 1463, "age": timedelta(minutes=5)}],
        "status": "STALE",
    }
    text, issues = hc.build_report([failing], {}, {}, {}, [])
    assert "push FAILING" in text
    assert "DENIED reading National" in text
    assert "source may be disconnected" not in text, "we know the cause; do not speculate"
    assert "Doc Roots: STALE" in issues


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

    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp, fail_marker=_no_fail(tmp_path))
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

    r = hc.check_document_roots(roots=[root], now=now, stamp=stamp, fail_marker=_no_fail(tmp_path))
    assert r["status"] == "OK", "undecodable stamp falls back to mtimes, never raises"


# --- Teams chats silently dropped from ingestion -----------------------------
# check_teams reported OK on message recency alone, so it stayed green while a
# one-way ingest_disabled sweep cut the working set from 1,219 chats to 40. The
# sync then printed "0 messages inserted across 40 chats; 0 errors" every 30
# minutes for 33 hours and nothing anywhere said the corpus had shrunk.


def _teams_db(
    enabled,
    disabled,
    seed_message=True,
    message_age_days=0,
    pulled_minutes_ago=5,
    disabled_at=None,
):
    """Teams tables as prod shapes them.

    `last_pulled_at` is the sync's own heartbeat: it is stamped on every poll
    whether or not the chat produced a message, and is NULL on disabled chats
    (prod: 12 NULLs against 12 ingest_disabled rows).
    """
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE teams_messages (id INTEGER PRIMARY KEY, composed_at TEXT, chat_id INT)"
    )
    db.execute("CREATE TABLE teams_threads (id INTEGER PRIMARY KEY)")
    db.execute(
        "CREATE TABLE teams_chats (id INTEGER PRIMARY KEY, ingest_disabled INT DEFAULT 0, "
        "last_pulled_at TEXT, ingest_disabled_at TEXT)"
    )
    for _ in range(enabled):
        db.execute(
            "INSERT INTO teams_chats (ingest_disabled, last_pulled_at) VALUES "
            "(0, strftime('%Y-%m-%dT%H:%M:%SZ','now',?))",
            (f"-{pulled_minutes_ago} minutes",),
        )
    for _ in range(disabled):
        db.execute(
            "INSERT INTO teams_chats (ingest_disabled, last_pulled_at, ingest_disabled_at) "
            "VALUES (1, NULL, ?)",
            (disabled_at,),
        )
    if seed_message:
        db.execute(
            "INSERT INTO teams_messages (composed_at, chat_id) VALUES "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now',?), 1)",
            (f"-{message_age_days} days",),
        )
    db.commit()
    return db


def test_check_teams_counts_chats_dropped_from_ingestion(hc):
    db = _teams_db(enabled=40, disabled=1179)

    r = hc.check_teams(db)

    assert r["chats_disabled"] == 1179
    assert r["chats"] == 1219


def test_check_teams_warns_when_most_of_the_corpus_is_disabled(hc):
    """Fresh messages from the survivors must not mask a corpus that shrank 97%."""
    db = _teams_db(enabled=40, disabled=1179)

    assert hc.check_teams(db)["status"] == "WARN"


def test_check_teams_tolerates_a_few_genuinely_unreadable_chats(hc):
    """Archived teams and guest-only channels legitimately never read; warning on
    those would make the row nag permanently."""
    db = _teams_db(enabled=1200, disabled=19)

    assert hc.check_teams(db)["status"] == "OK"


# --- Lexical timestamp comparison against datetime('now') --------------------
# Stored timestamps use the ISO 'T' separator ('2026-08-21T14:01:01.2980000Z');
# SQLite's datetime('now', ...) renders 'YYYY-MM-DD HH:MM:SS' with a SPACE. A
# bare `col > datetime('now','-1 day')` compares the two lexically, and at
# position 11 'T' (0x54) sorts above ' ' (0x20) — so EVERY row sharing the
# cutoff's date passed regardless of its time. Prod printed "(30 today)" beside
# a Teams source silent for 28h, and inflated the email count to 108 against 35
# real arrivals. Wrapping the column in datetime() normalises every stored form
# (naive, 'Z', '+00:00', and 7 fractional digits) to the cutoff's own shape.

_YESTERDAY_MIDNIGHT = "strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day','start of day')"


def test_check_teams_recent_count_excludes_earlier_same_day_messages(hc):
    """A message from the cutoff's own date but earlier in the day is >24h old.
    Counting it is what let a dead source advertise a day of phantom arrivals."""
    db = _teams_db(enabled=10, disabled=0, seed_message=False)
    db.execute(
        f"INSERT INTO teams_messages (composed_at, chat_id) VALUES ({_YESTERDAY_MIDNIGHT}, 1)"
    )
    db.commit()

    assert hc.check_teams(db)["recent_24h"] == 0


def test_check_teams_recent_count_includes_genuinely_recent_messages(hc):
    """Positive control: the fix must not simply zero the counter."""
    db = _teams_db(enabled=10, disabled=0, seed_message=False)
    db.execute(
        "INSERT INTO teams_messages (composed_at, chat_id) VALUES "
        "(strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour'), 1)"
    )
    db.commit()

    assert hc.check_teams(db)["recent_24h"] == 1


def test_check_emails_recent_count_excludes_earlier_same_day_mail(hc):
    db = _emails_db([(1, None, None)])
    db.execute(f"UPDATE emails SET date_received = {_YESTERDAY_MIDNIGHT}")
    db.commit()

    assert hc.check_emails(db)["recent_24h"] == 0


def test_check_emails_recent_count_includes_genuinely_recent_mail(hc):
    db = _emails_db([(1, None, None)])
    db.execute("UPDATE emails SET date_received = strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 hour')")
    db.commit()

    assert hc.check_emails(db)["recent_24h"] == 1


# --- Teams: assert on the pipeline, not on the conversation ------------------
# MAX(composed_at) measures whether COLLEAGUES were talking, not whether the
# sync ran. Greek August and every weekend produce genuinely silent stretches —
# two Saturdays in Aug 2026 recorded zero messages — so a 24h message-age
# threshold pages on a healthy system and gets trained away. teams_chats
# .last_pulled_at is written by the sync itself on every poll, so it separates
# "nobody spoke" from "we stopped listening".


def test_check_teams_ok_when_corpus_is_quiet_but_sync_is_polling(hc):
    """Three silent days with every chat still polled is a quiet week, not a fault."""
    db = _teams_db(enabled=1200, disabled=12, message_age_days=3, pulled_minutes_ago=30)

    assert hc.check_teams(db)["status"] == "OK"


def test_check_teams_stale_when_the_sync_stops_polling(hc):
    """The signal that actually means broken: chats are no longer being pulled.

    The message here is FRESH on purpose. Under the old rule a recent
    composed_at cleared the row, so a sync that had stopped polling entirely
    still read OK as long as one message had landed before it died.
    """
    db = _teams_db(enabled=1200, disabled=12, message_age_days=0, pulled_minutes_ago=60 * 30)

    assert hc.check_teams(db)["status"] == "STALE"


def test_check_teams_stale_when_no_chat_was_ever_polled(hc):
    """Fail closed: chats on the books and not one heartbeat is a dead sync,
    not an absence of evidence."""
    db = _teams_db(enabled=5, disabled=0, pulled_minutes_ago=0)
    db.execute("UPDATE teams_chats SET last_pulled_at = NULL")
    db.commit()

    assert hc.check_teams(db)["status"] == "STALE"


def test_check_teams_falls_back_to_message_age_without_the_heartbeat_column(hc):
    """A pre-v18 DB has no last_pulled_at. Raising OperationalError there would
    kill the whole nightly report, so the check degrades to the old signal."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE teams_messages (id INTEGER PRIMARY KEY, composed_at TEXT)")
    db.execute("CREATE TABLE teams_threads (id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE teams_chats (id INTEGER PRIMARY KEY, ingest_disabled INT DEFAULT 0)")
    db.execute("INSERT INTO teams_chats (ingest_disabled) VALUES (0)")
    db.execute(
        "INSERT INTO teams_messages (composed_at) VALUES "
        "(strftime('%Y-%m-%dT%H:%M:%SZ','now','-4 days'))"
    )
    db.commit()

    assert hc.check_teams(db)["status"] == "STALE"


def test_check_teams_reports_pull_age_for_the_report(hc):
    db = _teams_db(enabled=10, disabled=0, pulled_minutes_ago=90)

    assert hc.check_teams(db)["pull_age"] is not None


# --- Calendar: hardcoded "OK" ------------------------------------------------
# check_calendar queried MAX(end_at) — an EVENT END date, routinely months in
# the future — then returned a literal "OK" without comparing anything. Sync
# could stop for a year and the row would stay green off events already stored.
# STALE_THRESHOLDS["calendar"] was defined and never read by anything.


def _calendar_db(ingested_days_ago=None):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE calendar_events (id INTEGER PRIMARY KEY, ingested_at TEXT, end_at TEXT)"
    )
    if ingested_days_ago is not None:
        db.execute(
            "INSERT INTO calendar_events (ingested_at, end_at) VALUES "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now',?), '2099-01-01T00:00:00Z')",
            (f"-{ingested_days_ago} days",),
        )
    db.commit()
    return db


def test_check_calendar_stale_when_ingestion_stops(hc):
    """A far-future end_at must not keep this green after the sync dies."""
    db = _calendar_db(ingested_days_ago=9)

    assert hc.check_calendar(db)["status"] == "STALE"


def test_check_calendar_fresh_is_ok(hc):
    db = _calendar_db(ingested_days_ago=0)

    assert hc.check_calendar(db)["status"] == "OK"


def test_check_calendar_exposes_age_to_the_report(hc):
    """The age column read '?' for every source that skipped this key, which is
    precisely the set of sources that could not go red."""
    db = _calendar_db(ingested_days_ago=1)

    assert hc.check_calendar(db)["age"] is not None


def test_check_calendar_empty_table_is_warn_not_ok(hc):
    """No rows at all is a broken source, not a healthy quiet one."""
    db = _calendar_db(ingested_days_ago=None)

    assert hc.check_calendar(db)["status"] != "OK"


# --- Embeddings: presence is not liveness ------------------------------------
# check_embeddings asserted the .npz existed and had ids. A file written months
# ago satisfies both forever, so a dead embedding stage was indistinguishable
# from a current one.


def _npz(tmp_path, age_days):
    import os
    import time

    import numpy as np

    path = tmp_path / "embeddings.npz"
    np.savez(str(path), ids=np.array([1, 2, 3]))
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_check_embeddings_stale_when_the_index_stops_rebuilding(hc, tmp_path):
    db = _emails_db([(1, None, None)])

    r = hc.check_embeddings(db, npz_path=_npz(tmp_path, age_days=30))

    assert r["status"] == "STALE"


def test_check_embeddings_fresh_is_ok(hc, tmp_path):
    db = _emails_db([(1, None, None)])

    r = hc.check_embeddings(db, npz_path=_npz(tmp_path, age_days=0))

    assert r["status"] == "OK"


def test_check_embeddings_exposes_age_to_the_report(hc, tmp_path):
    db = _emails_db([(1, None, None)])

    r = hc.check_embeddings(db, npz_path=_npz(tmp_path, age_days=1))

    assert r["age"] is not None


def test_check_embeddings_missing_index_is_warn(hc, tmp_path):
    db = _emails_db([(1, None, None)])

    r = hc.check_embeddings(db, npz_path=tmp_path / "absent.npz")

    assert r["status"] == "WARN"


# --- SharePoint: a frozen ratio cannot move ----------------------------------
# check_sharepoint judged the share of rows whose last_status != 'ok'. If the
# fetcher stops, no row changes, so the ratio is pinned and the row reads OK
# forever. Ageing the *eligible* work is the signal: links past
# MAX_SHAREPOINT_ATTEMPTS are resting by design (prod's 43 sit at 6-9 attempts
# inside a 7-day cool-off) and must stay silent, but a link still inside its
# retry budget that nobody has touched for days means the fetcher is not running.


def test_check_sharepoint_stale_when_eligible_links_go_unattempted(hc):
    db = _sp_db(ok=100, http_error=1, attempted_days_ago=9, attempts=1)

    assert hc.check_sharepoint(db)["status"] == "STALE"


def test_check_sharepoint_ok_when_only_capped_links_are_resting(hc):
    """The real prod tail: attempts exhausted, waiting out the cool-off."""
    db = _sp_db(ok=991, http_error=20, stale=23, attempted_days_ago=9, attempts=6)

    assert hc.check_sharepoint(db)["status"] == "OK"


# --- Abandoned links must be counted, not inferred from silence -------------
# `overdue` deliberately ignores links past the attempt cap, on the grounds that
# they are resting inside a cool-off. But a never-fetched link past the cap is
# excluded from retry_candidates permanently, so it ages out of `overdue` and the
# row returns to OK with the link still missing. Prod carried 23 of them for
# weeks: not deleted documents, but URLs the extractor had mangled.


def test_check_sharepoint_counts_links_it_has_given_up_on(hc):
    db = _sp_db(attempts=5, ok=100, stale=23)

    r = hc.check_sharepoint(db)

    assert r["given_up"] == 23


def test_check_sharepoint_does_not_count_links_still_inside_their_budget(hc):
    """A link with retries left is queued work, not abandoned work."""
    db = _sp_db(attempts=2, ok=100, stale=23)

    assert hc.check_sharepoint(db)["given_up"] == 0


def test_check_sharepoint_does_not_count_successful_links_as_given_up(hc):
    """`fetched_at` is what distinguishes them: attempts alone would count a link
    that failed four times and then succeeded."""
    db = _sp_db(attempts=9, ok=100)

    assert hc.check_sharepoint(db)["given_up"] == 0


def test_report_names_given_up_links_as_no_longer_retried(hc):
    """ "unfetched" reads as "not yet". For these it means never, and the report
    has to say so or they stay indistinguishable from queued work."""
    db = _sp_db(attempts=5, ok=100, stale=23)

    report, _ = hc.build_report([hc.check_sharepoint(db)], {}, {}, {}, [])

    assert "23 given up" in report
    assert "no longer retried" in report


def test_check_sharepoint_ok_when_eligible_links_were_tried_recently(hc):
    db = _sp_db(ok=100, http_error=1, attempted_days_ago=0, attempts=1)

    assert hc.check_sharepoint(db)["status"] == "OK"


# --- Documents / sentinels / sync logs: hand-listed subsets ------------------


def test_check_documents_exposes_age_to_the_report(hc):
    db = _emails_db([(-1, "2026-08-01T10:00:00Z", None)])

    assert hc.check_documents(db)["age"] is not None


def test_check_sentinels_discovers_sentinels_added_later(hc, tmp_path):
    """Two filenames were hardcoded, so any sentinel a future job writes is
    invisible. Discovery must be by pattern, not by list."""
    (tmp_path / "needs_sharepoint_reauth").write_text("")

    r = hc.check_sentinels(state_dir=tmp_path)

    assert any(info.get("present") for info in r.values()), "new sentinel must be seen"
    assert "needs_sharepoint_reauth" in r


def test_check_sentinels_reports_known_sentinels_absent(hc, tmp_path):
    r = hc.check_sentinels(state_dir=tmp_path)

    assert r["needs_reauth"]["present"] is False
    assert r["needs_gcloud_reauth"]["present"] is False


def test_check_sync_logs_covers_every_scheduled_job(hc, tmp_path):
    """Four of twelve job logs were watched. A dead attachments or calendar job
    raised nothing here because its log was simply not in the dict."""
    r = hc.check_sync_logs(log_dir=tmp_path)

    for expected in ("attachments", "calendar_sync", "reverse_ingest", "curate_docs"):
        assert expected in r, f"{expected} log is unwatched"


# --- Inline images: the measured age was never asserted on -------------------


def test_check_images_exposes_age_to_the_report(hc):
    """vision_age was computed and dropped into a key the report never reads,
    so the age column printed '?' for the one source with a known outage."""
    db = _images_db()
    _visioned_baseline(db)

    assert hc.check_images(db)["age"] is not None


def test_sharepoint_attempt_cap_matches_the_fetcher(hc):
    """health_check duplicates MAX_SHAREPOINT_ATTEMPTS because it loads without
    the package. Pin the copy to the original: if the fetcher's cap moves, this
    check would start ageing links that are legitimately resting, or stop
    noticing ones that are not."""
    import sys

    sys.path.insert(0, str(HEALTH_CHECK_PATH.parent.parent))
    from src.export.sharepoint_fetcher import MAX_SHAREPOINT_ATTEMPTS

    assert hc.SHAREPOINT_MAX_ATTEMPTS == MAX_SHAREPOINT_ATTEMPTS


def test_check_sharepoint_token_reports_capture_age(hc, tmp_path):
    """The age column is the report's at-a-glance "is this source asserted on?"
    signal, and every '?' in it marked a check that could not go red. SP Session
    must therefore show its capture age rather than a bare '?' that reads
    identical to a blind check. (It now asserts on that age as well: see
    test_sharepoint_token_warns_when_renewal_stops_though_expiry_is_far_off.)"""
    import json
    from datetime import datetime, timedelta

    p = tmp_path / "sharepoint-session.json"
    p.write_text(
        json.dumps(
            {"capturedAt": "2026-07-01T00:00:00.000Z", "tokenExpiresAt": "2026-07-10T00:00:00.000Z"}
        )
    )

    r = hc.check_sharepoint_token(path=p, now=datetime(2026, 7, 3, tzinfo=UTC))

    assert r["age"] == timedelta(days=2)


def test_check_sharepoint_token_without_capture_stamp_has_no_age(hc, tmp_path):
    """Older session files predate capturedAt; missing it must not raise."""
    from datetime import datetime

    p = _write_sp_session(tmp_path, "2026-07-10T00:00:00.000Z")

    r = hc.check_sharepoint_token(path=p, now=datetime(2026, 7, 3, tzinfo=UTC))

    assert r["age"] is None
    assert r["status"] == "OK"


# --- A rolling session hides a dead renewer ----------------------------------
# tokenExpiresAt is a five-day cookie that the laptop re-mints every 15 minutes,
# so it reads ~5d whether renewal is healthy or stopped an hour ago, and only
# begins to fall a full day after the renewer dies. Judged on expiry alone this
# row stays green for four of the five days it has left. Capture age is the
# signal with the short fuse, and it was displayed but never asserted on.


def _sp_session(dir_path, expires_at, captured_at=None):
    import json

    p = dir_path / "sharepoint-session.json"
    body = {"host": "contoso.sharepoint.com", "tokenExpiresAt": expires_at}
    if captured_at is not None:
        body["capturedAt"] = captured_at
    p.write_text(json.dumps(body))
    return p


def test_sharepoint_token_warns_when_renewal_stops_though_expiry_is_far_off(hc, tmp_path):
    """The blind spot: four days of runway left, and nobody renewing it."""
    from datetime import datetime

    now = datetime(2026, 7, 3, tzinfo=UTC)
    p = _sp_session(tmp_path, "2026-07-07T00:00:00.000Z", captured_at="2026-07-01T00:00:00.000Z")

    r = hc.check_sharepoint_token(path=p, now=now)

    assert r["stale_capture"] is True
    assert r["status"] == "WARN", "a session nobody is renewing must not read OK for four more days"


def test_sharepoint_token_healthy_rolling_session_stays_quiet(hc, tmp_path):
    """The steady state is permanently ~5d out and minutes old. If that is not
    silent the row is noise every single night and stops being read at all."""
    from datetime import datetime

    now = datetime(2026, 7, 3, tzinfo=UTC)
    p = _sp_session(tmp_path, "2026-07-08T00:00:00.000Z", captured_at="2026-07-02T23:45:00.000Z")

    r = hc.check_sharepoint_token(path=p, now=now)

    assert r["stale_capture"] is False
    assert r["status"] == "OK"


def test_sharepoint_token_warns_two_days_out_not_one(hc, tmp_path):
    """Expiry inside 48h is a WARN even with a fresh capture. At a 15-minute
    cadence, an expiry this close means renewal has been failing for three days;
    the old 24h gate left a single day to notice and act."""
    from datetime import datetime

    now = datetime(2026, 7, 3, tzinfo=UTC)
    p = _sp_session(tmp_path, "2026-07-04T12:00:00.000Z", captured_at="2026-07-02T23:45:00.000Z")

    assert hc.check_sharepoint_token(path=p, now=now)["status"] == "WARN"


# --- Job health is not verifiable from the Mac -------------------------------
# All nine ingestion plists were renamed .disabled-migrated-to-vps when
# ingestion moved, so _check_jobs_launchd() reports "MIGRATED" for every one of
# them and _check_jobs_systemd() never runs on this host. Nine lines of
# "MIGRATED" read like nine verified jobs. They are nine unchecked ones: a VPS
# unit that has crashed, failed or been masked renders identically. auto_fix is
# inoperative here too, since nothing can ever reach "FAIL".


def test_report_flags_that_migrated_jobs_are_unchecked(hc):
    jobs = {f"j{i}": {"desc": f"Job {i}", "status": "MIGRATED"} for i in range(9)}

    text, _ = hc.build_report([], jobs, {}, {}, [])

    assert "not verifiable from this host" in text


def test_report_does_not_cry_unverifiable_when_jobs_are_real(hc):
    """On the VPS the same section is authoritative and must stay quiet."""
    jobs = {"a": {"desc": "Job A", "status": "OK"}, "b": {"desc": "Job B", "status": "MIGRATED"}}

    text, _ = hc.build_report([], jobs, {}, {}, [])

    assert "not verifiable from this host" not in text


def test_job_registry_covers_every_unit_that_actually_runs(hc):
    """SYSTEMD_UNITS is now the source both check_jobs and check_sync_logs read,
    so a unit missing from it is a job nobody watches. The VPS runs twelve;
    three — news-sync, conversation-sync and the health check itself — were
    never registered, so they had neither a job status nor a log-age signal."""
    for unit in (
        "sb-news-sync.service",
        "sb-conversation-sync.service",
        "sb-health-check.service",
    ):
        assert unit in hc.SYSTEMD_UNITS, f"{unit} runs on the VPS but nothing watches it"


def test_check_sync_logs_covers_the_late_registered_jobs(hc, tmp_path):
    r = hc.check_sync_logs(log_dir=tmp_path)

    for expected in ("news_sync", "conversation_sync"):
        assert expected in r, f"{expected} log is unwatched"


# --- The log check computed into the void ------------------------------------
# build_report took `logs` as a parameter and never read it, so every log-age
# signal check_sync_logs produced was discarded. A job that keeps its systemd
# unit healthy while writing nothing to its log raised nothing anywhere.


def _log_state(stale, name="outlook_sync"):
    from datetime import timedelta

    return {
        name: {
            "path": f"/x/{name}.log",
            "last_modified": "2026-08-01T00:00:00",
            "age": timedelta(hours=9),
            "stale": stale,
        }
    }


def test_report_surfaces_a_stale_job_log(hc):
    jobs = {"a": {"desc": "Hourly Outlook sync", "status": "OK"}}

    _, issues = hc.build_report([], jobs, _log_state(stale=True), {}, [])

    assert any("outlook_sync" in str(i) for i in issues), "a stale log must reach the issue list"


def test_report_stays_quiet_on_a_fresh_job_log(hc):
    jobs = {"a": {"desc": "Hourly Outlook sync", "status": "OK"}}

    _, issues = hc.build_report([], jobs, _log_state(stale=False), {}, [])

    assert not any("outlook_sync" in str(i) for i in issues)


def test_report_ignores_relic_logs_when_every_job_is_migrated(hc):
    """This host's log dir is a pre-migration relic once the jobs moved — its
    mtimes describe a machine that stopped running them months ago, so ageing
    them would add a dozen permanent false issues to every Mac-side run."""
    jobs = {f"j{i}": {"desc": f"Job {i}", "status": "MIGRATED"} for i in range(9)}

    _, issues = hc.build_report([], jobs, _log_state(stale=True), {}, [])

    assert not any("outlook_sync" in str(i) for i in issues)


def test_report_surfaces_a_missing_job_log(hc):
    """An absent log never got a 'stale' flag at all, so it produced silence
    rather than a signal — the same shape as every other blind spot here. All
    twelve logs resolve on the VPS today, so an absent one is a real fault."""
    jobs = {"a": {"desc": "News sync", "status": "OK"}}
    logs = {"news_sync": {"path": "/x/news-sync.log", "status": "MISSING"}}

    _, issues = hc.build_report([], jobs, logs, {}, [])

    assert any("news_sync" in str(i) for i in issues)


def test_report_ignores_missing_logs_when_jobs_run_elsewhere(hc):
    jobs = {f"j{i}": {"desc": f"Job {i}", "status": "MIGRATED"} for i in range(9)}
    logs = {"news_sync": {"path": "/x/news-sync.log", "status": "MISSING"}}

    _, issues = hc.build_report([], jobs, logs, {}, [])

    assert not any("news_sync" in str(i) for i in issues)


# --- Dating the disable (schema v19) -----------------------------------------
# The count alone says 1,179 chats are gone. The timestamp says they went in a
# single instant, which is the difference between a Graph-wide outage and rooms
# being archived one at a time. Diagnosing the August sweep took a DB dig
# precisely because nothing recorded when it happened.


def test_check_teams_reports_when_the_corpus_was_disabled(hc):
    db = _teams_db(enabled=40, disabled=1179, disabled_at="2026-08-18T14:00:00Z")

    r = hc.check_teams(db)

    assert r["disabled_at"] == "2026-08-18T14:00:00Z"


def test_check_teams_survives_a_db_without_the_disable_date(hc):
    """Pre-v19 DBs lack the column; raising would kill the whole nightly run."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE teams_messages (id INTEGER PRIMARY KEY, composed_at TEXT)")
    db.execute("CREATE TABLE teams_threads (id INTEGER PRIMARY KEY)")
    db.execute(
        "CREATE TABLE teams_chats (id INTEGER PRIMARY KEY, ingest_disabled INT DEFAULT 0, "
        "last_pulled_at TEXT)"
    )
    db.execute("INSERT INTO teams_chats (ingest_disabled, last_pulled_at) VALUES (0, NULL)")
    db.commit()

    assert hc.check_teams(db)["disabled_at"] is None


def test_report_dates_a_mass_disable(hc):
    """End-to-end on the artefact actually read at 23:50."""
    db = _teams_db(enabled=40, disabled=1179, disabled_at="2026-08-18T14:00:00Z")

    text, _ = hc.build_report([hc.check_teams(db)], {}, {}, {}, [])

    assert "2026-08-18" in text, "the report must say WHEN the corpus shrank"


# --- sb-brain-freshness Healthchecks ping --------------------------------------
#
# The check existed in Healthchecks from 2026-08-10 and nothing ever pinged it:
# sb-health-check.sh contains no curl and no HC_PING_URL, and no code anywhere on
# the box referenced the slug. It therefore sat red for fourteen days in total
# silence, because Healthchecks alerts on the transition and not on the state.
# health_check.py is the only place that computes the OK/WARN/STALE verdict, and
# main() exits 0 whether or not there are issues, so a wrapper cannot key on the
# exit code either. The ping belongs here.


def _check(name, status):
    return {"name": name, "status": status}


def test_freshness_verdict_is_ok_when_nothing_is_stale(hc):
    checks = [_check("Emails", "OK"), _check("Teams", "OK")]
    ok, detail = hc.freshness_verdict(checks, {})
    assert ok is True
    assert detail == "2 sources fresh"


def test_freshness_verdict_fails_and_names_every_stale_source(hc):
    checks = [_check("Emails", "STALE"), _check("Teams", "OK"), _check("News", "STALE")]
    ok, detail = hc.freshness_verdict(checks, {})
    assert ok is False
    assert detail == "stale: Emails, News"


def test_warn_is_not_a_freshness_failure(hc):
    """WARN is a quality signal the nightly email already carries in full.

    Making it red here would fire the dead-man's switch on Teams coverage share
    and inline-image queue depth, which self-resolve. Alert volume is the
    standing problem, not alert sensitivity.
    """
    ok, _detail = hc.freshness_verdict([_check("Inline Images", "WARN")], {})
    assert ok is True


def test_a_blocking_sentinel_alone_is_not_a_freshness_failure(hc):
    """Setting a needs_* sentinel is the sanctioned way to degrade gracefully.

    Failing on it would hold this check red for the whole latch period, which is
    exactly what the owner ruled out for the sync jobs' exit codes. If the latch
    lasts long enough to matter, the affected source crosses its own STALE
    threshold and the next line covers that.
    """
    sentinels = {"needs_teams_reauth": {"present": True, "since": "2026-08-24T22:01:48"}}
    ok, _detail = hc.freshness_verdict([_check("Teams", "OK")], sentinels)
    assert ok is True

    ok, detail = hc.freshness_verdict([_check("Teams", "STALE")], sentinels)
    assert ok is False
    assert detail == "stale: Teams"


def test_ping_freshness_is_a_no_op_without_hc_ping_url(hc, monkeypatch):
    """The Mac runs this script too, and CI runs the tests. Neither may ping."""
    monkeypatch.delenv("HC_PING_URL", raising=False)
    called = []
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: called.append(a))

    assert hc.ping_freshness(True, "2 sources fresh") is False
    assert called == []


def test_ping_freshness_posts_the_success_url(hc, monkeypatch):
    monkeypatch.setenv("HC_PING_URL", "http://127.0.0.1:8000/ping/KEY")
    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(hc.subprocess, "run", _fake_run)

    assert hc.ping_freshness(True, "12 sources fresh") is True
    assert seen["cmd"][-1] == "http://127.0.0.1:8000/ping/KEY/sb-brain-freshness"
    assert seen["input"] == "12 sources fresh"
    assert "?create=1" not in seen["cmd"][-1], (
        "auto-provision left eleven checks on the wrong default; do not add a twelfth"
    )


def test_ping_freshness_posts_the_fail_url_when_stale(hc, monkeypatch):
    monkeypatch.setenv("HC_PING_URL", "http://127.0.0.1:8000/ping/KEY/")
    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(hc.subprocess, "run", _fake_run)

    assert hc.ping_freshness(False, "stale: Emails") is True
    assert seen["cmd"][-1] == "http://127.0.0.1:8000/ping/KEY/sb-brain-freshness/fail", (
        "a trailing slash on HC_PING_URL must not produce a double slash"
    )


def test_ping_freshness_never_raises_and_never_echoes_curl_stderr(hc, monkeypatch, capsys):
    """curl -fsS writes the failing URL to stderr, and that URL carries the ping key.

    A ping is best-effort: an exception here must not take down the nightly
    report, and the failing URL must never reach the log.
    """
    monkeypatch.setenv("HC_PING_URL", "http://127.0.0.1:8000/ping/SECRETKEY")

    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(hc.subprocess, "run", _boom)
    assert hc.ping_freshness(True, "ok") is False

    def _curl_fails(*a, **k):
        return type(
            "R",
            (),
            {
                "returncode": 22,
                "stdout": "",
                "stderr": "curl: (22) http://127.0.0.1:8000/ping/SECRETKEY/x returned 404",
            },
        )()

    monkeypatch.setattr(hc.subprocess, "run", _curl_fails)
    assert hc.ping_freshness(True, "ok") is False

    captured = capsys.readouterr()
    assert "SECRETKEY" not in captured.err + captured.out
    assert "curl exit 22" in captured.err


# --- The migration marker did not survive a LaunchAgents rebuild -------------
# _is_migrated() keyed suppression on a FILENAME. Every plist on this Mac was
# re-laid on 2026-08-28 and the nine `*.plist.disabled-migrated-to-vps` markers
# went with it, so all nine jobs reported NOT_LOADED, jobs_elsewhere went False,
# and twelve pre-migration relic logs became twelve permanent false issues on
# every Mac-side run. Underneath that noise the Mac asserted on nothing it
# actually owns: db-pull had neither a job status nor a log-age signal, and a
# failing pull is precisely what corrupted the replica on 2026-08-29.
#
# The replacement evidence has to be something the migrated topology PRODUCES,
# and it has to be timestamped so the suppression expires instead of latching.


def _replica(tmp_path, days_ago=0):
    import os
    from datetime import datetime, timedelta

    p = tmp_path / "brain.db"
    p.write_bytes(b"x")
    when = (datetime.now() - timedelta(days=days_ago)).timestamp()
    os.utime(p, (when, when))
    return p


def _migrated_label(hc):
    """A label that is genuinely absent from the real LaunchAgents dir."""
    return f"{hc.LABEL_PREFIX}.attachments"


def test_is_migrated_accepts_a_freshly_received_replica(hc, tmp_path):
    """A host whose corpus arrives by rsync is not the host that produced it."""
    assert (
        hc._is_migrated(
            _migrated_label(hc),
            agents_dir=hc.LAUNCH_AGENTS_DIR,
            db_path=_replica(tmp_path),
        )
        is True
    )


def test_is_migrated_expires_when_the_replica_goes_stale(hc, tmp_path):
    """Suppression must not latch. A Mac that stopped receiving a corpus has no
    evidence anything runs anywhere and must go loud rather than stay quiet."""
    assert (
        hc._is_migrated(
            _migrated_label(hc),
            agents_dir=hc.LAUNCH_AGENTS_DIR,
            db_path=_replica(tmp_path, days_ago=8),
        )
        is False
    )


def test_is_migrated_never_suppresses_a_job_this_host_owns(hc, tmp_path):
    """db-pull IS the pull. Suppressing it on the strength of its own output
    would make the one job the Mac still runs unfailable."""
    for label in hc.LAUNCHD_LOCAL_JOBS:
        assert (
            hc._is_migrated(label, agents_dir=hc.LAUNCH_AGENTS_DIR, db_path=_replica(tmp_path))
            is False
        )


def test_is_migrated_still_defers_to_an_installed_plist(hc, tmp_path):
    """Unchanged guard: a Mac that still runs a job locally owns it, replica or no."""
    agents = tmp_path / "agents"
    agents.mkdir()
    label = f"{hc.LABEL_PREFIX}.attachments"
    (agents / f"{label}.plist").write_text("x")

    assert hc._is_migrated(label, agents_dir=agents) is False


def test_is_migrated_ignores_host_state_when_the_agents_dir_is_overridden(hc, tmp_path):
    """Guards a regression that CI structurally cannot see. Reading the real
    replica whenever db_path defaults would make _is_migrated depend on whether
    the developer's own laptop happens to be pulling: green on Linux CI, and
    quietly reversing test_is_migrated_false_when_nothing on a Mac."""
    assert hc._is_migrated("com.secondbrain.nope", agents_dir=tmp_path) is False


def test_detect_label_prefix_reads_the_plist_a_migrated_mac_keeps(hc, tmp_path, monkeypatch):
    """db-pull is the only second-brain plist left after migration, so it is the
    only thing to read the prefix off. Discovery missed it, LABEL_PREFIX fell
    back to the generic literal, and every derived label named a job that had
    never existed on this machine."""
    monkeypatch.delenv("BRAIN_LABEL_PREFIX", raising=False)
    (tmp_path / "com.example.brain.db-pull.plist").write_text("x")

    assert hc.detect_label_prefix(agents_dir=tmp_path) == "com.example.brain"


def test_mac_registry_asserts_on_the_jobs_the_mac_owns(hc):
    assert hc.LAUNCHD_LOCAL_JOBS, "the Mac still owns db-pull and the document push"
    assert set(hc.LAUNCHD_LOCAL_JOBS) <= set(hc.LAUNCHD_JOBS)
    assert set(hc.LAUNCHD_MIGRATED_JOBS).isdisjoint(hc.LAUNCHD_LOCAL_JOBS)


def test_db_pull_threshold_spans_the_overnight_gap(hc):
    """The pull fires 07:45..22:45, so the widest legitimate gap is 22:45 ->
    07:45. Under nine hours reports a false stale log every single night; over a
    day stops being an assertion at all."""
    from datetime import timedelta

    assert hc.STALE_THRESHOLDS["db_pull"] > timedelta(hours=9)
    assert hc.STALE_THRESHOLDS["db_pull"] < timedelta(hours=24)
    assert hc.MIGRATION_EVIDENCE_MAX_AGE > hc.STALE_THRESHOLDS["db_pull"], (
        "the fast signal must fire before the suppression lifts, or one dead pull "
        "produces ten issues instead of one"
    )


def _migrated_jobs(hc):
    jobs = {f"j{i}": {"desc": f"Job {i}", "status": "MIGRATED"} for i in range(9)}
    jobs["local"] = {"desc": next(iter(hc.LAUNCHD_LOCAL_JOBS.values())), "status": "OK"}
    return jobs


def test_relic_logs_stay_suppressed_once_the_local_job_is_registered(hc):
    """jobs_elsewhere used to be all(MIGRATED). Registering db-pull, a job that
    can never be MIGRATED, would have flipped it False and re-armed all twelve
    relic logs."""
    _, issues = hc.build_report([], _migrated_jobs(hc), _log_state(stale=True), {}, [])

    assert not any("outlook_sync" in str(i) for i in issues)


def test_the_local_job_log_is_exempt_from_the_relic_suppression(hc):
    """The Mac must never go green by ceasing to look: the one log it owns
    carries the timestamp assertion and has to survive the mute."""
    from datetime import timedelta

    logs = {
        "outlook_sync": {"path": "/x/outlook-sync.log", "age": timedelta(days=78), "stale": True},
        "db_pull": {"path": "/x/db-pull.log", "age": timedelta(hours=30), "stale": True},
    }

    _, issues = hc.build_report([], _migrated_jobs(hc), logs, {}, [])

    assert any("db_pull" in str(i) for i in issues), "the Mac's own job log must still fail"
    assert not any("outlook_sync" in str(i) for i in issues)


def test_migration_suppression_cannot_reach_the_vps(hc):
    """Nothing here may weaken the systemd path: _is_migrated is called from the
    launchd branch alone and check_jobs dispatches on platform."""
    import inspect

    assert "_is_migrated" in inspect.getsource(hc._check_jobs_launchd)
    assert "_is_migrated" not in inspect.getsource(hc._check_jobs_systemd)
    assert "_replica_is_fresh" not in inspect.getsource(hc._check_jobs_systemd)


# --- A replica cannot know what happened after it was taken ------------------
# Every source age is derived from rows inside brain.db. On the Mac that file is
# a copy pulled 07:45..22:45, so overnight the gap alone pushed Emails past its
# 6h threshold and the Mac reported "Emails: STALE" every night with nothing
# wrong. Ageing against the copy asks the only question a replica can answer.


def _pull_stamp(tmp_path, hours_old):
    """The stamp sb-db-pull.sh writes after a copy passes its integrity check."""
    from datetime import datetime, timedelta

    p = tmp_path / "db-pull.stamp"
    when = datetime.now(UTC) - timedelta(hours=hours_old)
    p.write_text(when.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    return p


def test_corpus_lag_is_zero_on_the_host_that_writes_the_database(hc, tmp_path):
    """A stamp written moments ago discounts nothing worth mentioning."""
    assert hc.corpus_lag(stamp=_pull_stamp(tmp_path, hours_old=0)) < timedelta(minutes=1)


def test_corpus_lag_reports_the_age_of_a_pulled_replica(hc, tmp_path):
    lag = hc.corpus_lag(stamp=_pull_stamp(tmp_path, hours_old=9))

    assert timedelta(hours=8, minutes=45) < lag < timedelta(hours=9, minutes=15)


def test_corpus_lag_never_goes_negative(hc, tmp_path):
    """A clock-skewed mtime in the future would otherwise make every source look
    fresher than it is, which is the one direction this must never fail in."""
    assert hc.corpus_lag(stamp=_pull_stamp(tmp_path, hours_old=-5)) == timedelta(0)


def test_corpus_lag_is_zero_when_there_is_no_stamp(hc, tmp_path):
    assert hc.corpus_lag(stamp=tmp_path / "absent.stamp") == timedelta(0)


def test_report_declares_that_ages_come_through_a_replica(hc, monkeypatch):
    """Unlabelled, "Emails 4h" on a copy taken 6h ago reads as 4h when the truth
    is 10h. The discount is only honest if the report says it applied one."""
    monkeypatch.setattr(hc, "CORPUS_LAG", timedelta(hours=6))

    report, _ = hc.build_report([], {}, {}, {}, [])

    assert "AS OF the local replica" in report


def test_report_stays_silent_about_a_replica_on_the_writing_host(hc, monkeypatch):
    monkeypatch.setattr(hc, "CORPUS_LAG", timedelta(0))

    report, _ = hc.build_report([], {}, {}, {}, [])

    assert "AS OF the local replica" not in report


def test_age_is_never_negative(hc, monkeypatch):
    """On a pull host, anything written locally AFTER the last pull is newer than
    the observation point (conversation capture does exactly that, and the row
    rendered as "-261m"). Nonsense to read, and it compares as fresh anyway."""
    from datetime import datetime

    monkeypatch.setattr(hc, "CORPUS_LAG", timedelta(hours=5))
    just_now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert hc._age(just_now) == timedelta(0)


def test_age_discounts_the_replica_lag(hc, monkeypatch):
    """The whole point: a row two hours old, seen through a copy taken five hours
    ago, was two hours old when the copy was taken."""
    from datetime import datetime

    monkeypatch.setattr(hc, "CORPUS_LAG", timedelta(hours=5))
    seven_hours_ago = (datetime.now(UTC) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert timedelta(hours=1, minutes=55) < hc._age(seven_hours_ago) < timedelta(hours=2, minutes=5)


def test_format_age_distinguishes_unknown_from_zero(hc):
    """ "?" is this report's marker for "nothing is being asserted here", so a
    clamped zero age must not borrow it: a timedelta is falsy at zero and the
    freshest possible answer rendered identically to a blind check."""
    assert hc.format_age(None) == "?"
    assert hc.format_age(timedelta(0)) == "0m"


# --- Curation runs, exits 0, and places nothing ------------------------------
# Every folder hit its soft cap, so 40 of the last 44 runs placed zero documents
# while the job ran cleanly and wrote its log. check_documents counts rows the
# reverse-ingest wrote and check_document_roots watches the push heartbeat, so
# nothing anywhere asked whether a document actually arrived. `deferred` is the
# signal: candidates the cap turned away rather than silently marked done.


def _curate_state(tmp_path, deferred=None, copied=None):
    import json

    p = tmp_path / "curate-state.json"
    p.write_text(json.dumps({"deferred": deferred or {}, "copied": copied or []}))
    return p


def test_check_curation_is_quiet_when_nothing_is_blocked(hc, tmp_path):
    state = _curate_state(tmp_path, copied=[{"id": 1, "classified_at": "2026-08-29T10:00:00Z"}])

    r = hc.check_curation(state_path=state)

    assert r["deferred"] == 0
    assert r["blocked"] == 0
    assert r["status"] == "OK"


def test_check_curation_tolerates_ordinary_back_pressure(hc, tmp_path):
    """A deferred candidate with retries left clears itself once a folder has
    room. That is the mechanism working, not a fault."""
    state = _curate_state(tmp_path, deferred={"7": {"folder": "retail", "attempts": 1}})

    r = hc.check_curation(state_path=state)

    assert r["deferred"] == 1
    assert r["status"] == "OK"


def test_check_curation_warns_once_candidates_run_out_of_retries(hc, tmp_path):
    """Exhausted retries mean the document is being DROPPED, which is the
    condition that went unnoticed for a month."""
    state = _curate_state(
        tmp_path,
        deferred={
            "7": {"folder": "retail", "attempts": hc.CURATE_MAX_DEFER_ATTEMPTS},
            "8": {"folder": "retail", "attempts": 1},
        },
    )

    r = hc.check_curation(state_path=state)

    assert r["blocked"] == 1
    assert r["status"] == "WARN"


def test_check_curation_is_na_without_a_state_file(hc, tmp_path):
    """The VPS has one; a machine that never runs curate must not invent a fault."""
    assert hc.check_curation(state_path=tmp_path / "absent.json")["status"] == "N/A"


def test_check_curation_survives_a_corrupt_state_file(hc, tmp_path):
    """main() runs the checks unguarded, so one bad byte here would kill the
    whole nightly report and send no email at all."""
    p = tmp_path / "curate-state.json"
    p.write_text("{not json")

    assert hc.check_curation(state_path=p)["status"] == "WARN"


def test_curate_defer_cap_matches_the_real_one(hc):
    """Duplicated because health_check.py is loaded standalone. Pin it to the
    source of truth so the two cannot drift into disagreeing about what
    "out of retries" means."""
    import re
    from pathlib import Path

    src = (Path(hc.__file__).parent / "curate_documents_daily.py").read_text()
    m = re.search(r"^MAX_DEFER_ATTEMPTS\s*=\s*(\d+)", src, re.MULTILINE)
    assert m, "could not find MAX_DEFER_ATTEMPTS in curate_documents_daily.py"
    assert hc.CURATE_MAX_DEFER_ATTEMPTS == int(m.group(1))


def test_report_names_the_curation_blockage(hc, tmp_path):
    state = _curate_state(
        tmp_path, deferred={"7": {"folder": "retail", "attempts": hc.CURATE_MAX_DEFER_ATTEMPTS}}
    )

    report, issues = hc.build_report([hc.check_curation(state_path=state)], {}, {}, {}, [])

    assert "out of retries" in report
    assert any("Curation" in str(i) for i in issues)


# --- Downloaded is not the same as registered --------------------------------
# outlook-cli writes binaries into data/attachments/<message_id>/ and a separate
# pass registers them. That pass defers any directory whose message_id has no row
# in `emails`, so a directory whose email never loaded is deferred for ever. Prod
# accumulated 3,396 of them, 7.23 GB, growing ~20/day and invisible to every
# search, while check_attachments counted attachment_content rows and stayed
# perfectly consistent. An attachment exists when the table says so.


def _attachments_db(root=None, *registered):
    """A DB where each name in  has an attachments row whose
    file_path points into that directory, which is what the registrar writes."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY, message_id TEXT)")
    db.execute("CREATE TABLE attachments (id INTEGER PRIMARY KEY, message_id, file_path TEXT)")
    for m in registered:
        db.execute("INSERT INTO emails (message_id) VALUES (?)", (str(m),))
        db.execute(
            "INSERT INTO attachments (message_id, file_path) VALUES (?,?)",
            (str(m), f"{root}/{m}/doc.pdf"),
        )
    db.commit()
    return db


def _attachment_dirs(tmp_path, *names):
    for n in names:
        (tmp_path / str(n)).mkdir()
    return tmp_path


def test_unregistered_dirs_counts_directories_with_no_email(hc, tmp_path):
    root = _attachment_dirs(tmp_path, "known-1", "known-2", "orphan-1", "orphan-2", "orphan-3")
    db = _attachments_db(root, "known-1", "known-2")

    assert hc.count_unregistered_attachment_dirs(db, root=root) == 3


def test_unregistered_dirs_is_zero_when_every_download_has_its_email(hc, tmp_path):
    root = _attachment_dirs(tmp_path, "known-1", "known-2")
    db = _attachments_db(root, "known-1", "known-2")

    assert hc.count_unregistered_attachment_dirs(db, root=root) == 0


def test_unregistered_dirs_ignores_loose_files(hc, tmp_path):
    """Only directories are message-id keyed; a stray file is not a deferred
    attachment and must not be counted as one."""
    root = _attachment_dirs(tmp_path, "known-1")
    db = _attachments_db(root, "known-1")
    (root / ".DS_Store").write_text("x")

    assert hc.count_unregistered_attachment_dirs(db, root=root) == 0


def test_unregistered_dirs_is_zero_without_an_attachments_root(hc, tmp_path):
    """A host that has never downloaded one must not invent a fault."""
    db = _attachments_db(tmp_path, "known-1")

    assert hc.count_unregistered_attachment_dirs(db, root=tmp_path / "absent") == 0


def test_a_reverse_ingested_document_is_not_an_orphan(hc, tmp_path):
    """The 44% over-report. A reverse-ingested document carries a NEGATIVE
    message_id but attachment_pipeline.py files it under str(abs(id)), so its
    directory name can never match the row that already registers it. Asking
    about names counts 1,468 healthy directories as leaked; asking about
    file_path does not."""
    root = _attachment_dirs(tmp_path, "1003114379954136173")
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY, message_id)")
    db.execute("CREATE TABLE attachments (id INTEGER PRIMARY KEY, message_id, file_path TEXT)")
    db.execute("INSERT INTO emails (message_id) VALUES (-1003114379954136173)")
    db.execute(
        "INSERT INTO attachments (message_id, file_path) VALUES (?,?)",
        (-1003114379954136173, f"{root}/1003114379954136173/INDEX.md"),
    )
    db.commit()

    assert hc.count_unregistered_attachment_dirs(db, root=root) == 0


def test_outlook_sync_threshold_spans_its_real_overnight_gap(hc):
    """sb-outlook-sync.timer is not hourly despite the name: its OnCalendar skips
    02:00 to 06:00, so the widest legitimate gap is 01:00 -> 07:00 plus a 300s
    randomised delay. At three hours this log reported stale from about 04:00 to
    07:00 every night, on the host that actually sends the report."""
    assert hc.STALE_THRESHOLDS["outlook_sync"] > timedelta(hours=6, minutes=5)
    assert hc.STALE_THRESHOLDS["outlook_sync"] < timedelta(hours=12), (
        "wide enough to stop being an assertion"
    )
