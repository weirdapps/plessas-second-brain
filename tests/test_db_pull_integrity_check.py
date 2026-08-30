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
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_WRAPPER = Path(__file__).parent.parent / "scripts" / "wrappers" / "launchd" / "sb-db-pull.sh"

# The sidecar cleanup that has to bracket the rsync. `rm -f` on a file that is
# already gone is a no-op, so running it twice costs nothing and closes the gap
# where a reader recreates a -wal against the newly renamed file.
_SIDECAR_RM = 'rm -f "$LOCAL_DATA/brain.db-wal" "$LOCAL_DATA/brain.db-shm"'
_DB_RSYNC = 'rsync $RSYNC_OPTS "$VPS:~/.second-brain/brain.snapshot.db" "$LOCAL_DATA/brain.db"'

# Captures whatever sits between `sqlite3` and the database path, i.e. the flags
# the wrapper actually passes. An empty capture (a plain open) is the fix.
_INTEGRITY_RE = re.compile(
    r"INTEGRITY=\$\(\s*sqlite3\s+(?P<flags>.*?)\"\$LOCAL_DATA/brain\.db\"\s+'PRAGMA quick_check;'"
)

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")


# Same principle as _INTEGRITY_RE above: read the real command out of the
# wrapper rather than restating it here. A behavioural test that runs its own
# private copy of the cleanup proves only that `rm` works, and stays green while
# the wrapper it is supposed to be guarding loses the line entirely.
_SIDECAR_RM_RE = re.compile(
    r"^\s*(rm -f [^\n]*brain\.db-wal[^\n]*brain\.db-shm[^\n]*)$", re.MULTILINE
)


def _sidecar_cleanup() -> str:
    """The wrapper's own sidecar-removal command, as written in the wrapper."""
    m = _SIDECAR_RM_RE.search(_WRAPPER.read_text())
    assert m, f"no sidecar cleanup command found in {_WRAPPER}"
    return m.group(1).strip()


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


# Builds a database whose -wal OUTLIVES the process, which is the only way to
# reproduce the incident: a clean `conn.close()` checkpoints and unlinks the WAL,
# so a fixture that closes politely can never leave the sidecar the bug needs.
# SIGKILL is the point, not an accident.
#
# `wal_autocheckpoint=0` from the very first statement is the other half. With it
# off, nothing is ever folded back into the main file, so the wal ends up holding
# EVERY page including page 1 and the database file itself stays a stub. That is
# the shape that reproduces: measured here, a 1-frame wal left by a checkpointed
# writer is simply ignored, while a full-coverage one replays in full.
#
# Which makes this the SILENT variant, and the reason these tests assert on
# contents rather than only on quick_check. Replaying a complete older database
# over a newer one leaves a perfectly well-formed file, so quick_check returns
# "ok" and every row is quietly the wrong one. Production caught the louder
# cousin (`sender_signature_index` rowids out of order, from a wal that covered
# only part of the file); this one would have passed the check and been believed.
_WAL_ORPHAN_MAKER = """
import os, signal, sqlite3, sys

path, rows = sys.argv[1], int(sys.argv[2])
conn = sqlite3.connect(path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE emails (id INTEGER PRIMARY KEY, v TEXT)")
conn.executemany("INSERT INTO emails (v) VALUES (?)", [(f"old-{i}",) for i in range(rows)])
conn.commit()
os.kill(os.getpid(), signal.SIGKILL)
"""


def _make_db_with_orphan_wal(path: Path, rows: int = 400) -> None:
    """A database left exactly as a killed local writer leaves one: db + live -wal."""
    proc = subprocess.run(
        [sys.executable, "-c", _WAL_ORPHAN_MAKER, str(path), str(rows)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == -signal.SIGKILL, f"maker did not SIGKILL itself: {proc.stderr}"
    wal = path.with_name(f"{path.name}-wal")
    assert wal.exists() and wal.stat().st_size > 0, (
        "no -wal survived; the fixture is not reproducing the bug"
    )


def _content(db: Path) -> list[str]:
    """What a reader actually sees, WAL replay and all.

    Opening is not read-only in effect: it is the act of opening that replays a
    stale wal, which is precisely how the integrity check damaged the copy it was
    checking.
    """
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM emails ORDER BY id")]
    finally:
        conn.close()


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


class TestStaleSidecarRemoval:
    """The rsync replaces brain.db but cannot know about its -wal and -shm.

    On 2026-08-29 a 4.5 MB -wal, left behind by a local `src.cli embed` that had
    been killed, outlived the file it belonged to. SQLite does not bind a WAL to
    the particular database it was written for, since replay simply writes frame
    page N over page N, so the integrity check's own read-write open replayed
    stale pages onto the freshly copied database and then reported the damage it
    had just caused, `sender_signature_index` rowids out of order, while both the
    VPS original and the snapshot it had copied verified `ok`.

    The copy was never the problem, so no amount of hardening the copy would have
    found this. What matters is that nothing belonging to the old database is
    still lying beside the new one when anything opens it.
    """

    def test_sidecars_are_cleared_on_both_sides_of_the_rsync(self):
        """Before, because the old -wal must not survive the swap; after, because
        a reader holding the old inode can recreate one against the new file
        between the rename and the integrity check."""
        text = _WRAPPER.read_text()
        rsync_at = text.find(_DB_RSYNC)
        assert rsync_at != -1, f"could not find the brain.db rsync in {_WRAPPER}"

        before = text.rfind(_SIDECAR_RM, 0, rsync_at)
        after = text.find(_SIDECAR_RM, rsync_at)
        assert before != -1, "no sidecar cleanup before the rsync: a stale -wal survives the swap"
        assert after != -1, "no sidecar cleanup after the rsync: a reader can recreate one"

    def test_cleanup_removes_sidecars_and_spares_the_database(self, tmp_path):
        """Run the wrapper's own line, not a paraphrase of it."""
        db = tmp_path / "brain.db"
        _make_wal_db(db)
        pristine = db.read_bytes()
        for suffix in ("-wal", "-shm"):
            db.with_name(f"brain.db{suffix}").write_bytes(b"stale frames from the old database")

        subprocess.run(
            ["bash", "-c", f'LOCAL_DATA="{tmp_path}"; {_sidecar_cleanup()}'],
            check=True,
            timeout=30,
        )

        assert not db.with_name("brain.db-wal").exists()
        assert not db.with_name("brain.db-shm").exists()
        assert db.read_bytes() == pristine, "cleanup must not touch the database itself"
        assert _run_check(db, _integrity_flags()) == "ok"

    # The two tests above assert on the wrapper's TEXT and on what `rm` does.
    # Neither reproduces the replay, so commenting both cleanup lines out of the
    # wrapper leaves them green. The two below fail in that state, which is the
    # only reason they are worth their runtime: this check has now been wrong
    # three times, and each time the previous test suite still passed.

    def test_an_orphan_wal_silently_replaces_the_replica_it_lands_beside(self, tmp_path):
        """The incident itself, from scratch: replay writes frame page N over page
        N of whatever file now sits at that path, and SQLite never checks that it
        is the same database the frames came from.

        Note what passes and what fails here: quick_check says "ok". The copy is
        structurally perfect and entirely the wrong data."""
        db = tmp_path / "brain.db"
        _make_db_with_orphan_wal(db)
        snapshot = tmp_path / "brain.snapshot.db"
        _make_wal_db(snapshot, rows=120)
        expected = _content(snapshot)

        shutil.copy(snapshot, db)  # what the rsync does, sidecars untouched
        assert db.with_name("brain.db-wal").exists(), "the orphan -wal is the premise"

        verdict = _run_check(db, _integrity_flags())
        assert verdict != "ok" or _content(db) != expected, (
            "a stale -wal beside a freshly copied replica must not go unnoticed; "
            f"quick_check said {verdict!r} and the contents matched the snapshot"
        )

    def test_the_wrappers_cleanup_makes_the_copy_faithful(self, tmp_path):
        """Same setup, with the wrapper's own rm run first. Asserting on contents
        and not only on quick_check is deliberate: the full-coverage variant of
        this bug replays as a clean whole-file revert that quick_check calls ok."""
        db = tmp_path / "brain.db"
        _make_db_with_orphan_wal(db)
        snapshot = tmp_path / "brain.snapshot.db"
        _make_wal_db(snapshot, rows=120)
        expected = _content(snapshot)

        subprocess.run(
            ["bash", "-c", f'LOCAL_DATA="{tmp_path}"; {_sidecar_cleanup()}'],
            check=True,
            timeout=30,
        )
        shutil.copy(snapshot, db)

        assert _run_check(db, _integrity_flags()) == "ok"
        assert _content(db) == expected, "the replica must be exactly what was copied"
