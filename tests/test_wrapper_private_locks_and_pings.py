"""Wrapper locks live in a private directory, and the Healthchecks URL never sits in argv.

Security audit of 2026-11-01, two findings on the scheduler wrappers:

* Their mkdir locks defaulted to /tmp (or to $TMPDIR, which launchd does not always
  set, so /tmp again). /tmp is writable by every local user, who could create the
  lock first and keep the job from ever running. The default is now
  ${XDG_RUNTIME_DIR:-$HOME/.second-brain/locks}, mode 0700; each override variable
  still wins. A lock left in the old place is simply no longer looked at.
* `curl ... "${HC_PING_URL}/slug"` put the ping URL, which is the secret, in every
  process listing for as long as curl ran. curl now reads it as a config line on
  stdin (`-K -`), and the Python ping is urllib.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_WRAPPERS = _ROOT / "scripts" / "wrappers"
_ALL_WRAPPERS = sorted(_WRAPPERS.rglob("*.sh"))


# --- No lock under /tmp or $TMPDIR --------------------------------------------


@pytest.mark.parametrize("wrapper", _ALL_WRAPPERS, ids=lambda p: p.name)
def test_no_wrapper_keeps_a_lock_in_a_shared_temp_directory(wrapper):
    for n, line in enumerate(wrapper.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"LOCK\w*=", line):
            assert "/tmp" not in line and "TMPDIR" not in line, f"{wrapper.name}:{n}: {line}"


def _python_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/bash\necho "$*" >> "$HOME/calls.log"\nexit 0\n')
    python.chmod(0o755)
    return home


def _held(lock: Path) -> Path:
    """A lock held by a live process: this test's own."""
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    return lock


def _run(wrapper: str, home: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPERS / "systemd" / wrapper)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash", **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


_SYSTEMD_LOCKS = [
    ("sb-daily-sync.sh", "sb-daily-sync.lock", "daily-sync.log"),
    ("sb-conversation-sync.sh", "sb-conversation-sync.lock", "conversation-sync.log"),
]


@pytest.mark.parametrize(("wrapper", "lock", "log"), _SYSTEMD_LOCKS)
def test_the_default_lock_is_under_xdg_runtime_dir(tmp_path, wrapper, lock, log):
    home = _python_home(tmp_path)
    runtime = tmp_path / "run-user"
    runtime.mkdir(mode=0o700)
    _held(runtime / lock)

    result = _run(wrapper, home, XDG_RUNTIME_DIR=str(runtime))

    assert result.returncode == 0, result.stderr
    assert "SKIP: already running" in (home / ".second-brain" / "logs" / log).read_text()
    assert not (home / "calls.log").exists(), "the run went on past a held lock"


@pytest.mark.parametrize(("wrapper", "lock", "log"), _SYSTEMD_LOCKS)
def test_without_xdg_runtime_dir_the_lock_is_in_a_private_home_directory(
    tmp_path, wrapper, lock, log
):
    home = _python_home(tmp_path)
    locks = home / ".second-brain" / "locks"
    _held(locks / lock)
    locks.chmod(0o755)

    result = _run(wrapper, home)

    assert result.returncode == 0, result.stderr
    assert "SKIP: already running" in (home / ".second-brain" / "logs" / log).read_text()
    assert stat.S_IMODE(locks.stat().st_mode) == 0o700


@pytest.mark.parametrize(("wrapper", "lock", "log"), _SYSTEMD_LOCKS)
def test_the_private_lock_directory_is_created_owner_only(tmp_path, wrapper, lock, log):
    home = _python_home(tmp_path)

    _run(wrapper, home)

    locks = home / ".second-brain" / "locks"
    assert locks.is_dir()
    assert stat.S_IMODE(locks.stat().st_mode) == 0o700
    assert not (locks / lock).exists(), "the lock outlived the run"


# --- No ping URL in argv -------------------------------------------------------


@pytest.mark.parametrize("wrapper", _ALL_WRAPPERS, ids=lambda p: p.name)
def test_no_wrapper_puts_the_ping_url_on_a_command_line(wrapper):
    """Read per command, continuation lines joined; a here-string is stdin, not argv."""
    code = [ln for ln in wrapper.read_text().splitlines() if not ln.lstrip().startswith("#")]
    for command in "\n".join(code).replace("\\\n", " ").splitlines():
        found = re.search(r"\bcurl\b", command)
        if found:
            argv = command[found.start() :].split("<<<", 1)[0]
            assert "HC_PING_URL" not in argv, f"{wrapper.name}: {command}"


def test_no_python_script_curls_the_ping_url():
    for script in (_ROOT / "scripts").glob("*.py"):
        text = script.read_text()
        if "HC_PING_URL" in text:
            assert '"curl"' not in text, f"{script.name} passes the ping URL to curl"


def test_the_daily_backup_ping_reaches_curl_on_stdin_only(tmp_path):
    home = _python_home(tmp_path)
    tools = home / "tools"
    tools.mkdir()
    (tools / "curl").write_text(
        '#!/bin/bash\n{ echo "argv: $*"; echo "stdin: $(cat)"; } >> "$HOME/curl.log"\n'
    )
    (tools / "curl").chmod(0o755)
    stamp = subprocess.run(["date", "+%Y%m%d"], capture_output=True, text=True).stdout.strip()
    backups = home / "SourceCode" / "plessas-second-brain" / "data" / "backups"
    backups.mkdir(parents=True)
    (backups / f"brain-{stamp}.db").write_text("")

    _run(
        "sb-daily-sync.sh",
        home,
        PATH=f"{tools}:/usr/bin:/bin",
        HC_PING_URL="https://hc.invalid/ping/SECRETKEY",
        SB_DAILY_SYNC_LOCK=str(home / "daily-sync.lock"),
    )

    log = (home / "curl.log").read_text()
    argv = [line for line in log.splitlines() if line.startswith("argv: ")]
    assert argv, log
    assert all("SECRETKEY" not in line for line in argv), log
    assert 'url = "https://hc.invalid/ping/SECRETKEY/brain-backup"' in log
