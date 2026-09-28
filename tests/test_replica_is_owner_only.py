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
from pathlib import Path

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


def test_every_file_arrives_owner_only():
    assert re.search(r'^RSYNC_OPTS="[^"]*--chmod=F600', TEXT, re.M)


def test_the_producer_side_pull_snapshot_is_owner_only():
    line = next(ln for ln in TEXT.splitlines() if ".backup '$REMOTE_SNAP'" in ln)
    assert "umask 077" in line
    assert "chmod 600 '$REMOTE_SNAP'" in line


def test_the_pull_waits_for_a_whatsapp_sync_too():
    line = next(ln for ln in TEXT.splitlines() if ln.startswith("ACTIVE="))
    assert "whatsapp-sync" in line
