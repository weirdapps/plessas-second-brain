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
