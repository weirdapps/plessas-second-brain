"""push-meeting-transcripts-to-vps.sh: what it sends, the markers it leaves, and how it reads the
fetch's exit code. Transport and CLI are stubbed by name (SB_TX_SSH, SB_TX_RSYNC,
SB_TX_BRAIN), never on PATH, which the script puts Homebrew and /usr/bin ahead of."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parent.parent / "scripts/wrappers/launchd/push-meeting-transcripts-to-vps.sh"
)

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="the push runs under zsh")


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _env(tmp_path: Path, fetch_rc: int = 0, ssh_rc: int = 0, records: int = 1) -> dict:
    calls = tmp_path / "calls"
    state = tmp_path / "state"
    ssh = _stub(tmp_path / "ssh", f'echo "ssh $*" >> "{calls}"\nexit {ssh_rc}\n')
    rsync = _stub(tmp_path / "rsync", f'echo "rsync $*" >> "{calls}"\n')
    writes = "".join(
        f'echo "{{}}" > "{state}/meeting-transcript-outbox/r{i}.json"\n' for i in range(records)
    )
    brain = _stub(tmp_path / "brain", f'echo "brain $*" >> "{calls}"\n{writes}exit {fetch_rc}\n')
    return {
        **os.environ,
        "SB_TX_SSH": str(ssh),
        "SB_TX_RSYNC": str(rsync),
        "SB_TX_BRAIN": str(brain),
        "SB_TX_LOG": str(tmp_path / "push.log"),
        "SB_TX_STATE": str(state),
        "TMPDIR": str(tmp_path),
    }


def _run(env: dict) -> int:
    return subprocess.run(["zsh", str(SCRIPT)], env=env, capture_output=True).returncode


def _calls(tmp_path: Path) -> str:
    return (tmp_path / "calls").read_text()


def test_fetches_then_pushes_the_outbox_and_stamps(tmp_path):
    env = _env(tmp_path)

    assert _run(env) == 0

    calls = _calls(tmp_path)
    assert "brain meetings-fetch --outbox" in calls
    assert "rsync" in calls and "--remove-source-files" in calls
    assert "meeting-transcript-inbox" in calls
    assert (tmp_path / "state" / "meeting-transcript-push.stamp").exists()
    assert not (tmp_path / "state" / "meeting-transcript-push.fail").exists()


def test_nothing_new_still_stamps_without_rsync(tmp_path):
    env = _env(tmp_path, records=0)

    assert _run(env) == 0
    assert "rsync" not in _calls(tmp_path)
    assert (tmp_path / "state" / "meeting-transcript-push.stamp").exists()


def test_an_expired_session_writes_the_marker_with_the_fix(tmp_path):
    env = _env(tmp_path, fetch_rc=2, records=0)

    assert _run(env) == 1
    marker = (tmp_path / "state" / "meeting-transcript-push.fail").read_text()
    assert "log in to M365 again" in marker
    assert "meeting-transcript-push.fail" in _calls(tmp_path)  # on the producer too
    assert not (tmp_path / "state" / "meeting-transcript-push.stamp").exists()


def test_one_failed_transcript_still_pushes_the_rest(tmp_path):
    env = _env(tmp_path, fetch_rc=1)

    assert _run(env) == 0
    assert "rsync" in _calls(tmp_path)
    assert not (tmp_path / "state" / "meeting-transcript-push.fail").exists()


def test_a_passing_search_failure_skips_quietly(tmp_path):
    env = _env(tmp_path, fetch_rc=4, records=0)

    assert _run(env) == 0
    assert not (tmp_path / "state" / "meeting-transcript-push.fail").exists()
    assert not (tmp_path / "state" / "meeting-transcript-push.stamp").exists()


def test_an_unreachable_producer_is_skipped_quietly_at_first(tmp_path):
    env = _env(tmp_path, ssh_rc=255)
    state = tmp_path / "state"
    state.mkdir()
    (state / "meeting-transcript-push.stamp").write_text("")  # a push an hour ago

    assert _run(env) == 0
    assert "brain" not in _calls(tmp_path)
    assert not (state / "meeting-transcript-push.fail").exists()


def test_an_unreachable_producer_pages_after_a_day(tmp_path):
    env = _env(tmp_path, ssh_rc=255)

    assert _run(env) == 1  # no stamp at all: last success is beyond 24h
    assert "unreachable" in (tmp_path / "state" / "meeting-transcript-push.fail").read_text()
