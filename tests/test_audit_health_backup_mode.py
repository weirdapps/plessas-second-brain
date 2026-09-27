"""Every snapshot and archive backup_db.py writes is 0600, whatever the umask.

The local snapshots are the whole corpus in plaintext, and on 2026-09-25 they
were found at 0644: sqlite and open() take the process umask, and nothing set
the mode explicitly.
"""

import importlib.util
import os
import sqlite3
import stat
from pathlib import Path

import pytest

_BACKUP_PATH = Path(__file__).parent.parent / "scripts" / "backup_db.py"
_spec = importlib.util.spec_from_file_location("backup_db", _BACKUP_PATH)
assert _spec and _spec.loader
backup_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_db)

_TOOLS = backup_db.tools_available()


@pytest.fixture(autouse=True)
def permissive_umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _make_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


def test_snapshot_is_0600(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src)
    dst = tmp_path / "snap.db"

    backup_db.snapshot(str(src), str(dst))

    assert _mode(dst) == 0o600


def test_a_snapshot_overwriting_an_older_0644_one_is_0600(tmp_path):
    """A second run on the same day writes to the same name."""
    src = tmp_path / "src.db"
    _make_db(src)
    dst = tmp_path / "snap.db"
    dst.write_bytes(b"")
    os.chmod(dst, 0o644)

    backup_db.snapshot(str(src), str(dst))

    assert _mode(dst) == 0o600


@pytest.mark.skipif(not _TOOLS, reason="zstd/openssl not available")
def test_archive_and_restore_are_0600(tmp_path):
    src = tmp_path / "src.db"
    _make_db(src)
    key = tmp_path / "backup.key"
    key.write_text("synthetic passphrase\n")
    enc = tmp_path / "snap.db.zst.enc"

    backup_db.compress_encrypt(str(src), str(enc), str(key))
    restored = tmp_path / "restored.db"
    backup_db.decrypt_decompress(str(enc), str(restored), str(key))

    assert _mode(enc) == 0o600
    assert _mode(restored) == 0o600


def test_main_writes_the_local_snapshot_0600(tmp_path):
    db = tmp_path / "brain.db"
    _make_db(db)
    local = tmp_path / "backups"

    assert backup_db.main(["--db", str(db), "--local-dir", str(local)]) == 0

    snaps = list(local.glob("brain-*.db"))
    assert snaps and all(_mode(p) == 0o600 for p in snaps)
