"""sb-auth-watch.sh queues a sync that failed for want of a credential once it probes healthy.

The restoration trigger fired only when a sentinel cleared, and most credential gaps
never latch one. On 2026-10-23 the laptop that pushes tokens to the producer slept from
17:27 to 19:43: sb-teams-sync ended 75 seven times on an expired token, and when the push
fixed it the run still waited for the next hourly tick. sb-creds-arrived.path now starts
auth-watch whenever a credential file changes, and auth-watch queues each sync that ended
failed on a credential code while that credential probes healthy.

The real script runs against a throwaway HOME with stub CLIs; systemctl answers `show`
from a table and systemd-run records what it was handed.
"""

import subprocess
from pathlib import Path

_WATCHER = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd" / "sb-auth-watch.sh"


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _home(
    tmp_path: Path,
    units: dict[str, tuple[str, int]],
    *,
    teams_ok: bool = True,
    gcloud_ok: bool = True,
) -> Path:
    """units: {unit: (ActiveState, ExecMainStatus)}; any other unit is inactive, 0."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (home / ".second-brain").mkdir()
    table = "\n".join(
        f'  *{unit}) printf "ActiveState=%s\\nExecMainStatus=%s\\n" {state} {code} ;;'
        for unit, (state, code) in units.items()
    )
    _write_stub(
        bin_dir / "systemctl",
        f"""echo "$*" >> "$HOME/systemctl-calls"
case "$*" in
  *show*)
    case "$*" in
{table}
      *) printf "ActiveState=inactive\\nExecMainStatus=0\\n" ;;
    esac ;;
esac
exit 0
""",
    )
    _write_stub(bin_dir / "systemd-run", 'printf "%s\\n" "$@" > "$HOME/systemd-run-args"\n')
    _write_stub(bin_dir / "gcloud", f"exit {0 if gcloud_ok else 1}\n")
    _write_stub(
        bin_dir / "outlook-cli",
        'case "$1" in auth-check) echo \'{"status":"ok","tokenExpiresAt":"2999-01-01T00:00:00.000Z"}\';; esac\nexit 0\n',
    )
    _write_stub(
        bin_dir / "teams-cli",
        f'case "$1" in health-check) echo \'{{"overall":"ok","probes":[]}}\'; exit {0 if teams_ok else 1};; esac\nexit 0\n',
    )
    _write_stub(bin_dir / "launchctl", "exit 1\n")
    _write_stub(home / "osascript", "exit 0\n")
    _write_stub(bin_dir / "terminal-notifier", "exit 0\n")
    for script in ("sb-teams-sync.sh", "sb-outlook-sync.sh"):
        _write_stub(bin_dir / script, "exit 0\n")
    return home


def _run(home: Path) -> str:
    subprocess.run(
        ["/bin/bash", str(_WATCHER)],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "UID": "501",
            "OSASCRIPT": str(home / "osascript"),
            "AUTH_WATCH_GCLOUD": str(home / ".local" / "bin" / "gcloud"),
            "AUTH_WATCH_PLATFORM": "Linux",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    return (home / ".second-brain" / "logs" / "auth-watch.log").read_text()


def _queued(home: Path) -> list[str]:
    args_file = home / "systemd-run-args"
    if not args_file.exists():
        return []
    args = args_file.read_text().splitlines()
    return args[args.index("--restore") + 3 :]


def test_a_teams_sync_that_ended_69_is_queued_once_teams_probes_healthy(tmp_path):
    home = _home(tmp_path, {"sb-teams-sync.service": ("failed", 69)})

    log = _run(home)

    assert _queued(home) == ["sb-teams-sync.service"]
    assert "teams-sync failed waiting on a credential" in log


def test_a_teams_sync_that_ended_75_is_queued_too(tmp_path):
    home = _home(tmp_path, {"sb-teams-sync.service": ("failed", 75)})

    _run(home)

    assert _queued(home) == ["sb-teams-sync.service"]


def test_a_mail_sync_that_lost_its_session_is_queued(tmp_path):
    home = _home(tmp_path, {"sb-outlook-sync.service": ("failed", 4)})

    log = _run(home)

    assert _queued(home) == ["sb-outlook-sync.service"]
    assert "outlook-sync failed waiting on a credential" in log


def test_a_failure_with_another_code_is_left_to_its_own_alert(tmp_path):
    home = _home(
        tmp_path,
        {"sb-teams-sync.service": ("failed", 1), "sb-outlook-sync.service": ("failed", 75)},
    )

    _run(home)

    assert _queued(home) == []


def test_nothing_is_queued_while_teams_still_fails(tmp_path):
    home = _home(tmp_path, {"sb-teams-sync.service": ("failed", 69)}, teams_ok=False)

    _run(home)

    assert "sb-teams-sync.service" not in _queued(home)


def test_nothing_is_queued_while_gcloud_is_down(tmp_path):
    """Both syncs would only end 69 again on the owed extraction, on every push."""
    home = _home(
        tmp_path,
        {"sb-teams-sync.service": ("failed", 69), "sb-outlook-sync.service": ("failed", 69)},
        gcloud_ok=False,
    )

    _run(home)

    assert _queued(home) == []


def test_a_healthy_estate_queues_nothing(tmp_path):
    home = _home(tmp_path, {})

    log = _run(home)

    assert _queued(home) == []
    assert "auth-trigger" not in log
