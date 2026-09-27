"""inbox_reconcile speaks the estate's exit codes, and lists through the shared adapter.

Every M365 CLI here, and every wrapper that runs one, reads 4 as "re-authenticate"
and 5 as "upstream misbehaved". The reconcile inverted that: an outlook-cli auth
loss (its exit 4) came out as 2, while a locked database came out as 4. The VPS
log holds 64 runs of `fail rc=4 (... reconcile=4)` that were lock contention,
each one reading as an auth failure, and 3 genuine auth losses reported as 2.
A replica refusal also sat on 5.

It also called a bare `outlook-cli` with neither --no-auto-reauth nor
OUTLOOK_CLI_PATH, unlike every other caller, which go through
src.export.outlook_cli.run_outlook_cli.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.export import inbox_reconcile
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError


def test_the_listing_goes_through_the_shared_adapter():
    items = [{"Id": "AAMk1", "InternetMessageId": "<a@example.test>"}, {"Id": "AAMk2"}]
    with patch.object(inbox_reconcile, "run_outlook_cli", return_value=items) as run:
        outlook_ids, internet_ids = inbox_reconcile.list_current_inbox_ids(max_results=300)

    run.assert_called_once_with(
        [
            "list-mail",
            "--folder",
            "Inbox",
            "--select",
            "Id,InternetMessageId",
            "--all",
            "--max",
            "300",
        ],
        timeout_sec=120,
    )
    assert outlook_ids == {"AAMk1", "AAMk2"}
    assert internet_ids == {"<a@example.test>"}


def _main_with(monkeypatch, tmp_path, error: BaseException) -> int:
    db = tmp_path / "brain.db"
    db.touch()
    monkeypatch.setattr(inbox_reconcile, "is_replica", lambda: False)
    monkeypatch.setattr(sys, "argv", ["inbox_reconcile", "--db", str(db)])

    def boom(*_a, **_k):
        raise error

    monkeypatch.setattr(inbox_reconcile, "reconcile_moves", boom)
    return inbox_reconcile.main()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OutlookCliAuthRequired("session expired"), 4),
        (OutlookCliError(exit_code=5, stderr="graph 503", retryable=True), 5),
        (OutlookCliError(exit_code=127, stderr="not found", retryable=False), 5),
        (subprocess.TimeoutExpired(cmd="outlook-cli", timeout=120), 5),
        (sqlite3.OperationalError("database is locked"), 75),
        (json.JSONDecodeError("Expecting value", "", 0), 75),
    ],
)
def test_each_failure_exits_with_its_estate_code(monkeypatch, tmp_path, error, expected):
    assert _main_with(monkeypatch, tmp_path, error) == expected


def test_a_replica_refusal_uses_the_cli_code_not_upstream():
    """src.cli exits 2 on a replica refusal; 5 means an upstream failure here."""
    assert inbox_reconcile.REFUSED_ON_REPLICA == 2


_OUTLOOK_SYNC = (
    Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-outlook-sync.sh"
)


def _run_outlook_sync(tmp_path, python_body: str) -> tuple[int, str]:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/bash\n" + python_body)
    python.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(_OUTLOOK_SYNC)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode, (home / ".second-brain" / "logs" / "outlook-sync.log").read_text()


def test_a_later_pass_does_not_overwrite_an_auth_failure(tmp_path):
    """The reconcile runs last, so its code used to become the unit's exit status
    even when an export pass had already reported that the session was dead."""
    rc, log = _run_outlook_sync(
        tmp_path,
        'case "$*" in\n  *"--folder Inbox"*) exit 4;;\n  *inbox_reconcile*) exit 75;;\nesac\nexit 0\n',
    )
    assert rc == 4, log


def test_the_failure_line_names_the_sent_pass(tmp_path):
    rc, log = _run_outlook_sync(tmp_path, 'case "$*" in *"Sent Items"*) exit 5;; esac\nexit 0\n')
    assert rc == 5, log
    assert "sent=5" in log, log
