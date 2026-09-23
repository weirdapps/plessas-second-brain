"""A scheduler wrapper must fail when the command it runs fails.

sb-calendar-sync.sh recorded "FAILED (exit N)" in its log and then exited 0,
because the last command it ran was that echo. sb-conversation-sync.sh never
looked at an exit code at all and logged "ok" unconditionally. systemd saw
success both times, fired hc-success@, and the dead-man switches stayed green
over any failure. These run the real wrappers against a throwaway HOME whose
venv python is a stub with a chosen exit code.
"""

import subprocess
from pathlib import Path

import pytest

_WRAPPERS = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd"


def _home_with_python(tmp_path: Path, body: str) -> Path:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/bash\n" + body)
    python.chmod(0o755)
    return home


def _run(wrapper: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPERS / wrapper)],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/bash",
            # Never the machine-wide lock: a real run holding it would make the
            # wrapper exit 0 before running anything.
            "SB_CONVERSATION_SYNC_LOCK": str(home / "conversation-sync.lock"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_calendar_wrapper_passes_the_command_failure_through(tmp_path):
    home = _home_with_python(tmp_path, "exit 3\n")
    assert _run("sb-calendar-sync.sh", home).returncode == 3


def test_calendar_wrapper_succeeds_when_the_command_does(tmp_path):
    home = _home_with_python(tmp_path, "exit 0\n")
    assert _run("sb-calendar-sync.sh", home).returncode == 0


@pytest.mark.parametrize("failing_step", ["export-conversations", "extract-conversations"])
def test_conversation_wrapper_fails_when_either_step_fails(tmp_path, failing_step):
    home = _home_with_python(tmp_path, f'case "$*" in *{failing_step}*) exit 4;; esac\nexit 0\n')
    assert _run("sb-conversation-sync.sh", home).returncode == 4


def test_conversation_wrapper_still_extracts_after_a_failed_export(tmp_path):
    """The extract step drains what earlier exports staged, so a failed export
    must not stop it; it only has to make the run fail."""
    marker = tmp_path / "extract-ran"
    home = _home_with_python(
        tmp_path,
        'case "$*" in\n'
        "  *export-conversations*) exit 4;;\n"
        f"  *extract-conversations*) touch {marker};;\n"
        "esac\nexit 0\n",
    )

    _run("sb-conversation-sync.sh", home)

    assert marker.exists()


def test_when_both_conversation_steps_fail_the_export_code_wins(tmp_path):
    home = _home_with_python(
        tmp_path,
        'case "$*" in\n'
        "  *export-conversations*) exit 4;;\n"
        "  *extract-conversations*) exit 5;;\n"
        "esac\nexit 0\n",
    )
    assert _run("sb-conversation-sync.sh", home).returncode == 4


def test_conversation_wrapper_succeeds_when_both_steps_do(tmp_path):
    home = _home_with_python(tmp_path, "exit 0\n")
    assert _run("sb-conversation-sync.sh", home).returncode == 0


def test_attachment_pass_runs_every_stage_even_after_one_fails(tmp_path):
    """Under set -e the first failing stage aborted the rest: one poison
    attachment in registration or Phase 1 starved the image and SharePoint
    passes every night. The stages are independent; each runs, and a failure
    ends the pass with 65, which the unit's retry.conf does not restart: every
    stage already ran, so a restart would repeat the hour for nothing."""
    marker = tmp_path / "sharepoint-ran"
    home = _home_with_python(
        tmp_path,
        'case "$*" in\n'
        "  *register-attachments*) exit 5;;\n"
        f"  *process-sharepoint*) touch {marker};;\n"
        "esac\nexit 0\n",
    )

    result = _run("sb-attachment-pass.sh", home)

    assert marker.exists()
    assert result.returncode == 65


def test_attachment_pass_succeeds_when_every_stage_does(tmp_path):
    home = _home_with_python(tmp_path, "exit 0\n")
    assert _run("sb-attachment-pass.sh", home).returncode == 0


def test_a_lock_override_that_is_not_a_lock_path_is_refused(tmp_path):
    """The wrapper deletes its lock directory recursively. An override pointing
    anywhere else would have deleted that directory."""
    home = _home_with_python(tmp_path, "exit 0\n")
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")

    result = subprocess.run(
        ["/bin/bash", str(_WRAPPERS / "sb-conversation-sync.sh")],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "SB_CONVERSATION_SYNC_LOCK": str(victim),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 64
    assert (victim / "keep.txt").exists()


def test_a_failed_attachment_stage_is_named_on_stderr(tmp_path):
    """The unit's own stderr is what the failure alert shows; the per-stage exit
    code used to reach only the wrapper's log file."""
    home = _home_with_python(
        tmp_path, 'case "$*" in *register-attachments*) exit 4;; esac\nexit 0\n'
    )

    result = _run("sb-attachment-pass.sh", home)

    assert "attachment registration FAILED (exit 4)" in result.stderr


def _run_daily(home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPERS / "sb-daily-sync.sh")],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "SB_DAILY_SYNC_LOCK": str(home / "daily-sync.lock"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def _daily_home(tmp_path: Path, body: str) -> Path:
    """A stub python that records every call, then runs `body`."""
    return _home_with_python(tmp_path, 'echo "$*" >> "$HOME/calls.log"\n' + body)


def _calls(home: Path) -> list[str]:
    return (home / "calls.log").read_text().splitlines()


def test_daily_sync_runs_the_action_lifecycle_after_a_successful_sync(tmp_path):
    """Extraction only appends, and the lifecycle job had no caller: nothing
    was ever deduped or aged out, so every action stayed open for good."""
    home = _daily_home(tmp_path, "exit 0\n")

    result = _run_daily(home)

    calls = _calls(home)
    sync = next(i for i, c in enumerate(calls) if "src.cli sync" in c)
    lifecycle = next(i for i, c in enumerate(calls) if "src.store.action_lifecycle" in c)
    assert result.returncode == 0
    assert lifecycle > sync


def test_a_failed_action_lifecycle_does_not_fail_the_daily_sync(tmp_path):
    home = _daily_home(tmp_path, 'case "$*" in *action_lifecycle*) exit 1;; esac\nexit 0\n')

    result = _run_daily(home)

    assert result.returncode == 0
    assert any("src.store.action_lifecycle" in c for c in _calls(home))
    log = (home / ".second-brain" / "logs" / "daily-sync.log").read_text()
    assert "WARN: action lifecycle failed" in log


def test_a_failed_daily_sync_keeps_its_code_and_skips_the_lifecycle(tmp_path):
    home = _daily_home(tmp_path, 'case "$*" in *"src.cli sync"*) exit 3;; esac\nexit 0\n')

    result = _run_daily(home)

    assert result.returncode == 3
    assert not any("action_lifecycle" in c for c in _calls(home))


def test_a_relative_daily_sync_lock_override_is_refused(tmp_path):
    """The wrapper changes directory before its EXIT trap removes the lock, so
    a relative path was made in one directory and removed from another."""
    home = _daily_home(tmp_path, "exit 0\n")

    result = subprocess.run(
        ["/bin/bash", str(_WRAPPERS / "sb-daily-sync.sh")],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SB_DAILY_SYNC_LOCK": "rel.lock"},
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )

    assert result.returncode == 64
    assert not (tmp_path / "rel.lock").exists()


@pytest.mark.parametrize(
    ("wrapper", "variable"),
    [
        ("sb-daily-sync.sh", "SB_DAILY_SYNC_LOCK"),
        ("sb-conversation-sync.sh", "SB_CONVERSATION_SYNC_LOCK"),
    ],
)
def test_a_lock_path_that_is_a_file_is_never_removed(tmp_path, wrapper, variable):
    """mkdir -p failed on it, the run went on without a lock, and the EXIT trap
    ran rm -rf on a file this run never created."""
    home = _daily_home(tmp_path, "exit 0\n")
    precious = tmp_path / "Cargo.lock"
    precious.write_text("x")

    result = subprocess.run(
        ["/bin/bash", str(_WRAPPERS / wrapper)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", variable: str(precious)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 73
    assert precious.read_text() == "x"


def test_a_relative_conversation_sync_lock_override_is_refused(tmp_path):
    home = _daily_home(tmp_path, "exit 0\n")

    result = subprocess.run(
        ["/bin/bash", str(_WRAPPERS / "sb-conversation-sync.sh")],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SB_CONVERSATION_SYNC_LOCK": "rel.lock"},
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )

    assert result.returncode == 64
    assert not (tmp_path / "rel.lock").exists()


def test_a_daily_sync_lock_override_that_is_not_a_lock_path_is_refused(tmp_path):
    home = _daily_home(tmp_path, "exit 0\n")
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")

    result = subprocess.run(
        ["/bin/bash", str(_WRAPPERS / "sb-daily-sync.sh")],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SB_DAILY_SYNC_LOCK": str(victim)},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 64
    assert (victim / "keep.txt").exists()
