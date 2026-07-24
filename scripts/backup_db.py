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

Encryption uses ``openssl enc -aes-256-cbc -pbkdf2`` — universal and zero-install
(``age``/``rclone crypt`` are absent on both hosts). The key file holds a
passphrase; keep a copy OFF every offsite target or the backups are
unrecoverable. ``_compress_encrypt`` is the single swap point if age is ever
installed on both hosts.

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
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from glob import glob
from pathlib import Path

LOCAL_PATTERN = "brain-*.db"
OFFSITE_SUFFIX = ".db.zst.enc"
OFFSITE_PATTERN = "brain-*.db.zst.enc"


def _log(msg: str) -> None:
    print(f"[backup_db] {msg}", flush=True)


def snapshot(db_path: str, dest_path: str) -> str:
    """MVCC-consistent online backup of ``db_path`` -> ``dest_path``, verified.

    ``sqlite3.Connection.backup()`` holds a read lock only for the copy and is
    safe against a concurrent writer, unlike ``shutil.copy2`` on a live WAL DB.
    The snapshot is validated with ``PRAGMA integrity_check`` and discarded if the
    check fails, so a corrupt snapshot never masquerades as a good backup.
    """
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
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


def compress_encrypt(src_path: str, dest_path: str, key_file: str) -> str:
    """zstd-compress then AES-256-encrypt ``src_path`` -> ``dest_path`` (streamed)."""
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as out:
        zst = subprocess.Popen(["zstd", "-q", "-c", src_path], stdout=subprocess.PIPE)
        enc = subprocess.Popen(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{key_file}"],
            stdin=zst.stdout,
            stdout=out,
        )
        assert zst.stdout is not None
        zst.stdout.close()  # let zstd get SIGPIPE if openssl exits early
        enc_rc = enc.wait()
        zst_rc = zst.wait()
    if zst_rc != 0 or enc_rc != 0:
        if os.path.exists(dest_path):
            os.unlink(dest_path)
        raise RuntimeError(f"compress/encrypt failed (zstd={zst_rc}, openssl={enc_rc})")
    return dest_path


def decrypt_decompress(enc_path: str, dest_path: str, key_file: str) -> str:
    """Reverse of :func:`compress_encrypt` — for verification and restore."""
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as out:
        dec = subprocess.Popen(
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-pass",
                f"file:{key_file}",
                "-in",
                enc_path,
            ],
            stdout=subprocess.PIPE,
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
    return dest_path


def prune_rolling(directory: str, pattern: str, keep: int) -> list[str]:
    """Keep the newest ``keep`` files matching ``pattern`` (by name); delete the rest."""
    files = sorted(glob(os.path.join(directory, pattern)), reverse=True)
    removed = []
    for old in files[keep:]:
        os.unlink(old)
        removed.append(old)
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
    return removed


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
    args = ap.parse_args(argv)

    if args.prune_only:
        if not args.offsite_dir or not os.path.isdir(args.offsite_dir):
            _log(f"prune-only: offsite dir missing, nothing to do: {args.offsite_dir}")
            return 0
        removed = prune_gfs(args.offsite_dir, OFFSITE_PATTERN, args.gfs_daily, args.gfs_weekly)
        _log(f"GFS prune removed {len(removed)} offsite snapshot(s)")
        return 0

    if not args.db or not os.path.exists(args.db):
        _log(f"no database found, skipping backup: {args.db}")
        return 0

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
            return 0
        if not tools_available():
            _log("offsite skipped: zstd/openssl not available")
            return 0
        offsite_path = os.path.join(args.offsite_dir, f"brain-{stamp}{OFFSITE_SUFFIX}")
        compress_encrypt(local_path, offsite_path, args.key_file)
        size_mb = os.path.getsize(offsite_path) / 1e6
        _log(f"offsite encrypted copy OK: {os.path.basename(offsite_path)} ({size_mb:.0f} MB)")
        # Bound the VPS-side offsite dir too (same GFS window the Mac keeps).
        prune_gfs(args.offsite_dir, OFFSITE_PATTERN, args.gfs_daily, args.gfs_weekly)

    return 0


if __name__ == "__main__":
    sys.exit(main())
