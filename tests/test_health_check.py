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
