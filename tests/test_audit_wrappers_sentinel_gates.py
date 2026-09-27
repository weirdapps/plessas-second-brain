"""A wrapper gates only on the sessions it uses, and every skip says so in its log.

`needs_reauth` means the M365 (Outlook) session is dead. The attachment pass and
the reverse-ingest never call outlook-cli: registration and Phase 1 read files
already on disk, Phase 2 and the image pass call Vertex, the SharePoint pass
uses sharepoint-cli with its own session, and reverse-ingest walks local
folders. Both still skipped on that sentinel, so an Outlook outage stopped them
while their Healthchecks stayed green. sb-curate-docs.sh dropped the same gate
on 2026-09-02 after it cost six days of curation.

The gates that remain are real (Vertex behind needs_gcloud_reauth, outlook-cli
behind needs_reauth for the calendar), but a skip that leaves no trace is
indistinguishable from a job that never started. The attachment pass exited
before its log file was even named, and the conversation sync skipped silently.

The exit code of these skips stays 0 for now: 69, as the mail and Teams
wrappers use, needs RestartPreventExitStatus=69 in each unit's drop-in first.
"""

import subprocess
from pathlib import Path

import pytest

_WRAPPERS = Path(__file__).parent.parent / "scripts" / "wrappers" / "systemd"


def _home(tmp_path: Path, sentinel: str) -> Path:
    home = tmp_path / "home"
    (home / "SourceCode" / "plessas-second-brain").mkdir(parents=True)
    (home / ".second-brain").mkdir(parents=True)
    (home / ".second-brain" / sentinel).touch()
    marker = home / "python-ran"
    python = home / ".venvs" / "second-brain" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/bash\necho "$*" >> {marker}\nexit 0\n')
    python.chmod(0o755)
    return home


def _run(wrapper: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPERS / wrapper)],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "SB_CONVERSATION_SYNC_LOCK": str(home / "conversation-sync.lock"),
            "SB_CURATE_DOCS_LOCK": str(home / "curate-docs.lock"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def _logs(home: Path) -> str:
    return "".join(p.read_text() for p in sorted((home / ".second-brain" / "logs").glob("*.log")))


def test_the_attachment_pass_runs_while_the_outlook_session_is_dead(tmp_path):
    home = _home(tmp_path, "needs_reauth")

    result = _run("sb-attachment-pass.sh", home)

    ran = (home / "python-ran").read_text() if (home / "python-ran").exists() else ""
    assert "register-attachments" in ran and "process-sharepoint" in ran, (
        "the attachment pass skipped on needs_reauth, though none of its stages calls outlook-cli"
    )
    assert result.returncode == 0


def test_reverse_ingest_does_not_gate_on_the_outlook_sentinel(tmp_path):
    home = _home(tmp_path, "needs_reauth")

    _run("sb-reverse-ingest.sh", home)

    log = (home / ".second-brain" / "logs" / "reverse-ingest.log").read_text()
    assert "needs_reauth sentinel present" not in log, log
    # With no Vertex project in the throwaway env it stops at its real guard.
    assert "no Vertex project" in log, log


@pytest.mark.parametrize(
    ("wrapper", "sentinel"),
    [
        ("sb-attachment-pass.sh", "needs_gcloud_reauth"),
        ("sb-reverse-ingest.sh", "needs_gcloud_reauth"),
        ("sb-calendar-sync.sh", "needs_gcloud_reauth"),
        ("sb-calendar-sync.sh", "needs_reauth"),
        ("sb-curate-docs.sh", "needs_gcloud_reauth"),
        ("sb-conversation-sync.sh", "needs_gcloud_reauth"),
    ],
)
def test_every_sentinel_skip_is_logged(tmp_path, wrapper, sentinel):
    home = _home(tmp_path, sentinel)

    _run(wrapper, home)

    assert not (home / "python-ran").exists(), f"{wrapper} ran past its {sentinel} gate"
    logs = _logs(home)
    assert sentinel in logs and "skip" in logs.lower(), (
        f"{wrapper} skipped on {sentinel} without saying so in its log:\n{logs}"
    )
