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
