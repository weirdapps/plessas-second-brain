"""The nightly mail reconcile, and the counters it leaves for the health check.

`mail-reconcile` was on no timer, so the 190 messages it found missing on
2026-10-07 were found by a human. And `fetch_gave_up` (messages the export
stopped retrying) lived only in the cursor files, quarantined staging batches
only in a directory, and the health check read neither. The 02:00 attachment
pass now runs `mail-reconcile --since 7d --refetch --limit N --health-json`,
which writes data/state/mail_loss.json: {updated_at, fetch_gave_up, quarantined}
plus per-folder and reconcile detail.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from src.export import mail_reconcile as mr
from src.export.outlook_cli import OutlookCliAuthRequired
from src.export.state import OutlookSyncState, save_outlook_sync_state
from src.store import sql_readonly
from src.store.schema import create_database

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7d", NOW - timedelta(days=7)),
        ("36h", NOW - timedelta(hours=36)),
        ("2026-05-01", datetime(2026, 5, 1, tzinfo=UTC)),
        ("2026-05-01T09:30:00Z", datetime(2026, 5, 1, 9, 30, tzinfo=UTC)),
    ],
)
def test_since_takes_a_time_before_now_or_an_iso_date(value, expected):
    assert mr.parse_since(value, now=NOW) == expected


@pytest.mark.parametrize("value", ["", "seven days", "-7d", "7w"])
def test_since_refuses_what_it_cannot_read(value):
    assert mr.parse_since(value, now=NOW) is None


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data home with three cursor files and a quarantined batch."""
    monkeypatch.setattr(mr, "DATA_ROOT", tmp_path)
    state = tmp_path / "state"
    for name, folder, gave_up, retries in (
        ("outlook_sync.json", "Inbox", 0, 1),
        ("outlook_sync_archive.json", "Archive", 2, 0),
        ("outlook_sync_sent.json", "Sent Items", 1, 0),
    ):
        save_outlook_sync_state(
            state / name,
            OutlookSyncState(
                last_seen_received_at="2026-10-10T20:00:00Z",
                folder=folder,
                fetch_gave_up=[{"id": f"{folder}-{n}", "attempts": 5} for n in range(gave_up)],
                fetch_retries={f"r{n}": {"attempts": 1} for n in range(retries)},
            ),
        )
    quarantine = tmp_path / "staging" / "quarantine"
    quarantine.mkdir(parents=True)
    (quarantine / "batch-50007.json").write_text("{not json", encoding="utf-8")
    return tmp_path


def test_the_counters_add_up_the_cursor_files_and_the_quarantine(home):
    written = mr.write_mail_loss()

    on_disk = json.loads((home / "state" / "mail_loss.json").read_text())
    assert on_disk == written
    assert (written["fetch_gave_up"], written["quarantined"]) == (3, 1)
    assert written["folders"]["Archive"] == {
        "fetch_gave_up": 2,
        "fetch_retries": 0,
        "last_gave_up_at": None,
    }
    assert written["folders"]["Inbox"]["fetch_retries"] == 1
    assert written["updated_at"].endswith("Z")
    assert written["reconcile"]["status"] == "not run"
    assert written["last_gave_up_at"] is None, "entries written before the field carry no time"
    assert not list((home / "state").glob("*.tmp")), "written atomically"


def test_the_newest_give_up_is_dated(home):
    path = home / "state" / "outlook_sync_archive.json"
    raw = json.loads(path.read_text())
    raw["fetch_gave_up"][-1]["gave_up_at"] = "2026-10-10T07:10:00Z"
    path.write_text(json.dumps(raw))

    written = mr.write_mail_loss()

    assert written["last_gave_up_at"] == "2026-10-10T07:10:00Z"
    assert written["folders"]["Archive"]["last_gave_up_at"] == "2026-10-10T07:10:00Z"


def test_a_failed_reconcile_keeps_when_the_last_one_succeeded(home):
    report = mr.Report(since=NOW - timedelta(days=7), until=NOW, cutoff=None)
    first = mr.write_mail_loss(report=report, refetch_stats={"staged": 2, "failed": ["x"]})
    assert first["reconcile"]["status"] == "ok"
    assert (first["reconcile"]["refetched"], first["reconcile"]["refetch_failed"]) == (2, 1)

    second = mr.write_mail_loss(error="outlook-cli needs re-authentication (exit 4)")

    assert second["reconcile"]["status"] == "failed"
    assert second["reconcile"]["ok_at"] == first["reconcile"]["ok_at"]
    assert second["fetch_gave_up"] == 3, "the counts stay fresh when the reconcile fails"


# --- The command ---------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(sql_readonly, "REPLICA_STAMP", tmp_path / "absent-db-pull.stamp")
    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    return path


def _run(monkeypatch, db, *argv):
    from src import cli

    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db), "mail-reconcile", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_the_nightly_command_reconciles_a_week_and_writes_the_counters(home, db, monkeypatch):
    listings: list[list[str]] = []

    def fake(args, timeout_sec=60):
        listings.append(list(args))
        return []

    monkeypatch.setattr(mr, "run_outlook_cli", fake)
    monkeypatch.setattr(
        mr,
        "refetch",
        lambda missing, concurrency=2, limit=0: {
            "requested": 0,
            "staged": 0,
            "failed": [],
            "attachments": 0,
        },
    )

    rc = _run(monkeypatch, db, "--since", "7d", "--refetch", "--limit", "100", "--health-json")

    assert rc == 0
    since = mr.parse_when(listings[0][listings[0].index("--since") + 1])
    assert abs(since - (datetime.now(UTC) - timedelta(days=7))) < timedelta(minutes=5)
    written = json.loads((home / "state" / "mail_loss.json").read_text())
    assert written["reconcile"]["status"] == "ok"
    assert written["reconcile"]["missing"] == 0
    assert written["fetch_gave_up"] == 3


def test_the_counters_are_written_when_the_reconcile_fails(home, db, monkeypatch):
    def expired(args, timeout_sec=60):
        raise OutlookCliAuthRequired("expired")

    monkeypatch.setattr(mr, "run_outlook_cli", expired)

    assert _run(monkeypatch, db, "--since", "7d", "--health-json") == 4

    written = json.loads((home / "state" / "mail_loss.json").read_text())
    assert written["reconcile"]["status"] == "failed"
    assert written["quarantined"] == 1


def test_a_timed_out_listing_is_exit_5_with_the_counters_written(home, db, monkeypatch):
    def slow(args, timeout_sec=60):
        raise subprocess.TimeoutExpired(cmd="outlook-cli", timeout=timeout_sec)

    monkeypatch.setattr(mr, "run_outlook_cli", slow)

    assert _run(monkeypatch, db, "--since", "7d", "--health-json") == 5
    assert (home / "state" / "mail_loss.json").exists()


def test_the_counters_are_refused_on_a_replica(home, db, monkeypatch):
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    assert _run(monkeypatch, db, "--since", "7d", "--health-json") == 2

    assert not (home / "state" / "mail_loss.json").exists()
