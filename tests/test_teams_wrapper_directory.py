"""sb-teams-sync.sh runs directory-sync after a good teams-sync, and never fails on it.

The directory step shares the Teams session, so it runs only when the sync succeeded,
only while the unit has time left, and its failure is logged without touching the unit's
exit code (scripts/wrappers/systemd/sb-teams-sync.sh). The real wrapper runs against a
throwaway HOME whose venv python is a stub that records its arguments.
"""

import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-teams-sync.sh"

STUB_PYTHON = """#!/bin/bash
echo "$*" >> "$HOME/calls.log"
case "$*" in
  *teams-sync*) exit "${TEAMS_RC:-0}" ;;
  *directory-sync*) exit "${DIRECTORY_RC:-0}" ;;
esac
exit 0
"""

# coreutils timeout is not on every test host; the stub runs the command it wraps.
STUB_TIMEOUT = """#!/bin/bash
shift
exec "$@"
"""


def _run(home: Path, teams_rc: int = 0, directory_rc: int = 0):
    venv = home / ".venvs" / "second-brain" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(STUB_PYTHON)
    (venv / "python").chmod(0o755)
    tools = home / "tools"
    tools.mkdir()
    (tools / "timeout").write_text(STUB_TIMEOUT)
    (tools / "timeout").chmod(0o755)
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    result = subprocess.run(
        ["/bin/bash", str(WRAPPER)],
        env={
            "HOME": str(home),
            "PATH": f"{tools}:/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "TEAMS_RC": str(teams_rc),
            "DIRECTORY_RC": str(directory_rc),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = (home / "calls.log").read_text().splitlines()
    log = (home / ".second-brain" / "logs" / "teams-sync.log").read_text()
    return result, calls, log


def test_a_good_sync_is_followed_by_the_directory_step(tmp_path):
    result, calls, _ = _run(tmp_path)
    assert result.returncode == 0
    assert calls == [
        "-m src.cli teams-sync --concurrency 2 --workers 4",
        "-m src.cli directory-sync --limit 400",
    ]


def test_a_failed_sync_skips_the_directory_step(tmp_path):
    result, calls, _ = _run(tmp_path, teams_rc=5)
    assert result.returncode == 5
    assert calls == ["-m src.cli teams-sync --concurrency 2 --workers 4"]


@pytest.mark.parametrize("directory_rc", [1, 4])
def test_a_failed_directory_step_does_not_fail_the_unit(tmp_path, directory_rc):
    result, calls, log = _run(tmp_path, directory_rc=directory_rc)
    assert result.returncode == 0
    assert len(calls) == 2
    assert f"directory-sync failed rc={directory_rc} (not fatal)" in log
