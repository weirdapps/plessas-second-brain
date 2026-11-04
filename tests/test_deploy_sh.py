"""scripts/deploy.sh (issue 173): the producer's deploy as one reconciliation.

Run by the hourly pull's hook after new commits arrive. It syncs the venv from the lock,
installs each wrapper whose installed copy differs (cmp), migrates once, with the sb-*
timers stopped and the running jobs let finish, only when the store is behind the code,
and never applies a gated migration. The tools it calls are stubs on PATH that log their
arguments; HOME is a temp dir. Every value is invented.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "scripts" / "deploy.sh"
WRAPPERS = ROOT / "scripts" / "wrappers" / "systemd"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def host(tmp_path):
    home = tmp_path / "home"
    bindir = tmp_path / "stubs"
    venv = home / ".venvs" / "second-brain" / "bin"
    for d in (home / ".local" / "bin", bindir, venv, home / ".config/systemd/user"):
        d.mkdir(parents=True)
    log = tmp_path / "calls.log"
    _stub(bindir / "uv", f'echo "uv $*" >> {log}; exit ${{UV_RC:-0}}\n')
    # systemctl: lists the active timers and, once, a running job.
    _stub(
        bindir / "systemctl",
        f"""echo "systemctl $*" >> {log}
case "$*" in
  *list-units*timer*) printf 'sb-sync.timer loaded active waiting x\\nsb-teams-sync.timer loaded active waiting x\\n' ;;
  *list-units*service*)
    if [ -n "${{BUSY_FILE:-}}" ] && [ -f "$BUSY_FILE" ]; then
      rm -f "$BUSY_FILE"; printf 'sb-teams-sync.service loaded activating start x\\n'
    elif [ -n "${{ALWAYS_BUSY:-}}" ]; then printf 'sb-sync.service loaded active running x\\n'
    fi
    printf 'sb-mcp.service loaded active running x\\n' ;;
esac
exit 0
""",
    )
    # The venv's python: `-m src.cli maint migrate --check` answers CHECK_RC.
    _stub(
        venv / "python",
        f"""echo "python $*" >> {log}
case "$*" in
  *--check*) echo "store v41, code v42"; exit ${{CHECK_RC:-0}} ;;
  *migrate*) exit ${{MIGRATE_RC:-0}} ;;
esac
exit 0
""",
    )
    (home / ".config/systemd/user/sb-mcp.service").write_text("[Unit]\n")
    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "SB_DEPLOY_POLL_SECONDS": "0",
        "SB_DEPLOY_WAIT_SECONDS": "2",
    }
    return {"home": home, "log": log, "env": env, "tmp": tmp_path}


def _run(host, **extra):
    env = {**host["env"], **extra}
    proc = subprocess.run(
        ["bash", str(DEPLOY)], env=env, capture_output=True, text=True, timeout=60, cwd=ROOT
    )
    calls = host["log"].read_text().splitlines() if host["log"].exists() else []
    return proc, calls


def test_deploy_syncs_from_the_lock(host):
    proc, calls = _run(host)
    assert proc.returncode == 0, proc.stderr
    (uv,) = [c for c in calls if c.startswith("uv ")]
    assert "sync" in uv and "--frozen" in uv and "--no-dev" in uv and "--inexact" in uv


def test_a_failed_uv_sync_stops_the_deploy(host):
    proc, calls = _run(host, UV_RC="1")
    assert proc.returncode != 0
    assert not any("migrate" in c for c in calls)


def test_wrappers_are_installed_only_when_they_differ(host):
    bin_ = host["home"] / ".local" / "bin"
    names = sorted(p.name for p in WRAPPERS.glob("*.sh"))
    same, stale = names[0], names[1]
    shutil.copy(WRAPPERS / same, bin_ / same)
    (bin_ / stale).write_text("#!/bin/bash\n# an old copy\n")
    before = os.stat(bin_ / same).st_mtime_ns
    proc, _ = _run(host)
    assert proc.returncode == 0, proc.stderr
    assert (bin_ / stale).read_bytes() == (WRAPPERS / stale).read_bytes()
    assert os.access(bin_ / stale, os.X_OK)
    assert os.stat(bin_ / same).st_mtime_ns == before  # identical: left alone
    # A wrapper with no installed copy has no unit yet: not installed, but named.
    assert not (bin_ / names[2]).exists()
    assert names[2] in proc.stdout


def test_a_current_store_is_not_migrated_and_no_timer_stops(host):
    proc, calls = _run(host, CHECK_RC="0")
    assert proc.returncode == 0, proc.stderr
    assert any("--check" in c for c in calls)
    assert not any("migrate" in c and "--check" not in c for c in calls if c.startswith("py"))
    assert not any(c.startswith("systemctl --user stop") for c in calls)


def test_a_behind_store_migrates_once_with_the_timers_stopped(host):
    busy = host["tmp"] / "busy"
    busy.write_text("1")
    proc, calls = _run(host, CHECK_RC="3", BUSY_FILE=str(busy))
    assert proc.returncode == 0, proc.stderr
    stop = calls.index(next(c for c in calls if c.startswith("systemctl --user stop")))
    migrate = calls.index(
        next(c for c in calls if c.startswith("python") and "migrate" in c and "--check" not in c)
    )
    start = calls.index(next(c for c in calls if c.startswith("systemctl --user start")))
    assert stop < migrate < start
    assert "sb-sync.timer" in calls[stop] and "sb-teams-sync.timer" in calls[stop]
    assert "sb-sync.timer" in calls[start] and "sb-teams-sync.timer" in calls[start]
    # The running job was waited for: the service list was read again after it.
    waits = [i for i, c in enumerate(calls) if "list-units" in c and "service" in c]
    assert len(waits) >= 2 and waits[-1] < migrate
    assert sum(1 for c in calls if c.startswith("python") and "maint migrate" in c) == 2


def test_deploy_never_applies_a_gated_migration(host):
    _, calls = _run(host, CHECK_RC="3")
    assert not any("--apply-gated" in c for c in calls)
    code = [ln for ln in DEPLOY.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("--apply-gated" in ln for ln in code)


def test_a_failed_migration_still_restarts_the_timers_and_fails(host):
    proc, calls = _run(host, CHECK_RC="3", MIGRATE_RC="1")
    assert proc.returncode != 0
    assert any(c.startswith("systemctl --user start") and "sb-sync.timer" in c for c in calls)


def test_a_job_that_does_not_finish_holds_the_migration(host):
    proc, calls = _run(host, CHECK_RC="3", ALWAYS_BUSY="1")
    assert proc.returncode != 0
    assert not any(c.startswith("python") and "migrate" in c and "--check" not in c for c in calls)
    assert any(c.startswith("systemctl --user start") for c in calls)


def test_a_failed_check_fails_the_deploy_without_migrating(host):
    proc, calls = _run(host, CHECK_RC="1")
    assert proc.returncode != 0
    assert not any(c.startswith("systemctl --user stop") for c in calls)


def test_the_mcp_server_is_restarted_last(host):
    proc, calls = _run(host, CHECK_RC="3")
    assert proc.returncode == 0, proc.stderr
    assert calls[-1] == "systemctl --user try-restart sb-mcp.service"


def test_no_mcp_unit_no_restart(host):
    (host["home"] / ".config/systemd/user/sb-mcp.service").unlink()
    proc, calls = _run(host)
    assert proc.returncode == 0, proc.stderr
    assert not any("try-restart" in c for c in calls)


def test_deploy_logs_to_the_data_home(host):
    _run(host)
    log = host["home"] / ".second-brain" / "logs" / "deploy.log"
    assert log.exists() and "deploy" in log.read_text()
