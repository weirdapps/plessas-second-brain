"""The Mac's replica of brain.db is readable by its owner only.

rsync -a carried the producer's modes across: brain.db arrived 0644 from the
pull snapshot, embeddings.npz 0664, in a 0755 data directory. That is every
mail, Teams message and, since v26, every WhatsApp message in the store,
readable by any other account on the Mac. The block below is read out of the
real wrapper and run, so it cannot drift from what production does.
"""

import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = Path(__file__).parent.parent / "scripts" / "wrappers" / "launchd" / "sb-db-pull.sh"
TEXT = WRAPPER.read_text()


def _block() -> str:
    m = re.search(
        r"# ---- REPLICA-MODES-BEGIN ----\n(.*?)# ---- REPLICA-MODES-END ----", TEXT, re.S
    )
    assert m, "the modes block is missing from sb-db-pull.sh"
    return m.group(1)


def test_the_replica_ends_owner_only(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    for name, mode in (("brain.db", 0o644), ("embeddings.npz", 0o664)):
        (data / name).write_bytes(b"x")
        (data / name).chmod(mode)
    subprocess.run(["/bin/bash", "-c", _block()], env={"LOCAL_DATA": str(data)}, check=True)
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE((data / "brain.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "embeddings.npz").stat().st_mode) == 0o600


def test_the_rsync_options_are_ones_the_macs_own_rsync_accepts(tmp_path):
    """/usr/bin/rsync on macOS is openrsync, which rejects --chmod.

    The first pull after #105 died on "rsync: --chmod=F600: invalid argument" for
    brain.db, the embeddings and the offsite copies alike, and the old test only
    checked that the text was present. Run the real options through the real binary.
    """
    rsync = Path("/usr/bin/rsync")
    if sys.platform != "darwin" or not rsync.exists():
        pytest.skip("the Mac's own rsync is what the LaunchAgent runs")
    opts = re.search(r'^RSYNC_OPTS="([^"]*)"', TEXT, re.M)
    assert opts, "RSYNC_OPTS is missing"
    (tmp_path / "src").write_bytes(b"x")
    out = subprocess.run(
        [str(rsync), *opts.group(1).split(), str(tmp_path / "src"), str(tmp_path / "dst")],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr


def test_the_producer_side_pull_snapshot_ends_owner_only(tmp_path):
    """Run the real remote command: .backup into an old 0644 copy, then the chmod.

    `chmod 600 '$REMOTE_SNAP'` single-quoted a path holding an escaped $HOME, so the
    producer's shell never expanded it, chmod failed, and the whole snapshot step
    reported rc=1 with the copy still 0644.
    """
    line = next(ln for ln in TEXT.splitlines() if ".backup '$REMOTE_SNAP'" in ln)
    home = tmp_path / "home"
    (home / ".second-brain").mkdir(parents=True)
    snap = home / ".second-brain" / "brain.snapshot.test.db"
    snap.write_bytes(b"old")
    snap.chmod(0o644)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "sqlite3"
    # .backup into an existing file keeps its mode, as sqlite3 does.
    fake.write_text(
        '#!/bin/sh\ndest=$(printf "%s" "$2" | sed -e "s/^.backup .//" -e "s/.$//")\n'
        'printf new > "$dest"\n'
    )
    fake.chmod(0o755)
    script = (
        "REMOTE_DATA=data; SNAP_NAME=brain.snapshot.test.db\n"
        'REMOTE_SNAP="\\$HOME/.second-brain/$SNAP_NAME"; SSH_OPTS=""; VPS=producer\n'
        "LOG_FILE=/dev/null\n"
        'ssh() { eval "remote=\\${$#}"; /bin/bash -c "$remote"; }\n' + line.strip()
    )
    env = {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"}
    out = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert snap.read_bytes() == b"new"
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600


def test_the_pull_waits_for_a_whatsapp_sync_too():
    line = next(ln for ln in TEXT.splitlines() if ln.startswith("ACTIVE="))
    assert "whatsapp-sync" in line
