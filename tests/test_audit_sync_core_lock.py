"""One `src.cli sync` at a time, and the noon catch-up runs through an Outlook outage.

Three units run `src.cli sync`: the hourly sb-outlook-sync, sb-noon-catchup and
sb-daily-sync. Only the hourly wrapper looked for another sync, with a pgrep, once,
one way and not atomically. On 2026-09-24 the noon catch-up started while the
hourly load was running, and both extracted the same six conversations and
rewrote the state file from their own snapshots, where the last writer drops the
other's processed_ids and give-up counters. cmd_sync now holds an exclusive lock
on DATA_ROOT/state/sync.lock for the whole run.

The noon catch-up also skipped, green, whenever the Outlook sentinel was set,
although sync never calls outlook-cli: it only drains what is already staged.
"""

import fcntl
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_WRAPPERS = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd"


@pytest.fixture
def store(tmp_path, monkeypatch):
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    return db_path


def _sync(db_path, lock_wait=0.0, extraction=None):
    """cmd_sync with every stage mocked. Returns (rc, whether extraction ran)."""
    from src import cli

    args = types.SimpleNamespace(
        db=db_path,
        limit=None,
        engine="claude",
        workers=1,
        skip_export=True,
        lock_wait=lock_wait,
    )
    ok = {"extracted": 0, "failed": 0, "quota_paused": False}
    with (
        patch("src.extract.local.run_extraction", return_value=ok, side_effect=extraction) as run,
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        rc = cli.cmd_sync(args)
    return rc, run.called


def _hold(path: Path):
    """Take the sync lock as another process would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    held = open(path, "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return held


def test_a_second_sync_skips_while_the_first_holds_the_lock(store, tmp_path, capsys):
    held = _hold(tmp_path / "state" / "sync.lock")
    try:
        rc, ran = _sync(store)
    finally:
        held.close()

    assert rc == 0
    assert not ran
    assert "skip: another sync is running" in capsys.readouterr().out


def test_a_sync_waits_up_to_lock_wait_and_then_runs(store, tmp_path):
    held = _hold(tmp_path / "state" / "sync.lock")
    threading.Timer(0.3, held.close).start()

    rc, ran = _sync(store, lock_wait=10.0)

    assert rc == 0
    assert ran


def test_a_sync_that_waited_its_full_lock_wait_skips(store, tmp_path, capsys):
    held = _hold(tmp_path / "state" / "sync.lock")
    started = time.monotonic()
    try:
        rc, ran = _sync(store, lock_wait=0.4)
    finally:
        held.close()

    assert time.monotonic() - started >= 0.4
    assert (rc, ran) == (0, False)
    assert "skip: another sync is running" in capsys.readouterr().out


def test_the_lock_is_held_for_the_whole_run(store, tmp_path):
    seen: list[bool] = []

    def extraction(*args, **kwargs):
        probe = open(tmp_path / "state" / "sync.lock", "a")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen.append(False)
        except BlockingIOError:
            seen.append(True)
        finally:
            probe.close()
        return {"extracted": 0, "failed": 0, "quota_paused": False}

    _sync(store, extraction=extraction)

    assert seen == [True]


def test_the_lock_is_released_after_a_run_and_after_a_failure(store):
    def boom(*args, **kwargs):
        raise RuntimeError("extraction blew up")

    with pytest.raises(RuntimeError):
        _sync(store, extraction=boom)

    assert _sync(store) == (0, True)
    assert _sync(store) == (0, True)


def test_lock_wait_reaches_cmd_sync_from_the_command_line(monkeypatch, tmp_path):
    from src import cli

    seen = []
    monkeypatch.setattr(cli, "cmd_sync", lambda args: seen.append(args.lock_wait))
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    for argv, expected in (([], 0), (["--lock-wait", "600"], 600)):
        monkeypatch.setattr(
            sys, "argv", ["brain", "--db", str(tmp_path / "brain.db"), "sync", *argv]
        )
        cli.main()
        assert seen[-1] == expected


# ------------------------------------------------------------------ wrappers


def _home(tmp_path: Path) -> Path:
    """A throwaway HOME whose venv python records its arguments and exits 0."""
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    (home / ".second-brain").mkdir()
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho "$*" >> "$HOME/calls.log"\nexit 0\n')
    python.chmod(0o755)
    return home


def _run(wrapper: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPERS / wrapper)],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "SB_DAILY_SYNC_LOCK": str(home / "daily-sync.lock"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def _sync_calls(home: Path) -> list[str]:
    calls = home / "calls.log"
    lines = calls.read_text().splitlines() if calls.exists() else []
    return [c for c in lines if "src.cli sync" in c]


@pytest.mark.parametrize(
    ("wrapper", "wait"), [("sb-noon-catchup.sh", 600), ("sb-daily-sync.sh", 300)]
)
def test_the_backlog_wrappers_wait_for_a_running_sync(tmp_path, wrapper, wait):
    """Sized against each unit's TimeoutStartSec=1800: the noon job has run up to
    13 minutes; the daily one, 21 minutes, 12 of them its backup before sync."""
    home = _home(tmp_path)

    result = _run(wrapper, home)

    assert result.returncode == 0
    assert _sync_calls(home) and all(f"--lock-wait {wait}" in c for c in _sync_calls(home))


def test_the_noon_catchup_runs_while_the_outlook_session_is_down(tmp_path):
    home = _home(tmp_path)
    (home / ".second-brain" / "needs_reauth").touch()

    result = _run("sb-noon-catchup.sh", home)

    assert result.returncode == 0
    assert len(_sync_calls(home)) == 1


def test_the_noon_catchup_says_why_it_skips_on_an_expired_gcloud_credential(tmp_path):
    home = _home(tmp_path)
    (home / ".second-brain" / "needs_gcloud_reauth").touch()

    _run("sb-noon-catchup.sh", home)

    assert _sync_calls(home) == []
    log = (home / ".second-brain" / "logs" / "noon-catchup.log").read_text()
    assert "needs_gcloud_reauth" in log
