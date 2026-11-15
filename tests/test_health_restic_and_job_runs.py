"""Two rows the nightly health check lacked: the restic backup and the job_runs ledger.

restic runs as a root system unit (restic-backup.service) that nothing read, and job_runs
(issue 173) recorded a failed or killed run that no report showed. These pin each row's
verdicts on canned `systemctl` / `journalctl` output and temporary stores; nothing here
touches a real unit, journal or data directory.
"""

import importlib.util
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

HEALTH_CHECK_PATH = Path(__file__).parent.parent / "scripts" / "health_check.py"
NOW = datetime(2026, 11, 15, 21, 50, tzinfo=UTC)


@pytest.fixture
def hc():
    spec = importlib.util.spec_from_file_location("health_check", HEALTH_CHECK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- restic --------------------------------------------------------------------------------


def _restic(journal: str = "", unit: bool = True, journal_rc: int = 0, journal_error=None):
    """A fake subprocess.run: `systemctl cat` finds the unit or not, journalctl prints
    `journal` (or exits journal_rc, or raises journal_error)."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs.get("timeout"), "every call needs a timeout"
        if "systemctl" in cmd[0]:
            assert cmd[1:] == ["cat", "restic-backup.service"]
            if not unit:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="No files found for restic-backup.service."
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="[Unit]\n", stderr="")
        if journal_error is not None:
            raise journal_error
        return subprocess.CompletedProcess(cmd, journal_rc, stdout=journal, stderr="")

    return run, calls


def _completed(delta: timedelta) -> str:
    return f"restic-backup: backup completed {(NOW - delta).isoformat(timespec='seconds')}"


def test_a_recent_restic_backup_is_ok(hc):
    journal = "\n".join(
        [
            _completed(timedelta(hours=43)),
            "Finished restic-backup.service",
            "snapshot 1a2b3c saved",
            _completed(timedelta(hours=19)),
            "Finished restic-backup.service",
        ]
    )
    run, calls = _restic(journal)

    r = hc.check_restic(run=run, now=NOW)

    assert r["name"] == "Restic"
    assert r["status"] == "OK"
    assert r["total"] == 2
    assert timedelta(hours=18) < r["age"] < timedelta(hours=20)
    journal_cmd = next(c for c in calls if "journalctl" in c[0])
    assert "restic-backup.service" in journal_cmd
    assert "--user" not in journal_cmd


def test_a_restic_backup_older_than_36_hours_is_stale(hc):
    run, _ = _restic(_completed(timedelta(hours=40)))

    r = hc.check_restic(run=run, now=NOW)

    assert r["status"] == "STALE"
    assert "1d 16h" in r["note"]


def test_no_completed_restic_backup_in_the_journal_is_stale(hc):
    run, _ = _restic("Starting restic-backup.service\nFatal: unable to open repository\n")

    r = hc.check_restic(run=run, now=NOW)

    assert r["status"] == "STALE"
    assert r["age"] is None
    assert "backup completed" in r["note"]


def test_a_completed_line_without_a_time_is_not_taken_for_a_backup(hc):
    run, _ = _restic("backup completed sometime\n")

    assert hc.check_restic(run=run, now=NOW)["status"] == "STALE"


def test_a_host_without_the_restic_unit_is_not_judged(hc):
    run, calls = _restic(unit=False)

    r = hc.check_restic(run=run, now=NOW)

    assert r["status"] == "N/A"
    assert all("journalctl" not in c[0] for c in calls)


def test_a_host_without_systemctl_is_not_judged(hc):
    def run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    assert hc.check_restic(run=run, now=NOW)["status"] == "N/A"


def test_an_unreadable_journal_warns_and_does_not_raise(hc):
    run, _ = _restic(journal_rc=1)
    assert hc.check_restic(run=run, now=NOW)["status"] == "WARN"

    run, _ = _restic(journal_error=subprocess.TimeoutExpired("journalctl", 30))
    r = hc.check_restic(run=run, now=NOW)
    assert r["status"] == "WARN"
    assert "journal" in r["note"]


# --- job_runs ------------------------------------------------------------------------------


def _store(rows=()):
    from src.store.schema import create_database

    conn = create_database(":memory:")
    for i, (job, stage, status, started) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO job_runs (run_id, job, stage, started_at, status, host, pid) "
            "VALUES (?, ?, ?, ?, ?, 'vps', ?)",
            (f"r{i}", job, stage, started, status, 1000 + i),
        )
    conn.commit()
    return conn


def _at(delta: timedelta) -> str:
    return (NOW - delta).isoformat(timespec="seconds")


def test_runs_that_all_finished_are_ok(hc):
    db = _store(
        [
            ("sync", "run", "ok", _at(timedelta(hours=1))),
            ("sync", "run", "running", _at(timedelta(minutes=5))),
            ("teams-sync", "run", "ok", _at(timedelta(hours=3))),
        ]
    )

    r = hc.check_job_runs(db, now=NOW)

    assert r["name"] == "Job runs"
    assert r["status"] == "OK"
    assert r["total"] == 3
    assert timedelta(minutes=4) < r["age"] < timedelta(minutes=6)


def test_failed_runs_of_the_last_day_warn_with_job_and_count(hc):
    db = _store(
        [
            ("sync", "run", "failed", _at(timedelta(hours=2))),
            ("sync", "run", "failed", _at(timedelta(hours=5))),
            ("teams-sync", "run", "failed", _at(timedelta(hours=20))),
            ("teams-sync", "extract", "failed", _at(timedelta(hours=20))),
            ("calendar-sync", "run", "failed", _at(timedelta(hours=30))),
        ]
    )

    r = hc.check_job_runs(db, now=NOW)

    assert r["status"] == "WARN"
    assert r["failed"] == {"sync": 2, "teams-sync": 1}
    assert "sync 2" in r["note"] and "teams-sync 1" in r["note"]
    assert "calendar-sync" not in r["note"]


def test_a_run_still_running_after_12_hours_warns_by_name(hc):
    db = _store(
        [
            ("attachments", "run", "running", _at(timedelta(days=6))),
            ("sync", "run", "running", _at(timedelta(hours=11))),
        ]
    )

    r = hc.check_job_runs(db, now=NOW)

    assert r["status"] == "WARN"
    assert [h["job"] for h in r["hung"]] == ["attachments"]
    assert "attachments" in r["note"]
    assert "sync" not in r["note"].replace("attachments", "")


def test_a_run_stamped_without_an_offset_is_read_as_local_time(hc):
    local = (NOW - timedelta(hours=13)).astimezone().replace(tzinfo=None).isoformat()
    db = _store([("sync", "run", "running", local)])

    assert hc.check_job_runs(db, now=NOW)["status"] == "WARN"


def test_a_store_without_job_runs_is_not_judged(hc):
    import sqlite3

    r = hc.check_job_runs(sqlite3.connect(":memory:"), now=NOW)

    assert r["status"] == "N/A"


def test_the_report_names_both_rows_and_lists_their_warnings(hc):
    checks = [
        {
            "name": "Restic",
            "status": "STALE",
            "total": 0,
            "age": None,
            "note": "no 'backup completed' in the journal for 7 days",
        },
        {
            "name": "Job runs",
            "status": "WARN",
            "total": 5,
            "age": timedelta(minutes=5),
            "note": "failed in the last 24h: sync 2",
        },
    ]

    text, issues = hc.build_report(checks, {}, {}, {}, [])

    assert "no 'backup completed' in the journal" in text
    assert "failed in the last 24h: sync 2" in text
    assert "Restic: STALE" in issues and "Job runs: WARN" in issues
