"""Tests for the integrity check in scripts/wrappers/launchd/sb-db-pull.sh.

That check has now been wrong twice, in opposite directions, and both times it
was a one-flag mistake that no syntax check could see:

  * before 2026-08-25 it was `SELECT COUNT(*) FROM emails`, which passed on a
    torn database for 386 consecutive runs;
  * from 2026-08-25 it was `sqlite3 -readonly`, which failed on a healthy one
    for all 21 runs it ever made, because brain.db is in WAL mode and the rsync
    that feeds it copies the database file alone, with no `-shm`. A read-only
    open may not create the `-shm` it needs, so it returns "unable to open
    database file (14)" and the job can never exit 0.

Reasoning about flags is what produced both. So these tests do not assert on the
flags: they extract the wrapper's real sqlite3 invocation and run it, once
against a freshly-copied healthy WAL database (must say ok) and once against a
corrupted one (must not). Any future flag that breaks either direction fails
here rather than in production.
"""

import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

_WRAPPER = Path(__file__).parent.parent / "scripts" / "wrappers" / "launchd" / "sb-db-pull.sh"

# Captures whatever sits between `sqlite3` and the database path, i.e. the flags
# the wrapper actually passes. An empty capture (a plain open) is the fix.
_INTEGRITY_RE = re.compile(
    r"INTEGRITY=\$\(\s*sqlite3\s+(?P<flags>.*?)\"\$LOCAL_DATA/brain\.db\"\s+'PRAGMA quick_check;'"
)

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")


def _integrity_flags() -> list[str]:
    """The flags the wrapper passes to sqlite3, read out of the wrapper itself."""
    text = _WRAPPER.read_text()
    m = _INTEGRITY_RE.search(text)
    assert m, f"could not find the quick_check invocation in {_WRAPPER}"
    return m.group("flags").split()


def _make_wal_db(path: Path, rows: int = 50) -> None:
    """A WAL database as the rsync leaves it: the db file alone, no -shm, no -wal.

    The missing -shm is the whole point. A WAL database that still has its
    sidecars opens read-only quite happily, so a fixture that keeps them would
    pass under the exact flag that broke production.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO emails (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])
    conn.commit()
    conn.close()
    path.with_suffix(path.suffix + "-shm").unlink(missing_ok=True)
    path.with_suffix(path.suffix + "-wal").unlink(missing_ok=True)


def _run_check(db: Path, flags: list[str]) -> str:
    """Reproduce the wrapper's pipeline: sqlite3 … | head -3 | tr '\\n' ' '."""
    proc = subprocess.run(
        ["sqlite3", *flags, str(db), "PRAGMA quick_check;"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = (proc.stdout + proc.stderr).splitlines()[:3]
    return " ".join(out).strip()


class TestIntegrityCheck:
    def test_healthy_freshly_copied_wal_db_passes(self, tmp_path):
        """The regression that shipped: a healthy copy must not be called broken."""
        db = tmp_path / "brain.db"
        _make_wal_db(db)
        assert not db.with_name("brain.db-shm").exists(), "fixture must have no -shm"

        assert _run_check(db, _integrity_flags()) == "ok"

    def test_corrupt_db_still_fails(self, tmp_path):
        """The regression before it: the check must not be weakened into a no-op."""
        db = tmp_path / "brain.db"
        _make_wal_db(db, rows=2000)
        with db.open("r+b") as fh:
            fh.seek(3 * 4096)  # scribble over a b-tree page, not the header
            fh.write(b"\xde\xad\xbe\xef" * 256)

        assert _run_check(db, _integrity_flags()) != "ok"
