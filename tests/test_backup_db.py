"""Tests for scripts/backup_db.py — MVCC-safe snapshot, encryption, retention.

scripts/ is not a package, so the module is loaded by path.
"""

import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest

_BACKUP_PATH = Path(__file__).parent.parent / "scripts" / "backup_db.py"
_spec = importlib.util.spec_from_file_location("backup_db", _BACKUP_PATH)
assert _spec and _spec.loader, f"could not load {_BACKUP_PATH}"
backup_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_db)

_TOOLS = backup_db.tools_available()


def _make_db(path: str, rows: int = 5) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


class TestSnapshot:
    def test_snapshot_is_valid_and_complete(self, tmp_path):
        src = str(tmp_path / "src.db")
        dst = str(tmp_path / "snap.db")
        _make_db(src, rows=7)

        backup_db.snapshot(src, dst)

        conn = sqlite3.connect(dst)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 7
        conn.close()

    def test_snapshot_creates_parent_dir(self, tmp_path):
        src = str(tmp_path / "src.db")
        dst = str(tmp_path / "nested" / "dir" / "snap.db")
        _make_db(src)
        backup_db.snapshot(src, dst)
        assert Path(dst).exists()


@pytest.mark.skipif(not _TOOLS, reason="zstd/openssl not available")
class TestEncryptRoundTrip:
    def test_compress_encrypt_then_decrypt_matches_original(self, tmp_path):
        src = str(tmp_path / "src.db")
        _make_db(src, rows=11)
        key = tmp_path / "backup.key"
        key.write_text("correct horse battery staple\n")

        enc = str(tmp_path / "snap.db.zst.enc")
        backup_db.compress_encrypt(src, enc, str(key))
        # Ciphertext must not contain the SQLite magic header.
        assert Path(enc).read_bytes()[:16] != b"SQLite format 3\x00"

        restored = str(tmp_path / "restored.db")
        backup_db.decrypt_decompress(enc, restored, str(key))

        conn = sqlite3.connect(restored)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 11
        conn.close()

    def test_wrong_key_fails_to_decrypt(self, tmp_path):
        src = str(tmp_path / "src.db")
        _make_db(src)
        good = tmp_path / "good.key"
        good.write_text("passphrase-A\n")
        enc = str(tmp_path / "snap.db.zst.enc")
        backup_db.compress_encrypt(src, enc, str(good))

        bad = tmp_path / "bad.key"
        bad.write_text("passphrase-B\n")
        with pytest.raises(RuntimeError):
            backup_db.decrypt_decompress(enc, str(tmp_path / "out.db"), str(bad))


class TestRetention:
    def _touch(self, directory: Path, stamps, suffix: str):
        for s in stamps:
            (directory / f"brain-{s}{suffix}").write_text("x")

    def test_prune_rolling_keeps_newest_n(self, tmp_path):
        stamps = [f"202607{d:02d}" for d in range(1, 11)]  # 20260701..20260710
        self._touch(tmp_path, stamps, ".db")
        removed = backup_db.prune_rolling(str(tmp_path), backup_db.LOCAL_PATTERN, keep=7)
        remaining = sorted(p.name for p in tmp_path.glob("brain-*.db"))
        assert len(remaining) == 7
        assert "brain-20260710.db" in remaining
        assert "brain-20260701.db" not in remaining
        assert len(removed) == 3

    def test_prune_rolling_takes_a_snapshots_sidecars_with_it(self, tmp_path):
        """A snapshot is a WAL database once it has been opened in WAL mode, and its
        -wal and -shm can outlive the open. The prune matched `brain-*.db` only, so on
        the producer brain-20261011.db went and its -wal and -shm stayed behind. Those
        of a snapshot still kept stay; strays from an earlier prune go now."""
        stamps = [f"202607{d:02d}" for d in range(1, 10)]  # 20260701..20260709
        self._touch(tmp_path, stamps, ".db")
        for suffix in ("-wal", "-shm", "-journal"):
            self._touch(tmp_path, ["20260701", "20260709"], f".db{suffix}")
        self._touch(tmp_path, ["20260601"], ".db-shm")  # its database pruned long ago

        backup_db.prune_rolling(str(tmp_path), backup_db.LOCAL_PATTERN, keep=7)

        left = {p.name for p in tmp_path.iterdir()}
        assert not any(n.startswith(("brain-20260701.", "brain-20260601.")) for n in left)
        assert {"brain-20260709.db-wal", "brain-20260709.db-shm"} <= left
        assert "brain-20260709.db-journal" in left

    def test_prune_gfs_keeps_daily_plus_weekly(self, tmp_path):
        # 14 consecutive daily (all within the daily window) ...
        daily = [f"202607{d:02d}" for d in range(1, 15)]  # 20260701..20260714
        # ... plus older files: two in the same ISO week (Jun 15-21) + two earlier weeks.
        older = ["20260620", "20260619", "20260610", "20260601"]
        self._touch(tmp_path, daily + older, backup_db.OFFSITE_SUFFIX)

        backup_db.prune_gfs(str(tmp_path), backup_db.OFFSITE_PATTERN, daily=14, weekly=8)
        remaining = {p.name for p in tmp_path.glob("brain-*")}

        # All 14 daily kept.
        for s in daily:
            assert f"brain-{s}{backup_db.OFFSITE_SUFFIX}" in remaining
        # Newest-per-week kept; the older same-week file dropped.
        assert f"brain-20260620{backup_db.OFFSITE_SUFFIX}" in remaining
        assert f"brain-20260619{backup_db.OFFSITE_SUFFIX}" not in remaining
        assert f"brain-20260610{backup_db.OFFSITE_SUFFIX}" in remaining
        assert f"brain-20260601{backup_db.OFFSITE_SUFFIX}" in remaining


class TestMainCli:
    def test_main_local_backup_only(self, tmp_path):
        db = str(tmp_path / "brain.db")
        _make_db(db, rows=3)
        local = tmp_path / "backups"
        rc = backup_db.main(["--db", db, "--local-dir", str(local), "--local-keep", "7"])
        assert rc == 0
        snaps = list(local.glob("brain-*.db"))
        assert len(snaps) == 1
        conn = sqlite3.connect(str(snaps[0]))
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
        conn.close()

    def test_main_offsite_skipped_without_key(self, tmp_path):
        db = str(tmp_path / "brain.db")
        _make_db(db)
        local = tmp_path / "backups"
        offsite = tmp_path / "offsite"
        rc = backup_db.main(
            [
                "--db",
                db,
                "--local-dir",
                str(local),
                "--offsite-dir",
                str(offsite),
                "--key-file",
                str(tmp_path / "nonexistent.key"),
            ]
        )
        assert rc == 0
        # Local backup still made; offsite silently skipped (no key).
        assert len(list(local.glob("brain-*.db"))) == 1
        assert not offsite.exists() or not list(offsite.glob("*"))

    @pytest.mark.skipif(not _TOOLS, reason="zstd/openssl not available")
    def test_main_offsite_produced_with_key(self, tmp_path):
        db = str(tmp_path / "brain.db")
        _make_db(db, rows=4)
        local = tmp_path / "backups"
        offsite = tmp_path / "offsite"
        key = tmp_path / "backup.key"
        key.write_text("a-strong-passphrase\n")
        rc = backup_db.main(
            [
                "--db",
                db,
                "--local-dir",
                str(local),
                "--offsite-dir",
                str(offsite),
                "--key-file",
                str(key),
            ]
        )
        assert rc == 0
        enc = list(offsite.glob("*" + backup_db.OFFSITE_SUFFIX))
        assert len(enc) == 1
        # Round-trips back to a valid DB.
        restored = str(tmp_path / "restored.db")
        backup_db.decrypt_decompress(str(enc[0]), restored, str(key))
        conn = sqlite3.connect(restored)
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 4
        conn.close()


def test_tools_available_returns_bool():
    assert isinstance(backup_db.tools_available(), bool)
    # sanity: shutil.which agrees with what the module reported at import time.
    assert _TOOLS == bool(shutil.which("zstd") and shutil.which("openssl"))


def _legacy_archive(src: str, enc: str, key: Path) -> None:
    """An archive as backup_db.py wrote them before 2026-11-15: openssl's default
    10,000 PBKDF2 iterations and no MAC sidecar."""
    import subprocess

    zst = subprocess.run(["zstd", "-q", "-c", src], capture_output=True, check=True).stdout
    subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{key}", "-out", enc],
        input=zst,
        check=True,
    )


def _flip(path: str, offset: int = 64) -> None:
    with open(path, "r+b") as fh:
        fh.seek(offset)
        byte = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([byte[0] ^ 0x01]))


@pytest.mark.skipif(not _TOOLS, reason="zstd/openssl not available")
class TestArchiveIntegrity:
    """Security audit 2026-11-01: the archives used openssl's default 10,000 PBKDF2
    iterations and carried no MAC, so a changed byte was found, if at all, only by
    zstd or SQLite after decryption. New archives use 600,000 iterations and an
    HMAC-SHA256 sidecar that is checked before anything is decrypted."""

    @pytest.fixture
    def made(self, tmp_path):
        src = str(tmp_path / "src.db")
        _make_db(src, rows=9)
        key = tmp_path / "backup.key"
        key.write_text("an invented passphrase for this test\n")
        enc = str(tmp_path / f"brain-20261110{backup_db.OFFSITE_SUFFIX}")
        backup_db.compress_encrypt(src, enc, str(key))
        return tmp_path, src, key, enc

    def test_a_new_archive_has_an_owner_only_mac_sidecar_and_no_leftovers(self, made):
        tmp_path, _src, _key, enc = made
        mac = Path(enc + backup_db.MAC_SUFFIX)
        assert mac.is_file()
        assert (mac.stat().st_mode & 0o777) == 0o600
        assert (Path(enc).stat().st_mode & 0o777) == 0o600
        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == sorted(["src.db", "backup.key", Path(enc).name, mac.name])

    def test_a_new_archive_uses_the_raised_iteration_count(self, made):
        import subprocess

        _tmp, _src, key, enc = made
        cmd = ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", f"file:{key}"]
        new = subprocess.run([*cmd, "-iter", "600000", "-in", enc], capture_output=True)
        old = subprocess.run([*cmd, "-in", enc], capture_output=True)
        assert backup_db.PBKDF2_ITER == 600000
        assert new.returncode == 0
        assert old.returncode != 0 or old.stdout != new.stdout

    def test_the_mac_key_is_not_the_passphrase_and_differs_per_key(self, tmp_path):
        a, b = tmp_path / "a.key", tmp_path / "b.key"
        a.write_text("first invented passphrase\n")
        b.write_text("second invented passphrase\n")
        ka, kb = backup_db.mac_key(str(a)), backup_db.mac_key(str(b))
        assert len(ka) == 32 and ka != kb
        assert b"first invented passphrase" not in ka

    def test_a_sound_new_archive_round_trips(self, made):
        tmp_path, _src, key, enc = made
        restored = str(tmp_path / "restored.db")
        backup_db.decrypt_decompress(enc, restored, str(key))
        conn = sqlite3.connect(restored)
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 9
        conn.close()

    def test_a_changed_archive_is_refused_before_decryption(self, made, monkeypatch):
        tmp_path, _src, key, enc = made
        _flip(enc)
        started = []
        real_popen = backup_db.subprocess.Popen
        monkeypatch.setattr(
            backup_db.subprocess,
            "Popen",
            lambda *a, **k: started.append(a) or real_popen(*a, **k),
        )
        out = tmp_path / "out.db"
        with pytest.raises(RuntimeError, match="MAC"):
            backup_db.decrypt_decompress(enc, str(out), str(key))
        assert started == [], "nothing may be decrypted before the MAC holds"
        assert not out.exists()

    def test_a_changed_mac_is_refused(self, made):
        tmp_path, _src, key, enc = made
        mac = Path(enc + backup_db.MAC_SUFFIX)
        text = mac.read_text()
        last = text.rstrip()[-1]
        mac.write_text(text.rstrip()[:-1] + ("0" if last != "0" else "1") + "\n")
        with pytest.raises(RuntimeError, match="MAC"):
            backup_db.decrypt_decompress(enc, str(tmp_path / "out.db"), str(key))

    def test_a_new_archive_without_its_mac_is_refused(self, made):
        """Deleting the sidecar must not be a way around the check: an archive that
        decrypts with the new iteration count was written with a MAC."""
        tmp_path, _src, key, enc = made
        Path(enc + backup_db.MAC_SUFFIX).unlink()
        out = tmp_path / "out.db"
        with pytest.raises(RuntimeError, match="MAC"):
            backup_db.decrypt_decompress(enc, str(out), str(key))
        assert not out.exists()

    def test_a_legacy_archive_still_decrypts_and_says_it_is_unverified(self, tmp_path, capsys):
        src = str(tmp_path / "src.db")
        _make_db(src, rows=6)
        key = tmp_path / "backup.key"
        key.write_text("an invented passphrase for this test\n")
        enc = str(tmp_path / f"brain-20261001{backup_db.OFFSITE_SUFFIX}")
        _legacy_archive(src, enc, key)

        restored = str(tmp_path / "restored.db")
        backup_db.decrypt_decompress(enc, restored, str(key))

        conn = sqlite3.connect(restored)
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 6
        conn.close()
        out = capsys.readouterr().out
        assert "no MAC" in out and "legacy" in out

    def test_a_legacy_archive_with_a_wrong_key_still_fails(self, tmp_path):
        src = str(tmp_path / "src.db")
        _make_db(src)
        key = tmp_path / "backup.key"
        key.write_text("passphrase-A\n")
        enc = str(tmp_path / f"brain-20261001{backup_db.OFFSITE_SUFFIX}")
        _legacy_archive(src, enc, key)
        bad = tmp_path / "bad.key"
        bad.write_text("passphrase-B\n")
        out = tmp_path / "out.db"
        with pytest.raises(RuntimeError):
            backup_db.decrypt_decompress(enc, str(out), str(bad))
        assert not out.exists()


class TestMacRetention:
    def test_prune_gfs_takes_each_archives_mac_with_it(self, tmp_path):
        stamps = [f"202607{d:02d}" for d in range(1, 6)]
        for s in stamps:
            (tmp_path / f"brain-{s}{backup_db.OFFSITE_SUFFIX}").write_text("x")
            (tmp_path / f"brain-{s}{backup_db.OFFSITE_SUFFIX}{backup_db.MAC_SUFFIX}").write_text(
                "m"
            )
        # A sidecar whose archive went in an earlier prune.
        (tmp_path / f"brain-20260601{backup_db.OFFSITE_SUFFIX}{backup_db.MAC_SUFFIX}").write_text(
            "m"
        )

        backup_db.prune_gfs(str(tmp_path), backup_db.OFFSITE_PATTERN, daily=2, weekly=0)

        left = sorted(p.name for p in tmp_path.iterdir())
        assert left == sorted(
            f"brain-{s}{backup_db.OFFSITE_SUFFIX}{extra}"
            for s in ("20260705", "20260704")
            for extra in ("", backup_db.MAC_SUFFIX)
        )
