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

The trigger then started every restored job in the same second (issue #153): on
2026-10-10 the daily sync, calendar, Teams, attachments and mail together, whose
memory peaks sum to about twice the host's RAM, and the daily sync a second time
that day. It now hands the jobs to one transient unit (`--restore`), which starts
them one at a time in a fixed order, waits for each within a bound, and skips a
daily sync that already ran that day before the outage. On Linux it also stops
attempting the silent renew, which never worked headless there, and it logs who
set a sentinel. The waits are environment settings, so these run in milliseconds.
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

_WATCHER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-auth-watch.sh"


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _triggered_scripts() -> set[str]:
    return set(re.findall(r"restore\+=\((sb-[\w-]+\.sh)\)", _WATCHER.read_text()))


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


def _restoration_home(home: Path, *, systemctl_ok: bool, systemd_run_ok: bool = False) -> Path:
    """A HOME where the Outlook sentinel is latched and the probe now passes.

    That is a restoration, so every Outlook-gated wrapper is re-fired, mail sync
    included. The gcloud probe is not the trigger here because it resolves a
    Homebrew gcloud ahead of any stub on a Mac. systemd-run is always a stub: a
    Linux runner has a real one, which must never be reached from a test.
    """
    sb = home / ".second-brain"
    sb.mkdir(parents=True)
    (sb / "needs_reauth").touch()
    bin_dir = home / ".local" / "bin"
    _write_stub(
        bin_dir / "systemd-run",
        f'printf "%s\\n" "$@" > "$HOME/systemd-run-args"\nexit {0 if systemd_run_ok else 1}\n',
    )
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
    # Alerts go to stubs, never to the owner's screen: osascript is called by its
    # absolute path unless $OSASCRIPT names another, and terminal-notifier is found
    # on the script's PATH, where $HOME/.local/bin comes first.
    _write_stub(home / "osascript", 'echo "osascript $*" >> "$HOME/alerts.txt"\n')
    _write_stub(
        bin_dir / "terminal-notifier", 'echo "terminal-notifier $*" >> "$HOME/alerts.txt"\n'
    )
    # Each stub wrapper records whether it inherited fd 9, the auth-watch lock.
    for script in _triggered_scripts():
        marker = home / f"{script}.fd9"
        _write_stub(
            bin_dir / script,
            f"if {{ : >&9; }} 2>/dev/null; then echo open > {marker}; else echo closed > {marker}; fi\n",
        )
    return home


def _run_watcher(home: Path, *args: str, platform: str | None = None, **env: str) -> str:
    subprocess.run(
        ["/bin/bash", str(_WATCHER), *args],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "UID": "501",
            "OSASCRIPT": str(home / "osascript"),
            **({"AUTH_WATCH_PLATFORM": platform} if platform else {}),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    log = home / ".second-brain" / "logs" / "auth-watch.log"
    return log.read_text() if log.is_file() else ""


def test_a_restoration_hands_the_jobs_to_one_transient_unit_in_order(tmp_path):
    """Neither caller can wait for the jobs: auth-watch's own unit stops after ten
    minutes and the daily sync alone takes twenty, and from sb-outlook-sync's
    pre-flight it would hold the mail sync and wait on itself."""
    home = _restoration_home(tmp_path / "home", systemctl_ok=True, systemd_run_ok=True)

    log = _run_watcher(home)

    args = (home / "systemd-run-args").read_text().splitlines()
    assert "--user" in args
    assert any(a.startswith("--unit=sb-auth-restore-") for a in args), args
    restore = args.index("--restore")
    assert args[restore + 3 :] == [
        "sb-outlook-sync.service",
        "sb-calendar-sync.service",
        "sb-attachments.service",
    ]
    calls = (home / "systemctl-calls").read_text() if (home / "systemctl-calls").exists() else ""
    assert " start " not in f" {calls} ", "a job was started beside the sequence"
    assert "one at a time" in log, log


def test_a_restoration_starts_outlook_sync_through_systemd(tmp_path):
    """Where systemd-run refuses, the jobs still go to systemd, as before."""
    home = _restoration_home(tmp_path / "home", systemctl_ok=True, systemd_run_ok=False)

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


# --- The sequence the transient unit runs: `sb-auth-watch.sh --restore` -------------------

_ALL = [
    "sb-daily-sync.service",
    "sb-outlook-sync.service",
    "sb-calendar-sync.service",
    "sb-teams-sync.service",
    "sb-attachments.service",
]

# A stub systemctl for the sequence. `start` blocks like the real one does for a oneshot
# unit, for as long as $HOME/takes/<unit> says ("hang" never returns); `is-active` reports a
# unit activating for as many polls as $HOME/active/<unit> holds; `show` prints
# $HOME/show/<unit>. Every call is recorded, in order.
_SEQUENCE_SYSTEMCTL = r"""
calls="$HOME/systemctl-calls"
shift
cmd="$1"
shift
case "$cmd" in
  is-active)
    echo "is-active" >> "$calls"
    for u in "$@"; do
      f="$HOME/active/$u"
      n=$(cat "$f" 2>/dev/null || echo 0)
      if [ "$n" -gt 0 ]; then echo activating; echo $((n - 1)) > "$f"; else echo inactive; fi
    done ;;
  show)
    for u in "$@"; do last="$u"; done
    cat "$HOME/show/$last" 2>/dev/null || printf 'Result=success\nExecMainExitTimestamp=\n' ;;
  reset-failed) ;;
  start)
    echo "begin $1" >> "$calls"
    takes=$(cat "$HOME/takes/$1" 2>/dev/null || echo 0)
    [ "$takes" = hang ] && exec sleep 30
    sleep "$takes"
    echo "end $1" >> "$calls" ;;
esac
exit 0
"""


def _sequence_home(tmp_path, takes=None, active=None, show=None) -> Path:
    home = tmp_path / "home"
    (home / ".second-brain").mkdir(parents=True)
    _write_stub(home / ".local" / "bin" / "systemctl", _SEQUENCE_SYSTEMCTL)
    for name, values in (("takes", takes), ("active", active), ("show", show)):
        (home / name).mkdir()
        for unit, value in (values or {}).items():
            (home / name / unit).write_text(f"{value}\n")
    return home


def _restore(home, units, restored_at=None, gcloud_set_at="-", wait="5", poll="0.02"):
    restored_at = int(time.time()) if restored_at is None else restored_at
    log = _run_watcher(
        home,
        "--restore",
        str(restored_at),
        str(gcloud_set_at),
        *units,
        AUTH_RESTORE_WAIT_S=wait,
        AUTH_RESTORE_POLL_S=poll,
    )
    calls = home / "systemctl-calls"
    return log, calls.read_text().splitlines() if calls.exists() else []


def _started(calls):
    return [c.split()[1] for c in calls if c.startswith("begin ")]


def test_the_restored_jobs_run_one_at_a_time_in_order(tmp_path):
    home = _sequence_home(tmp_path, takes=dict.fromkeys(_ALL, "0.1"))

    _log, calls = _restore(home, _ALL)

    assert _started(calls) == _ALL
    runs = [c for c in calls if c.startswith(("begin ", "end "))]
    assert runs == [f"{edge} {u}" for u in _ALL for edge in ("begin", "end")], (
        "a job started before the previous one had finished"
    )


def test_no_job_starts_while_another_is_running(tmp_path):
    """From sb-outlook-sync's pre-flight the mail sync is itself still running."""
    home = _sequence_home(tmp_path, active={"sb-outlook-sync.service": 3})

    _log, calls = _restore(home, ["sb-calendar-sync.service"])

    first_start = calls.index("begin sb-calendar-sync.service")
    assert calls[:first_start].count("is-active") >= 4, calls


def test_a_job_that_overruns_its_wait_does_not_hold_the_rest(tmp_path):
    home = _sequence_home(tmp_path, takes={"sb-outlook-sync.service": "hang"})
    t0 = time.monotonic()

    log, calls = _restore(home, _ALL[1:], wait="0.3", poll="0.05")

    assert time.monotonic() - t0 < 20
    assert _started(calls) == _ALL[1:]
    assert "sb-outlook-sync.service still running after 0.3s" in log, log


def _local_midnight() -> int:
    return int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _ran(epoch: int, result: str = "success") -> str:
    return f"Result={result}\nExecMainExitTimestamp=@{epoch}"


def test_a_daily_sync_that_succeeded_today_before_the_outage_is_not_run_again(tmp_path):
    """On 2026-10-10 it ran 07:08-07:29, then again at 12:02 after a false recovery,
    and the second run replaced the morning's backup."""
    now, midnight = int(time.time()), _local_midnight()
    if now - midnight < 4:
        pytest.skip("too close to midnight to place a run earlier today")
    ran, went_up = midnight + 1, now - 1
    home = _sequence_home(tmp_path, show={"sb-daily-sync.service": _ran(ran)})

    log, calls = _restore(home, _ALL, restored_at=now, gcloud_set_at=went_up)

    assert _started(calls) == _ALL[1:]
    assert "sb-daily-sync already succeeded today" in log, log


def test_a_daily_sync_that_succeeded_during_the_outage_skipped_and_runs_again(tmp_path):
    """sb-daily-sync exits 0 when it skips on needs_gcloud_reauth: not proof it ran."""
    now = int(time.time())
    home = _sequence_home(tmp_path, show={"sb-daily-sync.service": _ran(now - 60)})

    _log, calls = _restore(home, _ALL[:1], restored_at=now, gcloud_set_at=now - 600)

    assert _started(calls) == _ALL[:1]


def test_a_failed_daily_sync_today_runs_again(tmp_path):
    now = int(time.time())
    home = _sequence_home(tmp_path, show={"sb-daily-sync.service": _ran(now - 900, "exit-code")})

    _log, calls = _restore(home, _ALL[:1], restored_at=now, gcloud_set_at=now - 600)

    assert _started(calls) == _ALL[:1]


def test_a_job_that_already_ran_since_the_restore_is_not_run_again(tmp_path):
    """The pre-flight's own mail sync finishes after the restore began, and is the catch-up."""
    now = int(time.time())
    home = _sequence_home(tmp_path, show={"sb-outlook-sync.service": _ran(now + 5)})

    log, calls = _restore(home, _ALL[1:3], restored_at=now)

    assert _started(calls) == ["sb-calendar-sync.service"]
    assert "already ran since the restore" in log, log


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the date with GNU date")
def test_a_systemd_without_unix_timestamps_is_read_too(tmp_path):
    """systemd before 248 has no --timestamp=unix and prints a local date instead."""
    now, midnight = int(time.time()), _local_midnight()
    if now - midnight < 4:
        pytest.skip("too close to midnight to place a run earlier today")
    printed = datetime.fromtimestamp(midnight + 1).strftime("%a %Y-%m-%d %H:%M:%S EEST")
    home = _sequence_home(tmp_path)
    _write_stub(
        home / ".local" / "bin" / "systemctl",
        'case " $* " in *" --timestamp=unix "*) exit 1 ;; esac\n'
        + f'[ "$2" = show ] && {{ printf "Result=success\\nExecMainExitTimestamp={printed}\\n"; exit 0; }}\n'
        + _SEQUENCE_SYSTEMCTL,
    )

    _log, calls = _restore(home, _ALL, restored_at=now, gcloud_set_at=now - 1)

    assert "sb-daily-sync.service" not in _started(calls)


# --- Silent renew on Linux, and who set a sentinel ---------------------------------------


def _failing_auth_home(tmp_path) -> Path:
    """Outlook auth-check and Teams health-check both fail; every renew attempt is recorded."""
    home = tmp_path / "home"
    (home / ".second-brain").mkdir(parents=True)
    bin_dir = home / ".local" / "bin"
    _write_stub(
        bin_dir / "outlook-cli",
        'case "$1" in auth-renew) echo outlook >> "$HOME/renew-calls";; esac\nexit 4\n',
    )
    _write_stub(
        bin_dir / "teams-cli",
        'case "$1" in auth-renew) echo teams >> "$HOME/renew-calls";; '
        'health-check) echo \'{"overall":"degraded","probes":[]}\';; esac\nexit 1\n',
    )
    _write_stub(bin_dir / "gcloud", "exit 0\n")
    _write_stub(home / "osascript", 'echo "osascript $*" >> "$HOME/alerts.txt"\n')
    _write_stub(bin_dir / "terminal-notifier", "exit 0\n")
    return home


def _renews(home) -> list[str]:
    p = home / "renew-calls"
    return p.read_text().split() if p.exists() else []


def test_linux_does_not_attempt_the_silent_renew(tmp_path):
    """1 success in 347 Teams attempts and 0 in 184 Outlook ones on the producer."""
    home = _failing_auth_home(tmp_path)

    log = _run_watcher(home, platform="Linux")

    assert _renews(home) == []
    assert (home / ".second-brain" / "needs_reauth").exists(), "the probe still latches"
    assert "no silent renew on Linux" in log, log


def test_other_platforms_still_attempt_it(tmp_path):
    home = _failing_auth_home(tmp_path)

    _run_watcher(home, platform="Darwin")

    assert sorted(set(_renews(home))) == ["outlook", "teams"]


def test_setting_a_sentinel_logs_who_set_it(tmp_path):
    home = _failing_auth_home(tmp_path)

    log = _run_watcher(home, platform="Linux")

    line = next((ln for ln in log.splitlines() if "set needs_reauth" in ln), "")
    assert "auth-watch pid" in line and "run by" in line, log


def test_a_sentinel_keeps_the_time_it_went_up(tmp_path):
    """Its mtime is "present since" in the health report, and the restore reads it."""
    home = _failing_auth_home(tmp_path)
    sentinel = home / ".second-brain" / "needs_reauth"
    sentinel.touch()
    went_up = time.time() - 3 * 86400
    os.utime(sentinel, (went_up, went_up))

    log = _run_watcher(home, platform="Linux")

    assert abs(sentinel.stat().st_mtime - went_up) < 2, "a failing pass moved the sentinel's time"
    assert "needs_reauth still set" in log, log
