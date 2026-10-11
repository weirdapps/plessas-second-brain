"""scripts/backup_db.py --drill, the scheduled restore drill (issue #150).

The only proof a restore worked was a manual run a month and two schema versions
old, and no written archive was ever decrypted again. The drill decrypts the newest
archive into a private temporary directory, runs PRAGMA quick_check, compares row
counts with the source database, removes the copy, and records a pass where the
health check ages it.

Every archive here is made by the script's own snapshot() and compress_encrypt()
from a store generated in the test, so the drill reads the format the producer
writes and nothing real.
"""

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader, f"could not load {name}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup_db = _load("backup_db")

pytestmark = pytest.mark.skipif(
    not backup_db.tools_available(), reason="zstd/openssl not available"
)

COUNTS = {"emails": 120, "attachments": 60, "teams_messages": 90, "decisions": 40}


def _store(path: Path, counts: dict[str, int], skip: tuple[str, ...] = ()) -> Path:
    conn = sqlite3.connect(path)
    for table, n in counts.items():
        if table in skip:
            continue
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany(
            f"INSERT INTO {table} (body) VALUES (?)", [(f"{table}-{i}",) for i in range(n)]
        )
    conn.commit()
    conn.close()
    return path


def _scribble(db: Path) -> None:
    """Overwrite a table's page, not the header, as a bad disk would."""
    with db.open("r+b") as fh:
        fh.seek(3 * 4096)
        fh.write(b"\xde\xad\xbe\xef" * 256)


@pytest.fixture
def hc():
    return _load("health_check")


@pytest.fixture
def drill(tmp_path, monkeypatch):
    pings: list[tuple[str | None, str]] = []
    monkeypatch.setattr(backup_db, "_ping", lambda slug, suffix="": pings.append((slug, suffix)))
    key = tmp_path / "backup.key"
    key.write_text("a generated passphrase for this test\n")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    source = _store(tmp_path / "brain.db", COUNTS)
    stamp = tmp_path / "state" / "restore-drill.stamp"

    def archive(day="20261010", counts=COUNTS, skip=(), corrupt=False) -> Path:
        plain = _store(tmp_path / f"archived-{day}.db", counts, skip)
        if corrupt:
            # snapshot() would refuse it, which is the point of snapshot(); an
            # archive that holds a bad database anyway is what the drill is for.
            _scribble(plain)
        else:
            snap = tmp_path / f"snapshot-{day}.db"
            backup_db.snapshot(str(plain), str(snap))
            plain = snap
        enc = offsite / f"brain-{day}{backup_db.OFFSITE_SUFFIX}"
        backup_db.compress_encrypt(str(plain), str(enc), str(key))
        return enc

    def run(*extra: str, key_file: Path = key, stamp_path: Path | None = stamp) -> int:
        argv = ["--drill", "--offsite-dir", str(offsite), "--key-file", str(key_file)]
        argv += ["--db", str(source), "--work-dir", str(work), "--hc-slug", "drill"]
        if stamp_path is not None:
            argv += ["--drill-stamp", str(stamp_path)]
        return backup_db.main([*argv, *extra])

    return SimpleNamespace(
        tmp=tmp_path,
        source=source,
        stamp=stamp,
        work=work,
        pings=pings,
        archive=archive,
        run=run,
    )


class TestAPassingDrill:
    def test_a_sound_archive_passes_and_is_recorded(self, drill):
        drill.archive()
        before = drill.source.read_bytes()

        assert drill.run() == 0

        lines = drill.stamp.read_text().splitlines()
        assert (
            timedelta(0)
            <= datetime.now(UTC) - datetime.fromisoformat(lines[0])
            < timedelta(minutes=5)
        )
        assert lines[1] == f"brain-20261010{backup_db.OFFSITE_SUFFIX}"
        assert lines[2].startswith("ok")
        assert drill.pings == [("drill", "")]
        assert list(drill.work.iterdir()) == [], "the decrypted copy must not outlive the drill"
        assert drill.source.read_bytes() == before

    def test_counts_a_day_behind_the_source_pass(self, drill):
        drill.archive(counts={**COUNTS, "emails": 115})

        assert drill.run() == 0

    def test_the_newest_archive_by_name_is_the_one_restored(self, drill):
        drill.archive(day="20261001", counts=dict.fromkeys(COUNTS, 1))
        drill.archive(day="20261010")

        assert drill.run() == 0

        assert (
            drill.stamp.read_text().splitlines()[1] == f"brain-20261010{backup_db.OFFSITE_SUFFIX}"
        )

    def test_the_health_check_ages_the_stamp_the_drill_writes(self, drill, hc):
        drill.archive()
        assert drill.run() == 0
        local = drill.tmp / "backups"
        local.mkdir()
        (local / "brain-20261010.db").write_bytes(b"a snapshot")

        row = hc.check_backups(local, drill_stamp=drill.stamp)

        assert row["drill_age"] is not None
        assert row["drill_age"] < timedelta(minutes=5)

    def test_by_default_it_records_where_the_health_check_looks(self, hc, monkeypatch):
        monkeypatch.setenv("BRAIN_RESTORE_DRILL_STAMP", "")
        assert Path(backup_db.default_drill_stamp()) == hc.RESTORE_DRILL_STAMP

        monkeypatch.setenv("BRAIN_RESTORE_DRILL_STAMP", "/elsewhere/drill.stamp")
        assert backup_db.default_drill_stamp() == "/elsewhere/drill.stamp"

    def test_without_a_stamp_path_it_writes_where_the_environment_points(self, drill, monkeypatch):
        target = drill.tmp / "elsewhere" / "drill.stamp"
        monkeypatch.setenv("BRAIN_RESTORE_DRILL_STAMP", str(target))
        drill.archive()

        assert drill.run(stamp_path=None) == 0

        assert target.is_file()

    def test_a_pulled_replica_is_read_without_a_trace(self, drill, monkeypatch):
        """A replica is WAL-mode with no sidecars, and only an immutable open reads it
        without leaving any: the drill must not create a -wal or -shm beside it."""
        monkeypatch.setenv("BRAIN_ROLE", "replica")
        conn = sqlite3.connect(drill.source)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        assert sorted(p.name for p in drill.tmp.glob("brain.db*")) == ["brain.db"]
        before = drill.source.read_bytes()
        drill.archive()

        assert drill.run() == 0

        assert sorted(p.name for p in drill.tmp.glob("brain.db*")) == ["brain.db"]
        assert drill.source.read_bytes() == before


class TestAFailingDrill:
    def _failed(self, drill) -> None:
        assert not drill.stamp.exists(), "a failed drill must not refresh the stamp"
        assert drill.pings == [("drill", "/fail")]
        assert list(drill.work.iterdir()) == [], "the decrypted copy must not outlive the drill"

    def test_a_wrong_key_fails(self, drill):
        drill.archive()
        other = drill.tmp / "other.key"
        other.write_text("not the passphrase the archive was written with\n")

        assert drill.run(key_file=other) == 1

        self._failed(drill)

    def test_a_restored_copy_that_fails_quick_check_fails(self, drill):
        drill.archive(corrupt=True)

        assert drill.run() == 1

        self._failed(drill)

    def test_counts_far_below_the_source_fail(self, drill):
        drill.archive(counts={**COUNTS, "emails": 30})

        assert drill.run() == 1

        self._failed(drill)

    def test_the_tolerance_is_adjustable(self, drill):
        drill.archive(counts={**COUNTS, "emails": 100})

        assert drill.run() == 1
        assert drill.run("--drill-tolerance", "0.2") == 0

    def test_a_table_missing_from_the_restored_copy_fails(self, drill):
        drill.archive(skip=("decisions",))

        assert drill.run() == 1

        self._failed(drill)

    def test_no_archive_fails(self, drill):
        assert drill.run() == 1

        self._failed(drill)

    def test_a_missing_source_fails(self, drill):
        drill.archive()
        drill.source.unlink()

        assert drill.run() == 1

        self._failed(drill)


@pytest.mark.parametrize(
    "argv",
    [
        ["--drill"],
        ["--drill", "--offsite-dir", "o", "--key-file", "k"],
        ["--drill", "--prune-only", "--offsite-dir", "o", "--key-file", "k", "--db", "d"],
        ["--drill", "--offsite-dir", "o", "--key-file", "k", "--db", "d", "--drill-tolerance", "2"],
    ],
)
def test_a_drill_without_what_it_needs_is_a_usage_error(argv):
    with pytest.raises(SystemExit) as exc:
        backup_db.main(argv)
    assert exc.value.code == 2
