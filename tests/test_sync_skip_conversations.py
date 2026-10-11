"""`sync --skip-conversations` leaves Step 7, the Claude Code conversations, out of a run.

It is there for anyone who wants a sync without them; no scheduled unit passes it. Step 7
read every transcript of the last week (1.15 GB on the producer) and every staged batch
(773 MB) on every run. With staging pruned once loaded and an unchanged transcript not read
again (tests/test_conversation_staging.py) it is cheap, and conversation memory should not
lag the mail by half a day, so the hourly mail unit loads conversations as the daily and
noon syncs do.
"""

import re
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

_WRAPPERS = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd"


def _sync(tmp_path, monkeypatch, **flags):
    from src import cli, llm_deadline
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.pipeline.sync.DATA_ROOT", tmp_path)
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda: None)
    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=True, **flags
    )
    ok = {"extracted": 0, "failed": 0, "quota_paused": False}
    with (
        patch("src.extract.local.run_extraction", return_value=ok),
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch(
            "src.export.conversation_export.export_conversations", return_value={"exported": 1}
        ) as export,
        patch("src.extract.local.run_conversation_extraction") as extraction,
        patch("src.store.loader.load_conversations", return_value=0) as load,
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        rc = cli.cmd_sync(args)
    return rc, export, extraction, load


def test_skip_conversations_leaves_step_7_out(tmp_path, monkeypatch, capsys):
    rc, export, extraction, load = _sync(tmp_path, monkeypatch, skip_conversations=True)

    assert rc == 0
    export.assert_not_called()
    extraction.assert_not_called()
    load.assert_not_called()
    assert "--skip-conversations" in capsys.readouterr().out


def test_without_it_step_7_runs_as_before(tmp_path, monkeypatch):
    rc, export, extraction, load = _sync(tmp_path, monkeypatch)

    assert rc == 0
    export.assert_called_once()
    extraction.assert_called_once()
    load.assert_called_once()


def test_skip_conversations_reaches_cmd_sync_from_the_command_line(monkeypatch, tmp_path):
    from src import cli

    seen = []
    monkeypatch.setattr(cli, "cmd_sync", lambda args: seen.append(args.skip_conversations))
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    for argv, expected in (([], False), (["--skip-conversations"], True)):
        monkeypatch.setattr(
            sys, "argv", ["brain", "--db", str(tmp_path / "brain.db"), "sync", *argv]
        )
        cli.main()
        assert seen[-1] is expected


# ------------------------------------------------------------------------ wrappers


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


def _sync_calls(wrapper: str, tmp_path: Path) -> list[str]:
    home = _home(tmp_path)
    subprocess.run(
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
    calls = (home / "calls.log").read_text().splitlines()
    return [c for c in calls if "src.cli sync" in c]


_SYNC_WRAPPERS = ("sb-outlook-sync.sh", "sb-noon-catchup.sh", "sb-daily-sync.sh")


def test_every_sync_unit_loads_the_conversations(tmp_path):
    for wrapper in _SYNC_WRAPPERS:
        calls = _sync_calls(wrapper, tmp_path / wrapper)
        assert calls, wrapper
        assert not any("--skip-conversations" in c for c in calls), wrapper


def test_no_sync_start_in_any_wrapper_skips_them_the_lock_retries_too():
    for wrapper in _SYNC_WRAPPERS:
        text = (_WRAPPERS / wrapper).read_text()
        starts = [line for line in text.splitlines() if re.search(r"-m src\.cli sync\b", line)]
        assert starts, wrapper
        assert not any("--skip-conversations" in line for line in starts), wrapper
