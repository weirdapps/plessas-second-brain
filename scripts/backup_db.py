#!/usr/bin/env python3
"""MVCC-safe, compressed, encrypted local + offsite backups of brain.db.

Replaces the old ``shutil.copy2`` backup, which raw-copies a live file and can
capture a torn WAL (an inconsistent, possibly unopenable snapshot). This uses
SQLite's online-backup API for an MVCC-consistent snapshot, verifies it with
``PRAGMA integrity_check``, then optionally compresses (zstd) and encrypts
(openssl AES-256) an offsite copy.

Retention:
  * local   — rolling ``--local-keep`` most-recent snapshots (default 7)
  * offsite — GFS: ``--gfs-daily`` daily + ``--gfs-weekly`` weekly (newest per
    ISO week)

Encryption uses ``openssl enc -aes-256-cbc -pbkdf2 -iter 600000`` — universal and
zero-install (``age``/``rclone crypt`` are absent on both hosts). The key file holds a
passphrase; keep a copy OFF every offsite target or the backups are
unrecoverable. ``compress_encrypt`` is the single swap point if age is ever
installed on both hosts.

``openssl enc`` carries no MAC, so each archive gets a sidecar ``<archive>.mac``: an
HMAC-SHA256 over the encrypted bytes, under a key derived from the passphrase by its
own PBKDF2 pass (another salt, so it is not the encryption key). Decryption checks it
before anything is decrypted. Archives written before 2026-11-15 have no sidecar and
used openssl's default 10,000 iterations; they still decrypt, and the log says they
were not verified (security audit 2026-11-01).

Offsite is scoped by key-file presence: a host without the key (e.g. the Mac
replica) silently skips the encrypted copy, so only the authoritative host (the
VPS, where the key lives) produces offsite snapshots.

Usage (VPS, from sb-daily-sync.sh):
    python scripts/backup_db.py --db data/brain.db \\
        --local-dir data/backups --local-keep 7 \\
        --offsite-dir data/backups/offsite --key-file ~/.second-brain/backup.key

Usage (Mac, from sb-db-pull.sh — GFS-prune the pulled offsite copies):
    python scripts/backup_db.py --prune-only \\
        --offsite-dir ~/second-brain-backups/offsite --gfs-daily 14 --gfs-weekly 8

Usage (Mac, weekly: the restore drill on the newest pulled archive, docs/RESTORE.md):
    python scripts/backup_db.py --drill --offsite-dir ~/second-brain-backups/offsite \\
        --key-file ~/.second-brain/backup.key --db data/brain.db --hc-slug brain-restore-drill
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from datetime import UTC, date, datetime
from glob import glob
from pathlib import Path
from typing import NamedTuple

LOCAL_PATTERN = "brain-*.db"
OFFSITE_SUFFIX = ".db.zst.enc"
OFFSITE_PATTERN = "brain-*.db.zst.enc"

# PBKDF2 iterations for the encryption key. Archives up to 2026-11-15 took openssl's
# default for -pbkdf2, 10,000; new ones take 600,000, and decryption reads both.
PBKDF2_ITER = 600_000
LEGACY_PBKDF2_ITER = 10_000

# The integrity sidecar beside each archive: "v1 hmac-sha256 iter=<n> <hex digest>".
MAC_SUFFIX = ".mac"
MAC_VERSION = "v1"
# The salt of the MAC key's own PBKDF2 pass. openssl draws a random salt for the
# encryption key, so the two keys never coincide.
MAC_SALT = b"second-brain backup archive mac v1"
# zstd's frame magic: what a correctly decrypted archive starts with.
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# What the restore drill counts in the restored copy and in the database it is
# compared with: the four tables every other one hangs off.
DRILL_TABLES = ("emails", "attachments", "teams_messages", "decisions")
# How far a restored count may sit from the source's, as a share of the larger of the
# two. The newest archive is up to a day older than the database it is compared with,
# so equality is not the test; an archive of an empty, cut short or stale database is.
DRILL_TOLERANCE = 0.10

# Every file this script writes is the whole corpus, plaintext or encrypted.
# sqlite and open() take the process umask, which left the local plaintext
# snapshots at 0644 on 2026-09-25, so the mode is set explicitly instead.
BACKUP_MODE = 0o600


def _private_file(path: str) -> None:
    """Create ``path`` owner-only before any content lands in it.

    Created first, not chmod-ed afterwards, so the bytes are never readable by
    others even while being written. The chmod covers a file an earlier run
    left under the same name, whose mode O_CREAT does not touch.
    """
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, BACKUP_MODE))
    os.chmod(path, BACKUP_MODE)


def _log(msg: str) -> None:
    print(f"[backup_db] {msg}", flush=True)


def _ping(slug: str | None, suffix: str = "") -> None:
    """Ping a Healthchecks slug, if one was requested and a base URL is in env.

    The caller (sb-daily-sync.sh) runs this step with `|| echo WARN` and the
    wrapper has no `set -e`, so a failed backup could not turn any check red:
    hc-success@sb-daily-sync fired whether or not a snapshot was taken. Making
    the step fatal would have been worse, because a transient backup failure
    would then also abort that day's mail and Teams ingest. So the backup
    reports on its own slug instead, and the sync keeps its own verdict.

    HC_PING_URL is a secret and is read from the environment only. It is never
    a default and never a CLI argument, because this repo is public.

    Best-effort by design: a monitoring call must not be able to fail a backup
    that has already succeeded.
    """
    base = os.environ.get("HC_PING_URL", "").rstrip("/")
    if not slug or not base:
        return
    try:
        with urllib.request.urlopen(f"{base}/{slug}{suffix}", timeout=10) as r:
            r.read()
    except Exception as exc:  # noqa: BLE001 - never let telemetry break a backup
        _log(f"healthchecks ping failed ({slug}{suffix}): {exc}")


def snapshot(db_path: str, dest_path: str) -> str:
    """MVCC-consistent online backup of ``db_path`` -> ``dest_path``, verified.

    ``sqlite3.Connection.backup()`` holds a read lock only for the copy and is
    safe against a concurrent writer, unlike ``shutil.copy2`` on a live WAL DB.
    The snapshot is validated with ``PRAGMA integrity_check`` and discarded if the
    check fails, so a corrupt snapshot never masquerades as a good backup.
    """
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    _private_file(dest_path)
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    check = sqlite3.connect(dest_path)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        os.unlink(dest_path)
        raise RuntimeError(f"integrity_check failed for snapshot: {result}")
    return dest_path


def tools_available() -> bool:
    """True when both zstd and openssl are on PATH (required for offsite copies)."""
    return bool(shutil.which("zstd") and shutil.which("openssl"))


def _passphrase(key_file: str) -> bytes:
    """The passphrase as ``openssl -pass file:`` reads it: the file's first line."""
    with open(key_file, "rb") as f:
        return f.readline().rstrip(b"\r\n")


def mac_key(key_file: str) -> bytes:
    """The HMAC key of the archives' sidecars: PBKDF2-SHA256 of the passphrase under
    MAC_SALT, at the encryption's iteration count. Stretched as hard as the encryption
    key, so a sidecar is no cheaper a target for guessing the passphrase."""
    return hashlib.pbkdf2_hmac("sha256", _passphrase(key_file), MAC_SALT, PBKDF2_ITER)


def archive_mac(path: str, key: bytes) -> str:
    """HMAC-SHA256 of the file at ``path``, hex."""
    digest = hmac.new(key, digestmod=hashlib.sha256)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_mac(enc_path: str) -> tuple[int, str] | None:
    """(iterations, hex digest) from ``enc_path``'s sidecar, None when it has none."""
    try:
        with open(enc_path + MAC_SUFFIX, encoding="ascii") as f:
            fields = f.read().split()
    except FileNotFoundError:
        return None
    try:
        version, algorithm, iterations, digest = fields
        if version != MAC_VERSION or algorithm != "hmac-sha256":
            raise ValueError(version)
        return int(iterations.removeprefix("iter=")), digest
    except ValueError:
        raise RuntimeError(f"unreadable MAC sidecar: {enc_path}{MAC_SUFFIX}") from None


def _openssl_dec(key_file: str, iterations: int) -> list[str]:
    return [
        "openssl",
        "enc",
        "-d",
        "-aes-256-cbc",
        "-pbkdf2",
        "-iter",
        str(iterations),
        "-pass",
        f"file:{key_file}",
    ]


def compress_encrypt(src_path: str, dest_path: str, key_file: str) -> str:
    """zstd-compress then AES-256-encrypt ``src_path`` -> ``dest_path`` (streamed), and
    write its MAC sidecar ``dest_path + MAC_SUFFIX``.

    Written under a hidden name the archive pattern does not match, and renamed into
    place after its sidecar, so a pull never sees the archive without it.
    """
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    folder, name = os.path.split(dest_path)
    tmp = os.path.join(folder, f".{name}.tmp")
    tmp_mac = os.path.join(folder, f".{name}{MAC_SUFFIX}.tmp")
    _private_file(tmp)
    try:
        with open(tmp, "wb") as out:
            zst = subprocess.Popen(["zstd", "-q", "-c", src_path], stdout=subprocess.PIPE)
            enc = subprocess.Popen(
                [
                    "openssl",
                    "enc",
                    "-aes-256-cbc",
                    "-pbkdf2",
                    "-iter",
                    str(PBKDF2_ITER),
                    "-salt",
                    "-pass",
                    f"file:{key_file}",
                ],
                stdin=zst.stdout,
                stdout=out,
            )
            assert zst.stdout is not None
            zst.stdout.close()  # let zstd get SIGPIPE if openssl exits early
            enc_rc = enc.wait()
            zst_rc = zst.wait()
        if zst_rc != 0 or enc_rc != 0:
            raise RuntimeError(f"compress/encrypt failed (zstd={zst_rc}, openssl={enc_rc})")
        _private_file(tmp_mac)
        with open(tmp_mac, "w", encoding="ascii") as f:
            digest = archive_mac(tmp, mac_key(key_file))
            f.write(f"{MAC_VERSION} hmac-sha256 iter={PBKDF2_ITER} {digest}\n")
        os.replace(tmp_mac, dest_path + MAC_SUFFIX)
        os.replace(tmp, dest_path)
    finally:
        for leftover in (tmp, tmp_mac):
            if os.path.exists(leftover):
                os.unlink(leftover)
    return dest_path


def _decrypts_at(enc_path: str, key_file: str, iterations: int) -> bool:
    """Whether ``enc_path`` decrypts at ``iterations``: openssl's padding check holds
    and what comes out starts as a zstd frame. Nothing is written."""
    dec = subprocess.Popen(
        [*_openssl_dec(key_file, iterations), "-in", enc_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert dec.stdout is not None
    head = dec.stdout.read(len(ZSTD_MAGIC))
    while dec.stdout.read(1 << 20):
        pass
    dec.stdout.close()
    return dec.wait() == 0 and head == ZSTD_MAGIC


def _decrypt_at(enc_path: str, dest_path: str, key_file: str, iterations: int) -> None:
    _private_file(dest_path)
    with open(dest_path, "wb") as out:
        dec = subprocess.Popen(
            [*_openssl_dec(key_file, iterations), "-in", enc_path], stdout=subprocess.PIPE
        )
        unz = subprocess.Popen(["zstd", "-q", "-d"], stdin=dec.stdout, stdout=out)
        assert dec.stdout is not None
        dec.stdout.close()
        unz_rc = unz.wait()
        dec_rc = dec.wait()
    if dec_rc != 0 or unz_rc != 0:
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        raise RuntimeError(f"decrypt/decompress failed (openssl={dec_rc}, zstd={unz_rc})")


def decrypt_decompress(enc_path: str, dest_path: str, key_file: str) -> str:
    """Reverse of :func:`compress_encrypt`, for verification and restore.

    With a sidecar, its MAC must hold before anything is decrypted. Without one, the
    archive must be a legacy one (it decrypts only at LEGACY_PBKDF2_ITER): an archive
    that decrypts at PBKDF2_ITER was written with a sidecar, so a missing one is
    refused rather than read as unverified. A legacy archive is restored, and the log
    says it was not verified.
    """
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(enc_path)
    sidecar = _read_mac(enc_path)
    if sidecar is not None:
        iterations, expected = sidecar
        if not hmac.compare_digest(archive_mac(enc_path, mac_key(key_file)), expected):
            raise RuntimeError(
                f"MAC mismatch on {name}: the archive or its {MAC_SUFFIX} changed, or the "
                "key is not the one it was written with; nothing was decrypted"
            )
        _decrypt_at(enc_path, dest_path, key_file, iterations)
        return dest_path
    if _decrypts_at(enc_path, key_file, PBKDF2_ITER):
        raise RuntimeError(
            f"no MAC sidecar for {name}, which was written with {PBKDF2_ITER:,} iterations "
            "and so with a sidecar; refusing to trust it"
        )
    _decrypt_at(enc_path, dest_path, key_file, LEGACY_PBKDF2_ITER)
    _log(
        f"{name}: no MAC sidecar, a legacy archive ({LEGACY_PBKDF2_ITER:,} iterations); "
        "decrypted, integrity not verified by MAC"
    )
    return dest_path


# SQLite's files beside a database: a snapshot opened in WAL mode can leave -wal and -shm.
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def prune_rolling(directory: str, pattern: str, keep: int) -> list[str]:
    """Keep the newest ``keep`` files matching ``pattern`` (by name); delete the rest.

    A deleted database's sidecars go with it, and so do sidecars whose database is
    already gone: ``brain-*.db`` never matched them, so on the producer the prune of
    brain-20261011.db left its -wal and -shm behind on the data disk.
    """
    files = sorted(glob(os.path.join(directory, pattern)), reverse=True)
    removed = []
    for old in files[keep:]:
        os.unlink(old)
        removed.append(old)
    for suffix in SQLITE_SIDECARS:
        for sidecar in glob(os.path.join(directory, pattern + suffix)):
            if not os.path.exists(sidecar[: -len(suffix)]):
                os.unlink(sidecar)
    return removed


def _dated_files(directory: str, pattern: str) -> list[tuple[date, str]]:
    """``[(date, path)]`` for files named ``brain-YYYYMMDD*``, newest first."""
    out = []
    for p in glob(os.path.join(directory, pattern)):
        name = os.path.basename(p)
        try:
            stamp = name.split("brain-", 1)[1][:8]
            d = datetime.strptime(stamp, "%Y%m%d").date()
        except (IndexError, ValueError):
            continue
        out.append((d, p))
    return sorted(out, reverse=True)


def prune_gfs(directory: str, pattern: str, daily: int, weekly: int) -> list[str]:
    """Grandfather-father-son retention.

    Keep the newest ``daily`` snapshots, plus the newest-per-ISO-week for the most
    recent ``weekly`` weeks among the older ones. Delete everything else.
    """
    files = _dated_files(directory, pattern)
    keep = {p for _, p in files[:daily]}
    per_week: dict[tuple[int, int], str] = {}
    for d, p in files[daily:]:
        wk = d.isocalendar()[:2]  # (iso_year, iso_week); files are desc so first=newest
        if wk not in per_week:
            per_week[wk] = p
    for wk in sorted(per_week, reverse=True)[:weekly]:
        keep.add(per_week[wk])
    removed = []
    for _, p in files:
        if p not in keep:
            os.unlink(p)
            removed.append(p)
    # An archive's MAC sidecar goes with it, and so does one whose archive is gone.
    for sidecar in glob(os.path.join(directory, pattern + MAC_SUFFIX)):
        if not os.path.exists(sidecar[: -len(MAC_SUFFIX)]):
            os.unlink(sidecar)
    return removed


def default_drill_stamp() -> str:
    """Where a passing drill records itself: the file scripts/health_check.py ages.

    BRAIN_RESTORE_DRILL_STAMP when set, else ~/.second-brain/restore-drill.stamp, which
    is the rule health_check.check_backups reads it by.
    """
    return os.environ.get("BRAIN_RESTORE_DRILL_STAMP") or str(
        Path.home() / ".second-brain" / "restore-drill.stamp"
    )


def newest_archive(directory: str) -> str | None:
    """The newest ``brain-YYYYMMDD.db.zst.enc`` in ``directory``, by the date in its name."""
    files = _dated_files(directory, OFFSITE_PATTERN)
    return files[0][1] if files else None


def _count_rows(conn: sqlite3.Connection) -> dict[str, int | None]:
    """Rows in each of DRILL_TABLES, None for a table the database does not have."""
    counts: dict[str, int | None] = {}
    for table in DRILL_TABLES:
        try:
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.OperationalError:  # no such table
            counts[table] = None
    return counts


def _source_counts(db_path: str) -> dict[str, int | None]:
    """Row counts of the database an archive is compared with, read without writing it.

    mode=ro, so nothing can change it. On a replica, immutable=1 as well: a plain
    read-only open of a freshly pulled WAL copy fails, and nothing but the pull writes
    there. Never on the producer, where writers fill the WAL and the locks must stay on.
    src.config.is_replica decides, as it does for every other reader of the store.
    """
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"source database not found: {db_path}")
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    from src.config import is_replica

    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    if is_replica():
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return _count_rows(conn)
    finally:
        conn.close()


class DrillResult(NamedTuple):
    """What a restore drill found. It passed when ``problems`` is empty.

    A NamedTuple, not a dataclass: the tests load this script by path, outside
    sys.modules, where a dataclass under postponed annotations cannot be built.
    """

    archive: str
    quick_check: str
    restored: dict[str, int | None]
    source: dict[str, int | None]
    problems: list[str]


def restore_drill(
    archive: str,
    key_file: str,
    source_db: str,
    work_dir: str | None = None,
    tolerance: float = DRILL_TOLERANCE,
) -> DrillResult:
    """Restore ``archive`` into a private temporary directory and judge what comes back.

    Decrypts and decompresses it with :func:`decrypt_decompress`, runs PRAGMA
    quick_check, and compares the row counts of DRILL_TABLES with ``source_db``, each
    within ``tolerance`` of the larger of the two. The directory, and the plaintext copy
    of the corpus in it, is removed however the drill ends. ``source_db`` is only read.
    Raises when the archive cannot be restored at all.
    """
    want = _source_counts(source_db)
    got: dict[str, int | None] = dict.fromkeys(DRILL_TABLES)
    with tempfile.TemporaryDirectory(prefix="restore-drill-", dir=work_dir) as tmp:
        restored = os.path.join(tmp, "brain.db")
        decrypt_decompress(archive, restored, key_file)
        conn = sqlite3.connect(restored)
        try:
            verdict = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if verdict == "ok":
                got = _count_rows(conn)
        finally:
            conn.close()
    if verdict != "ok":
        return DrillResult(
            os.path.basename(archive), verdict, got, want, [f"quick_check: {verdict}"]
        )
    problems = []
    for table in DRILL_TABLES:
        restored_rows, source_rows = got[table], want[table]
        if restored_rows is None:
            problems.append(f"{table}: missing from the restored copy")
        elif source_rows is None:
            problems.append(f"{table}: missing from {source_db}")
        elif abs(restored_rows - source_rows) > tolerance * max(restored_rows, source_rows):
            problems.append(
                f"{table}: {restored_rows:,} rows restored, {source_rows:,} in the source"
            )
    return DrillResult(os.path.basename(archive), verdict, got, want, problems)


def _counts_line(result: DrillResult) -> str:
    return ", ".join(f"{t} {result.restored[t]:,}/{result.source[t]:,}" for t in DRILL_TABLES)


def _record_drill(path: str, result: DrillResult) -> None:
    """Write the stamp of a passing drill: when (ISO-8601 UTC on line 1, the line the
    health check ages), which archive, and the verdict with the counts."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"{now}\n{result.archive}\nok: quick_check ok; restored/source rows: ")
        f.write(f"{_counts_line(result)}\n")
    os.replace(tmp, path)


def _drill(args: argparse.Namespace) -> int:
    """main()'s --drill: 0, a stamp and a success ping on a pass; 1 and /fail otherwise.

    A failed drill leaves the stamp alone, so the health check's drill age keeps growing
    while the Healthchecks slug carries the failure at once.
    """
    try:
        if not tools_available():
            raise RuntimeError("zstd/openssl not available")
        if not os.path.isfile(args.key_file):
            raise FileNotFoundError(f"key file not found: {args.key_file}")
        archive = newest_archive(args.offsite_dir)
        if archive is None:
            raise FileNotFoundError(f"no {OFFSITE_PATTERN} in {args.offsite_dir}")
        result = restore_drill(archive, args.key_file, args.db, args.work_dir, args.drill_tolerance)
        if not result.problems:
            _record_drill(args.drill_stamp or default_drill_stamp(), result)
    except Exception as exc:  # noqa: BLE001 - every failure is a failed drill, on its slug
        _log(f"restore drill FAILED: {exc}")
        _ping(args.hc_slug, "/fail")
        return 1
    if result.problems:
        _log(f"restore drill FAILED on {result.archive}: {'; '.join(result.problems)}")
        _ping(args.hc_slug, "/fail")
        return 1
    _log(f"restore drill OK on {result.archive}: quick_check ok, {_counts_line(result)}")
    _ping(args.hc_slug)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MVCC-safe brain.db backup")
    ap.add_argument("--db")
    ap.add_argument("--local-dir")
    ap.add_argument("--local-keep", type=int, default=7)
    ap.add_argument("--offsite-dir")
    ap.add_argument("--key-file")
    ap.add_argument("--gfs-daily", type=int, default=14)
    ap.add_argument("--gfs-weekly", type=int, default=8)
    ap.add_argument(
        "--prune-only",
        action="store_true",
        help="Only GFS-prune --offsite-dir (Mac side, run after rsync).",
    )
    ap.add_argument(
        "--hc-slug",
        help=(
            "Healthchecks slug to report this backup on. Pings success only when a "
            "snapshot was actually written, and /fail on every skip or error path. "
            "With --drill, success is a drill that passed. "
            "Requires HC_PING_URL in the environment; ignored without it."
        ),
    )
    ap.add_argument(
        "--drill",
        action="store_true",
        help=(
            "Restore drill: decrypt the newest archive in --offsite-dir with --key-file "
            "into a temporary directory, run PRAGMA quick_check, compare row counts with "
            "--db, remove the copy, and on a pass write the drill stamp. --db is only "
            "read. Exits 1 on any failure."
        ),
    )
    ap.add_argument(
        "--drill-stamp",
        help=(
            "Where a passing drill records itself (default: $BRAIN_RESTORE_DRILL_STAMP, "
            "else ~/.second-brain/restore-drill.stamp, the file health_check.py ages)."
        ),
    )
    ap.add_argument(
        "--drill-tolerance",
        type=float,
        default=DRILL_TOLERANCE,
        help=(
            "How far each restored row count may sit from --db's, as a share of the "
            f"larger of the two (default {DRILL_TOLERANCE})."
        ),
    )
    ap.add_argument(
        "--work-dir",
        help=(
            "Where the drill decrypts to (default: the system temporary directory). "
            "It needs room for the whole database."
        ),
    )
    args = ap.parse_args(argv)

    if args.drill:
        if args.prune_only:
            ap.error("--drill and --prune-only are separate runs")
        needed = (
            ("--offsite-dir", args.offsite_dir),
            ("--key-file", args.key_file),
            ("--db", args.db),
        )
        missing = [flag for flag, value in needed if not value]
        if missing:
            ap.error(f"--drill needs {', '.join(missing)}")
        if not 0 <= args.drill_tolerance < 1:
            ap.error("--drill-tolerance is a share, from 0 up to but not including 1")
        return _drill(args)

    # prune-only is the Mac side tidying copies it pulled. It takes no backup, so
    # it must never report on the backup slug: that would turn the VPS's snapshot
    # green from a machine that did not take one.
    if args.prune_only:
        if not args.offsite_dir or not os.path.isdir(args.offsite_dir):
            _log(f"prune-only: offsite dir missing, nothing to do: {args.offsite_dir}")
            return 0
        removed = prune_gfs(args.offsite_dir, OFFSITE_PATTERN, args.gfs_daily, args.gfs_weekly)
        _log(f"GFS prune removed {len(removed)} offsite snapshot(s)")
        return 0

    # Every `return 0` below is a path where NO backup was taken. They stay 0 so
    # the caller's behaviour is unchanged, but each one pings /fail, because
    # "exited cleanly" and "took a backup" are different facts and only the
    # second one is worth a green check.
    if not args.db or not os.path.exists(args.db):
        _log(f"no database found, skipping backup: {args.db}")
        _ping(args.hc_slug, "/fail")
        return 0

    try:
        # 1. Local MVCC-safe snapshot + rolling retention.
        local_dir = args.local_dir or os.path.join(os.path.dirname(args.db), "backups")
        stamp = date.today().strftime("%Y%m%d")
        local_path = os.path.join(local_dir, f"brain-{stamp}.db")
        snapshot(args.db, local_path)
        _log(f"local snapshot OK: {os.path.basename(local_path)}")
        removed = prune_rolling(local_dir, LOCAL_PATTERN, args.local_keep)
        if removed:
            _log(f"pruned {len(removed)} old local backup(s)")

        # 2. Offsite encrypted copy — scoped by key-file presence (VPS only).
        if args.offsite_dir and args.key_file:
            if not os.path.exists(args.key_file):
                _log(f"offsite skipped: key file not found ({args.key_file})")
                _ping(args.hc_slug, "/fail")
                return 0
            if not tools_available():
                _log("offsite skipped: zstd/openssl not available")
                _ping(args.hc_slug, "/fail")
                return 0
            offsite_path = os.path.join(args.offsite_dir, f"brain-{stamp}{OFFSITE_SUFFIX}")
            compress_encrypt(local_path, offsite_path, args.key_file)
            size_mb = os.path.getsize(offsite_path) / 1e6
            _log(f"offsite encrypted copy OK: {os.path.basename(offsite_path)} ({size_mb:.0f} MB)")
            # Bound the VPS-side offsite dir too (same GFS window the Mac keeps).
            prune_gfs(args.offsite_dir, OFFSITE_PATTERN, args.gfs_daily, args.gfs_weekly)
    except Exception as exc:  # noqa: BLE001 - report, then re-raise for the log
        _log(f"backup FAILED: {exc}")
        _ping(args.hc_slug, "/fail")
        raise

    _ping(args.hc_slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
