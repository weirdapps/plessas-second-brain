"""scripts/wrappers/launchd/sb-db-pull.sh, run whole against a stand-in producer.

ssh runs its command in a directory standing in for the producer's home, so the
WAL checkpoint, the producer's report on its brain.db and the `.backup` snapshot
all run for real (real sqlite3, real python). rsync copies from that directory
the way rsync lands a file, and pgrep reads a process table the test writes. The
readiness gate is the real wait-for-vps.sh, with its waits set to zero.

What these pin (issue #149):
  * verify, then swap: a new copy that fails its check never replaces the replica,
    and the embedding index is only replaced alongside a database that passed;
  * an unchanged producer costs no snapshot: its `.backup` is not rewritten;
  * the DEFER guard sees the conversation sync's writers as well.
"""

import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

REPO = Path(__file__).parent.parent
WRAPPER = REPO / "scripts" / "wrappers" / "launchd" / "sb-db-pull.sh"
GATE = WRAPPER.parent / "wait-for-vps.sh"
BACKUP_DB = REPO / "scripts" / "backup_db.py"
# The wrapper's own layout, the same relative path on both hosts.
DATA = Path("SourceCode") / "plessas-second-brain" / "data"
PRODUCER_ROWS = 2000
REPLICA_ROWS = 300

pytestmark = pytest.mark.skipif(
    not (shutil.which("sqlite3") and shutil.which("unzip")),
    reason="the pull shells out to sqlite3 and unzip",
)

SSH_STUB = r"""#!/bin/bash
# ssh [-o option]... host command: run the command in the stand-in producer home.
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
if [ -n "${FAKE_SSH_DOWN:-}" ]; then
  echo "ssh: connect to host: No route to host" >&2
  exit 255
fi
cd "$FAKE_REMOTE" && HOME="$FAKE_REMOTE" exec bash -c "$*"
"""

RSYNC_STUB = r"""#!/bin/bash
# rsync [option]... host:path dest: copy from the stand-in producer home, landing
# each file as rsync does (a temporary name, then a rename), mode and mtime kept.
args=()
for a in "$@"; do
  case "$a" in -*) ;; *) args+=("$a") ;; esac
done
[ -n "${FAKE_SSH_DOWN:-}" ] && exit 255
src="${args[0]#*:}"
src="${src#\~/}"
dest="${args[1]}"
printf '%s\n' "$src" >> "$FAKE_RSYNC_LOG"
shopt -s nullglob
matches=( "$FAKE_REMOTE"/$src )
if [ ${#matches[@]} -eq 0 ]; then
  echo "rsync: link_stat \"$src\" failed: No such file or directory (2)" >&2
  exit 23
fi
for f in "${matches[@]}"; do
  if [ -d "$dest" ]; then out="${dest%/}/$(basename "$f")"; else out="$dest"; fi
  tmp="$(dirname "$out")/.$(basename "$out").partial"
  cp -p "$f" "$tmp" && mv -f "$tmp" "$out" || exit 11
done
"""

PGREP_STUB = r"""#!/bin/bash
# pgrep -f PATTERN over the command lines listed one per line in $FAKE_PROCS.
pattern="${!#}"
[ -f "${FAKE_PROCS:-}" ] || exit 1
hit=1
n=0
while IFS= read -r cmdline; do
  n=$((n + 1))
  if printf '%s\n' "$cmdline" | grep -Eq -- "$pattern"; then
    echo $((4000 + n))
    hit=0
  fi
done < "$FAKE_PROCS"
exit $hit
"""


def _store(path: Path, rows: int, tag: str) -> None:
    """A WAL-mode store as both hosts keep it, closed cleanly so no sidecar is left."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS emails "
        "(id INTEGER PRIMARY KEY, message_id INTEGER, date_received TEXT, subject TEXT)"
    )
    conn.executemany(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
        [(i + 1, f"2026-10-{1 + i % 28:02d}T08:00:00Z", f"{tag}-{i}") for i in range(rows)],
    )
    conn.commit()
    conn.close()


def _index(path: Path, ids: range) -> None:
    np.savez(path, ids=np.array(ids, dtype=np.int64), vectors=np.ones((len(ids), 4), np.float32))


def _subjects(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT subject FROM emails ORDER BY id")]
    finally:
        conn.close()


def _index_ids(path: Path) -> list[int]:
    with np.load(path) as data:
        return [int(i) for i in data["ids"]]


def _quick_check(db: Path) -> str:
    out = subprocess.run(
        ["sqlite3", str(db), "PRAGMA quick_check;"], capture_output=True, text=True, timeout=60
    )
    return (out.stdout + out.stderr).strip()


def _scribble(db: Path) -> None:
    """Overwrite a b-tree page, not the header, as a bad disk would."""
    with db.open("r+b") as fh:
        fh.seek(3 * 4096)
        fh.write(b"\xde\xad\xbe\xef" * 256)


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def world(tmp_path):
    home, remote, bindir = tmp_path / "home", tmp_path / "remote", tmp_path / "bin"
    for name, body in (("ssh", SSH_STUB), ("rsync", RSYNC_STUB), ("pgrep", PGREP_STUB)):
        _executable(bindir / name, body)
    python = f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
    _executable(remote / ".venvs" / "second-brain" / "bin" / "python", python)
    _executable(home / "SourceCode" / "plessas-second-brain" / ".venv" / "bin" / "python3", python)
    _executable(home / ".local" / "bin" / "wait-for-vps.sh", GATE.read_text())
    scripts = home / "SourceCode" / "plessas-second-brain" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "backup_db.py").symlink_to(BACKUP_DB)

    producer, replica = remote / DATA, home / DATA
    producer.mkdir(parents=True)
    replica.mkdir(parents=True)
    _store(producer / "brain.db", PRODUCER_ROWS, "new")
    _index(producer / "embeddings.npz", range(PRODUCER_ROWS))
    _store(replica / "brain.db", REPLICA_ROWS, "old")
    _index(replica / "embeddings.npz", range(REPLICA_ROWS))
    offsite = producer / "backups" / "offsite"
    offsite.mkdir(parents=True)
    (offsite / "brain-20261010.db.zst.enc").write_bytes(b"ciphertext")

    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
        "FAKE_REMOTE": str(remote),
        "FAKE_RSYNC_LOG": str(tmp_path / "rsync.log"),
        "FAKE_PROCS": str(tmp_path / "procs"),
        # A host that cannot resolve, and the transport by name: should anything
        # slip past the stubs it fails here instead of reaching a real producer.
        "SB_PULL_VPS": "sb-pull-test.invalid",
        "SB_PULL_RSYNC": str(bindir / "rsync"),
        "SB_PULL_LOCK": str(tmp_path / "pull.lock"),
        "SB_PULL_WAIT_BUDGET": "0",
        "SB_PULL_WAIT_INTERVAL": "0",
    }

    def run(**extra):
        return subprocess.run(
            ["/bin/bash", str(WRAPPER)],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=300,
        )

    def log() -> str:
        path = home / ".second-brain" / "logs" / "db-pull.log"
        return path.read_text() if path.exists() else ""

    def snapshot() -> Path:
        found = sorted((remote / ".second-brain").glob("brain.snapshot.*.db"))
        assert len(found) == 1, f"expected one producer-side snapshot, found {found}"
        return found[0]

    return SimpleNamespace(
        home=home,
        remote=remote,
        producer=producer,
        replica=replica,
        procs=tmp_path / "procs",
        stamp=home / ".second-brain" / "db-pull.stamp",
        run=run,
        log=log,
        snapshot=snapshot,
    )


def _leftovers(replica: Path) -> list[str]:
    return sorted(p.name for p in replica.iterdir() if ".new" in p.name or ".partial" in p.name)


class TestVerifyThenSwap:
    def test_a_sound_copy_replaces_both_files_and_stamps(self, world):
        out = world.run()

        assert out.returncode == 0, world.log()
        assert _subjects(world.replica / "brain.db") == _subjects(world.producer / "brain.db")
        assert _index_ids(world.replica / "embeddings.npz") == list(range(PRODUCER_ROWS))
        assert _leftovers(world.replica) == []
        assert world.stamp.is_file()

    def test_a_copy_that_fails_quick_check_never_replaces_the_replica(self, world):
        _scribble(world.producer / "brain.db")
        assert _quick_check(world.producer / "brain.db") != "ok", "the premise: a bad source"
        replica_before = (world.replica / "brain.db").read_bytes()
        index_before = (world.replica / "embeddings.npz").read_bytes()

        out = world.run()

        assert out.returncode == 1
        assert (world.replica / "brain.db").read_bytes() == replica_before
        # The index stays paired with the database that stayed.
        assert (world.replica / "embeddings.npz").read_bytes() == index_before
        assert _leftovers(world.replica) == []
        assert not world.stamp.exists()
        assert "ERROR" in world.log()

    def test_an_index_that_is_not_a_sound_zip_never_replaces_the_old_one(self, world):
        index_before = (world.replica / "embeddings.npz").read_bytes()
        (world.producer / "embeddings.npz").write_bytes(b"not an archive at all")

        out = world.run()

        assert out.returncode == 1
        assert (world.replica / "embeddings.npz").read_bytes() == index_before
        assert _leftovers(world.replica) == []
        assert not world.stamp.exists()

    def test_a_stale_wal_beside_the_replica_is_not_replayed_onto_the_new_copy(self, world):
        """The 2026-08-29 incident, at the swap: a -wal left by the old file must go."""
        expected = _subjects(world.producer / "brain.db")
        (world.replica / "brain.db-wal").write_bytes(b"frames from the old database")
        (world.replica / "brain.db-shm").write_bytes(b"index of the old database")

        assert world.run().returncode == 0, world.log()

        assert not (world.replica / "brain.db-wal").exists()
        assert _subjects(world.replica / "brain.db") == expected


class TestUnchangedProducer:
    def test_an_unchanged_producer_is_not_snapshotted_again(self, world):
        assert world.run().returncode == 0, world.log()
        first = world.snapshot().stat()

        out = world.run()

        assert out.returncode == 0, world.log()
        again = world.snapshot().stat()
        assert (again.st_ino, again.st_mtime_ns) == (first.st_ino, first.st_mtime_ns)
        assert _subjects(world.replica / "brain.db") == _subjects(world.producer / "brain.db")
        assert world.stamp.is_file()

    def test_a_changed_producer_is_snapshotted_again(self, world):
        assert world.run().returncode == 0, world.log()
        first = world.snapshot().stat()
        _store(world.producer / "brain.db", 5, "later")

        assert world.run().returncode == 0, world.log()

        assert world.snapshot().stat().st_mtime_ns != first.st_mtime_ns
        assert _subjects(world.replica / "brain.db")[-1] == "later-4"

    def test_a_snapshot_gone_from_the_producer_is_taken_again(self, world):
        assert world.run().returncode == 0, world.log()
        world.snapshot().unlink()

        assert world.run().returncode == 0, world.log()

        assert world.snapshot().is_file()

    def test_a_failed_run_forgets_the_producer_so_the_next_one_snapshots(self, world):
        assert world.run().returncode == 0, world.log()
        good_index = (world.producer / "embeddings.npz").read_bytes()
        (world.producer / "embeddings.npz").write_bytes(b"not an archive at all")
        assert world.run().returncode == 1
        (world.producer / "embeddings.npz").write_bytes(good_index)
        first = world.snapshot().stat()

        assert world.run().returncode == 0, world.log()

        assert world.snapshot().stat().st_mtime_ns != first.st_mtime_ns


def _defer_pattern() -> str:
    line = next(ln for ln in WRAPPER.read_text().splitlines() if ln.startswith("ACTIVE="))
    m = re.search(r'pgrep -f "([^"]+)"', line)
    assert m, "the DEFER guard's pgrep pattern is missing"
    return m.group(1)


class TestDeferGuard:
    @pytest.mark.parametrize(
        "args",
        [
            "sync --skip-export --workers 8",
            "teams-sync --concurrency 2 --workers 4",
            "calendar-sync",
            "whatsapp-sync",
            "load",
            "export-conversations --days 7",
            "extract-conversations --workers 2",
        ],
    )
    def test_every_writer_on_the_producer_defers_the_pull(self, args):
        cmdline = f"/srv/venv/bin/python -m src.cli {args}"
        found = subprocess.run(["grep", "-Eq", "--", _defer_pattern()], input=cmdline, text=True)
        assert found.returncode == 0, f"the pull would copy while `{args}` writes"

    @pytest.mark.parametrize("cmdline", ["python -m src.cli stats", "python -m src.mcp_server"])
    def test_a_reader_does_not(self, cmdline):
        found = subprocess.run(["grep", "-Eq", "--", _defer_pattern()], input=cmdline, text=True)
        assert found.returncode == 1

    def test_a_running_conversation_extraction_defers_the_whole_run(self, world):
        world.procs.write_text(
            "/srv/venv/bin/python -m src.cli extract-conversations --workers 2\n"
        )
        before = (world.replica / "brain.db").read_bytes()

        out = world.run()

        assert out.returncode == 0
        assert "DEFER" in world.log()
        assert not list((world.remote / ".second-brain").glob("brain.snapshot.*"))
        assert (world.replica / "brain.db").read_bytes() == before


def test_an_unreachable_producer_is_skipped_within_the_configured_wait(world):
    before = (world.replica / "brain.db").read_bytes()

    out = world.run(FAKE_SSH_DOWN="1")

    assert out.returncode == 0
    assert "SKIP" in world.log()
    assert (world.replica / "brain.db").read_bytes() == before
