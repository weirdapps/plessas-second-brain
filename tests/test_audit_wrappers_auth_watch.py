"""auth-watch's restoration trigger must hand every job to the supervisor.

When a sentinel clears, `sb-auth-watch.sh` re-fires the jobs that skipped during
the outage through `trigger_job`, which maps each wrapper to its systemd unit
(or launchd label) and falls back to a bare `nohup` only when it has none. That
fallback is unsafe on its own terms: the child lands in auth-watch's cgroup.

`sb-outlook-sync.sh` was fired but never mapped, so every Outlook or gcloud
restoration took the fallback. From auth-watch's own timer the child died with
auth-watch's cgroup a second later, so the mail catch-up never happened. From
sb-outlook-sync's pre-flight it ran a second full sync beside the first, which
doubled the extraction, collided on UNIQUE in the load, and was SIGKILLed when
the unit stopped, turning it red. The child also inherited fd 9, the auth-watch
lock, so every other auth-watch run skipped while it lived.

These run the real watcher on a throwaway HOME with stub CLIs, stub wrappers and
a stub systemctl, so nothing real is started.
"""

import re
import subprocess
import time
from pathlib import Path

_WATCHER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-auth-watch.sh"


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _triggered_scripts() -> set[str]:
    return set(re.findall(r"trigger_job\s+(sb-[\w-]+\.sh)", _WATCHER.read_text()))


def _mapped_units() -> dict[str, str]:
    return dict(
        re.findall(r"^\s*(sb-[\w-]+\.sh)\)[^\n]*unit=\"([^\"]+)\"", _WATCHER.read_text(), re.M)
    )


def test_every_triggered_wrapper_has_a_unit_mapping():
    triggered = _triggered_scripts()
    assert "sb-outlook-sync.sh" in triggered, "the parse found no trigger_job calls"
    missing = sorted(triggered - set(_mapped_units()))
    assert not missing, (
        f"trigger_job is called for {missing} but its case table has no unit for them, "
        "so they always take the nohup fallback: killed with auth-watch's cgroup, or "
        "run twice beside the unit that called auth-watch."
    )


def test_outlook_sync_maps_to_its_unit():
    assert _mapped_units().get("sb-outlook-sync.sh") == "sb-outlook-sync.service"


def _restoration_home(home: Path, *, systemctl_ok: bool) -> Path:
    """A HOME where the Outlook sentinel is latched and the probe now passes.

    That is a restoration, so every Outlook-gated wrapper is re-fired, mail sync
    included. The gcloud probe is not the trigger here because it resolves a
    Homebrew gcloud ahead of any stub on a Mac.
    """
    sb = home / ".second-brain"
    sb.mkdir(parents=True)
    (sb / "needs_reauth").touch()
    bin_dir = home / ".local" / "bin"
    _write_stub(bin_dir / "gcloud", "exit 0\n")
    _write_stub(
        bin_dir / "outlook-cli",
        'case "$1" in auth-check) echo \'{"status":"ok","tokenExpiresAt":"2999-01-01T00:00:00.000Z"}\';; esac\nexit 0\n',
    )
    _write_stub(
        bin_dir / "teams-cli",
        'case "$1" in health-check) echo \'{"overall":"ok","probes":[]}\';; esac\nexit 0\n',
    )
    calls = home / "systemctl-calls"
    _write_stub(bin_dir / "systemctl", f'echo "$*" >> {calls}\nexit {0 if systemctl_ok else 1}\n')
    # Never the real launchctl: on a Mac it would kickstart a real job.
    _write_stub(bin_dir / "launchctl", "exit 1\n")
    # Each stub wrapper records whether it inherited fd 9, the auth-watch lock.
    for script in _triggered_scripts():
        marker = home / f"{script}.fd9"
        _write_stub(
            bin_dir / script,
            f"if {{ : >&9; }} 2>/dev/null; then echo open > {marker}; else echo closed > {marker}; fi\n",
        )
    return home


def _run_watcher(home: Path) -> str:
    subprocess.run(
        ["/bin/bash", str(_WATCHER)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/bash", "UID": "501"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    log = home / ".second-brain" / "logs" / "auth-watch.log"
    return log.read_text() if log.is_file() else ""


def test_a_restoration_starts_outlook_sync_through_systemd(tmp_path):
    home = _restoration_home(tmp_path / "home", systemctl_ok=True)

    log = _run_watcher(home)

    calls = (home / "systemctl-calls").read_text()
    assert "start --no-block sb-outlook-sync.service" in calls, log
    assert "nohup-fallback" not in log, log


def test_a_nohup_fallback_child_does_not_hold_the_auth_watch_lock(tmp_path):
    home = _restoration_home(tmp_path / "home", systemctl_ok=False)

    log = _run_watcher(home)

    assert "nohup-fallback sb-outlook-sync.sh" in log, log
    marker = home / "sb-outlook-sync.sh.fd9"
    for _ in range(50):
        if marker.is_file() and marker.read_text().strip():
            break
        time.sleep(0.1)
    assert marker.read_text().strip() == "closed", (
        "the fallback child inherited fd 9, so while it runs every other auth-watch "
        "invocation finds the lock held and skips."
    )
