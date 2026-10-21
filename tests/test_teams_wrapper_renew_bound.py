"""sb-teams-sync.sh bounds each teams-cli call it makes before the sync.

The pre-flight probe and the silent renew ran unbounded. A renew drives a headless
Chrome whose launch alone may wait three minutes before it gives up, then 90 s of login
and 40 s more, inside a unit with TimeoutStartSec=600 that still has the sync to run.
In 139 VPS attempts to 2026-10-20 a renew never took longer than 92 s and succeeded
once, so a bound costs nothing on the evidence and caps the case the evidence has not
seen yet. A call cut off by its bound reads as the call failing, which keeps the exit
codes' meaning: a dead probe and a renew that did not take still exit 75.

The real wrapper runs against a throwaway HOME, with stub teams-cli and timeout.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_WRAPPER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-teams-sync.sh"

_STUB_TIMEOUT = """#!/bin/bash
echo "$*" >> "$HOME/timeout.log"
while [ "${1#-}" != "$1" ]; do
  case "$1" in -k|-s) shift 2 ;; *) shift ;; esac
done
shift
exec "$@"
"""


def _home(tmp_path: Path, teams_cli: str, real_timeout: bool = False) -> Path:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho "$*" >> "$HOME/calls.log"\nexit 0\n')
    python.chmod(0o755)
    tools = home / "tools"
    tools.mkdir()
    (tools / "teams-cli").write_text("#!/bin/bash\n" + teams_cli)
    (tools / "teams-cli").chmod(0o755)
    if not real_timeout:
        (tools / "timeout").write_text(_STUB_TIMEOUT)
        (tools / "timeout").chmod(0o755)
    return home


def _run(home: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPER)],
        env={
            "HOME": str(home),
            "PATH": f"{home / 'tools'}:/usr/bin:/bin",
            "SHELL": "/bin/bash",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _bounds(home: Path) -> dict[str, list[str]]:
    """{teams-cli subcommand: the timeout arguments in front of it}."""
    out: dict[str, list[str]] = {}
    for line in (home / "timeout.log").read_text().splitlines():
        args = line.split()
        if "teams-cli" in args:
            at = args.index("teams-cli")
            out.setdefault(args[at + 1], args[:at])
    return out


def test_the_probe_and_the_renew_are_both_bounded(tmp_path):
    home = _home(tmp_path, 'case "$1" in health-check) exit 1;; *) exit 0;; esac\n')

    result = _run(home)

    bounds = _bounds(home)
    assert set(bounds) == {"health-check", "auth-renew"}
    for args in bounds.values():
        # -k: a renew that ignores SIGTERM (a wedged Chrome) is killed, not waited on.
        assert args[0] == "-k"
        assert int(args[-1]) > 0
    # The renew's bound sits above the slowest one seen (92 s) and leaves the sync its
    # time inside TimeoutStartSec=600.
    assert 92 < int(bounds["auth-renew"][-1]) <= 180
    assert result.returncode == 75


def test_a_healthy_session_is_probed_once_and_not_renewed(tmp_path):
    home = _home(tmp_path, "exit 0\n")

    result = _run(home)

    assert set(_bounds(home)) == {"health-check"}
    assert result.returncode == 0


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") and shutil.which("timeout")),
    reason="needs coreutils timeout, which the producer has",
)
def test_a_renew_that_hangs_is_cut_off_and_reads_as_a_failed_renew(tmp_path):
    home = _home(
        tmp_path,
        'case "$1" in health-check) exit 1;; auth-renew) exec sleep 600;; esac\n',
        real_timeout=True,
    )

    result = _run(home, SB_TEAMS_RENEW_TIMEOUT_S="2")

    assert result.returncode == 75
    assert not (home / "calls.log").exists()
