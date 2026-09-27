"""auth-watch must not write the gcloud relogin helper's stdout to a log.

The host-local gcloud-auto-login.sh ends by echoing the ADC access token to
stdout. auth-watch needs only the helper's side effect, never its output, yet it
appended stdout to gcloud-auto-login.log, where 57 bearer tokens sat in
plaintext. Only stderr, the diagnostics, belongs in that log.

The helper lives outside this repo, so the test runs the real watcher against a
stub helper that prints a fake token on stdout and a diagnostic on stderr.
"""

import subprocess
import time
from pathlib import Path

_WATCHER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-auth-watch.sh"


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def test_the_relogin_token_on_stdout_never_reaches_the_log(tmp_path):
    home = tmp_path / "home"
    (home / ".second-brain").mkdir(parents=True)
    bin_dir = home / ".local" / "bin"
    # Only a failing probe reaches the relogin. A Homebrew gcloud wins over these
    # stubs on a Mac, and fails too: the throwaway HOME holds no credentials.
    for gcloud in (bin_dir / "gcloud", home / "google-cloud-sdk" / "bin" / "gcloud"):
        _write_stub(gcloud, "exit 1\n")
    _write_stub(
        bin_dir / "outlook-cli",
        'case "$1" in auth-check) echo \'{"status":"ok","tokenExpiresAt":"2999-01-01T00:00:00.000Z"}\';; esac\nexit 0\n',
    )
    _write_stub(
        bin_dir / "teams-cli",
        'case "$1" in health-check) echo \'{"overall":"ok","probes":[]}\';; esac\nexit 0\n',
    )
    # Alerts go to stubs, never to the owner's screen: osascript is called by its
    # absolute path unless $OSASCRIPT names another, and terminal-notifier is found
    # on the script's PATH, where $HOME/.local/bin comes first.
    _write_stub(home / "osascript", 'echo "osascript $*" >> "$HOME/alerts.txt"\n')
    _write_stub(
        bin_dir / "terminal-notifier", 'echo "terminal-notifier $*" >> "$HOME/alerts.txt"\n'
    )
    done = home / "relogin-ran"
    _write_stub(
        home / "scripts" / "gcloud-auto-login.sh",
        f"echo 'relogin diagnostic' >&2\necho 'FAKE-ACCESS-TOKEN-ON-STDOUT'\ntouch {done}\n",
    )

    subprocess.run(
        ["/bin/bash", str(_WATCHER)],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "UID": "501",
            "OSASCRIPT": str(home / "osascript"),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    for _ in range(50):
        if done.exists():
            break
        time.sleep(0.1)
    time.sleep(0.2)

    assert done.exists(), "the relogin helper never ran; the probe did not fail"
    log = (home / ".second-brain" / "logs" / "gcloud-auto-login.log").read_text()
    assert "relogin diagnostic" in log, log
    assert "FAKE-ACCESS-TOKEN-ON-STDOUT" not in log, (
        "the helper's stdout, an access token, was written to the log"
    )
