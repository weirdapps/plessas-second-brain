"""The 02:00 attachment pass runs the week's mail reconcile, within a call budget.

It is the one stage of the pass that calls outlook-cli, so it alone waits for a
dead Outlook session (sb-outlook-sync owns that alarm) and says so in the log;
every other stage still runs. A failed reconcile fails the pass like any stage.
These run the real wrapper against a throwaway HOME whose venv python records
its arguments and exits as told.
"""

import subprocess
from pathlib import Path

_WRAPPER = (
    Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-attachment-pass.sh"
)


def _home(tmp_path: Path, body: str = "exit 0\n", sentinel: str | None = None) -> Path:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    (home / ".second-brain").mkdir(parents=True)
    if sentinel:
        (home / ".second-brain" / sentinel).touch()
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho "$*" >> "$HOME/calls.log"\n' + body)
    python.chmod(0o755)
    return home


def _run(home: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPER)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash", **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(home: Path) -> list[str]:
    log = home / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _reconcile(home: Path) -> list[str]:
    return [c for c in _calls(home) if "mail-reconcile" in c]


def test_the_pass_reconciles_a_week_of_mail_with_a_budget_and_counters(tmp_path):
    home = _home(tmp_path)

    assert _run(home).returncode == 0

    (call,) = _reconcile(home)
    args = call.split()
    assert args[args.index("--since") + 1] == "7d"
    assert "--refetch" in args and "--health-json" in args
    assert args[args.index("--limit") + 1] == "100"


def test_the_budget_can_be_set_from_the_environment(tmp_path):
    home = _home(tmp_path)

    _run(home, SB_MAIL_RECONCILE_LIMIT="25")

    (call,) = _reconcile(home)
    args = call.split()
    assert args[args.index("--limit") + 1] == "25"


def test_a_dead_outlook_session_skips_only_the_reconcile_and_says_so(tmp_path):
    home = _home(tmp_path, sentinel="needs_reauth")

    result = _run(home)

    assert result.returncode == 0
    assert _reconcile(home) == []
    calls = " ".join(_calls(home))
    assert "register-attachments" in calls and "process-sharepoint" in calls
    log = (home / ".second-brain" / "logs" / "attachments.log").read_text()
    assert "mail reconcile" in log and "skip" in log.lower() and "needs_reauth" in log


def test_a_failed_reconcile_fails_the_pass_and_names_itself(tmp_path):
    home = _home(tmp_path, body='case "$*" in *mail-reconcile*) exit 5;; esac\nexit 0\n')

    result = _run(home)

    assert result.returncode == 65
    assert "mail reconcile FAILED (exit 5)" in result.stderr
