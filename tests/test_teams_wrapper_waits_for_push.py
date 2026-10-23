"""On Linux, sb-teams-sync.sh waits for the laptop's push instead of renewing an expired token.

The producer cannot renew Teams itself (1 success in 347 attempts): silent SSO needs
device trust it does not hold, and only the laptop's token push renews it. On
2026-10-23 the laptop slept from 17:27 to 19:43 and the unit spent seven attempts of
about 100 s and 850 MB each renewing a token that had already expired, each ending 75
and buying another retry. Now an expired token ends 69 at once (no retry), and the push
restarts the run through sb-creds-arrived.path and sb-auth-watch. A token that has not
expired, an unreadable file, or a Mac keep the renew and its 75.
"""

import json
import subprocess
import time
from pathlib import Path

_WRAPPER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-teams-sync.sh"

_STUB_TIMEOUT = """#!/bin/bash
while [ "${1#-}" != "$1" ]; do
  case "$1" in -k|-s) shift 2 ;; *) shift ;; esac
done
shift
exec "$@"
"""

_REQUIRED = ("https://graph.microsoft.com", "https://chatsvcagg.teams.microsoft.com")


def _home(tmp_path: Path, exp_in_s: int | None) -> Path:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho "$*" >> "$HOME/calls.log"\nexit 0\n')
    python.chmod(0o755)
    tools = home / "tools"
    tools.mkdir()
    (tools / "teams-cli").write_text(
        '#!/bin/bash\necho "$1" >> "$HOME/teams-cli.log"\n'
        'case "$1" in health-check) exit 1;; *) exit 1;; esac\n'
    )
    (tools / "teams-cli").chmod(0o755)
    (tools / "timeout").write_text(_STUB_TIMEOUT)
    (tools / "timeout").chmod(0o755)
    if exp_in_s is not None:
        tokens = home / ".teams-cli" / "multi-tokens.json"
        tokens.parent.mkdir()
        # Field shape only: an exp per audience, as teams-cli stores it.
        exp = time.time() + exp_in_s
        tokens.write_text(json.dumps({a: {"exp": exp} for a in _REQUIRED}))
    return home


def _run(home: Path, platform: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPER)],
        env={
            "HOME": str(home),
            "PATH": f"{home / 'tools'}:/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "TEAMS_SYNC_PLATFORM": platform,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _renewed(home: Path) -> bool:
    return "auth-renew" in (home / "teams-cli.log").read_text().split()


def _log(home: Path) -> str:
    return (home / ".second-brain" / "logs" / "teams-sync.log").read_text()


def test_an_expired_token_on_linux_ends_69_without_a_renew(tmp_path):
    home = _home(tmp_path, exp_in_s=-600)

    result = _run(home, "Linux")

    assert result.returncode == 69
    assert not _renewed(home)
    assert "waiting for the laptop's push" in _log(home)
    assert not (home / "calls.log").exists()


def test_a_token_that_has_not_expired_still_gets_the_renew_and_its_retry(tmp_path):
    """A 401 or a 429 on a live token can clear by itself; 75 keeps its retry."""
    home = _home(tmp_path, exp_in_s=1800)

    result = _run(home, "Linux")

    assert result.returncode == 75
    assert _renewed(home)


def test_an_unreadable_token_file_keeps_the_old_path(tmp_path):
    home = _home(tmp_path, exp_in_s=None)

    result = _run(home, "Linux")

    assert result.returncode == 75
    assert _renewed(home)


def test_a_mac_renews_an_expired_token_itself(tmp_path):
    home = _home(tmp_path, exp_in_s=-600)

    result = _run(home, "Darwin")

    assert result.returncode == 75
    assert _renewed(home)
