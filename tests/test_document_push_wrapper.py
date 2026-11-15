"""push-documents-to-vps.sh: what it sends, the stamp it leaves, and its quiet skip.

The transport is stubbed by name (SB_DOCS_SSH, SB_DOCS_RSYNC), never on PATH: the
script puts Homebrew and /usr/bin first, so a PATH stub would be passed over for the
real ssh to the real producer.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts/wrappers/launchd/push-documents-to-vps.sh"

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="the push runs under zsh")


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _env(tmp_path: Path, ssh_rc: int = 0) -> dict:
    calls = tmp_path / "calls"
    ssh = _stub(tmp_path / "ssh", f'echo "ssh $*" >> "{calls}"\nexit {ssh_rc}\n')
    # The stub rsync keeps the NUL-separated list it was handed, one name a line.
    rsync = _stub(
        tmp_path / "rsync",
        f'for a in "$@"; do case "$a" in --files-from=*) '
        f'tr "\\000" "\\n" < "${{a#--files-from=}}" >> "{tmp_path / "sent"}";; esac; done\n'
        f'echo "rsync $*" >> "{calls}"\n',
    )
    return {
        **os.environ,
        "DOCPUSH_MATERIALIZE_ON": "1",
        "SB_DOCS_SSH": str(ssh),
        "SB_DOCS_RSYNC": str(rsync),
        "SB_DOCS_ROOT": str(tmp_path / "National"),
        "SB_DOCS_SINCE": "202610070000",
        "SB_DOCS_LOG": str(tmp_path / "push.log"),
        "SB_DOCS_STATE": str(tmp_path / "state"),
        "TMPDIR": str(tmp_path),
    }


def _file(root: Path, rel: str, stamp: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    subprocess.run(["touch", "-t", stamp, str(path)], check=True)


def _run(env: dict) -> int:
    return subprocess.run(["zsh", str(SCRIPT)], env=env, capture_output=True).returncode


def _sent(tmp_path: Path) -> list[str]:
    path = tmp_path / "sent"
    return sorted(path.read_text().split()) if path.exists() else []


def test_the_first_run_sends_what_changed_since_the_retirement_and_nothing_else(tmp_path):
    root = tmp_path / "National"
    _file(root, "units/cards/new.pdf", "202611010900")
    _file(root, "_from_downloads_20261025/study/notes.md", "202610250900")
    _file(root, "units/cards/old.pdf", "202609010900")  # before SB_DOCS_SINCE
    _file(root, "units/cards/~$new.docx", "202611010900")  # an Office lock file
    _file(root, "units/cards/photo.jpg", "202611010900")  # not a format it reads
    env = _env(tmp_path)

    assert _run(env) == 0

    assert _sent(tmp_path) == ["./_from_downloads_20261025/study/notes.md", "./units/cards/new.pdf"]
    assert (tmp_path / "state" / "document-push.stamp").exists()
    assert "doc-inbox/National" in (tmp_path / "calls").read_text()


def test_the_next_run_sends_only_what_changed_since_the_last_one(tmp_path):
    root = tmp_path / "National"
    _file(root, "a.pdf", "202611010900")
    env = _env(tmp_path)
    assert _run(env) == 0
    (tmp_path / "sent").unlink()

    assert _run(env) == 0
    assert _sent(tmp_path) == []
    assert "pushed 0 file(s)" in (tmp_path / "push.log").read_text()


def test_an_unreachable_producer_is_skipped_quietly(tmp_path):
    (tmp_path / "National").mkdir()
    env = _env(tmp_path, ssh_rc=255)

    assert _run(env) == 0
    assert not (tmp_path / "state" / "document-push.fail").exists()
    assert "skipping (1 consecutive" in (tmp_path / "push.log").read_text()


def test_a_missing_tree_fails_and_says_so(tmp_path):
    env = _env(tmp_path)

    assert _run(env) == 1
    assert "MISSING" in (tmp_path / "state" / "document-push.fail").read_text()


def test_a_file_that_arrives_with_an_old_mtime_goes_next_run(tmp_path):
    # Copied or synced in keeping its original date: its mtime is long before the stamp,
    # but its status-change time is the day it arrived.
    root = tmp_path / "National"
    _file(root, "a.pdf", "202611010900")
    env = _env(tmp_path)
    assert _run(env) == 0
    (tmp_path / "sent").unlink()

    _file(root, "units/cards/copied.pdf", "202501010900")
    assert _run(env) == 0
    assert _sent(tmp_path) == ["./units/cards/copied.pdf"]


def test_a_pushed_file_whose_status_alone_changed_is_not_sent_again(tmp_path):
    root = tmp_path / "National"
    _file(root, "a.pdf", "202611010900")
    env = _env(tmp_path)
    assert _run(env) == 0
    (tmp_path / "sent").unlink()

    (root / "a.pdf").chmod(0o600)  # a new ctime, same size and mtime
    assert _run(env) == 0
    assert _sent(tmp_path) == []


def test_a_pushed_file_that_changed_goes_again(tmp_path):
    root = tmp_path / "National"
    _file(root, "a.pdf", "202611010900")
    env = _env(tmp_path)
    assert _run(env) == 0
    (tmp_path / "sent").unlink()

    (root / "a.pdf").write_text("revised")
    subprocess.run(["touch", "-t", "202611010900", str(root / "a.pdf")], check=True)
    assert _run(env) == 0
    assert _sent(tmp_path) == ["./a.pdf"]


def test_a_failed_push_is_not_recorded_as_sent(tmp_path):
    root = tmp_path / "National"
    _file(root, "a.pdf", "202611010900")
    env = _env(tmp_path)
    failing = _stub(tmp_path / "rsync-fail", "exit 12\n")
    assert _run({**env, "SB_DOCS_RSYNC": str(failing)}) == 1

    assert _run(env) == 0
    assert _sent(tmp_path) == ["./a.pdf"]


def test_partial_transfers_wait_in_a_hidden_dir_the_ingest_skips(tmp_path):
    _file(tmp_path / "National", "a.pdf", "202611010900")
    env = _env(tmp_path)
    assert _run(env) == 0

    rsync_line = next(
        line for line in (tmp_path / "calls").read_text().splitlines() if line.startswith("rsync")
    )
    assert "--partial-dir=.rsync-partial" in rsync_line
    assert "--partial " not in rsync_line
