"""Recover messages with extractions on disk that never made it into the DB.

Symptom: data/staging/batch-*.json contains messages with valid extraction
files at data/extracted/<msg_id>.json, but the messages are missing from the
`emails` table. The state file claims they were extracted, so run_extraction
won't retry; load_extractions doesn't log per-file failures, so silent drops
went unnoticed.

Walks every extraction file, checks against the current staging_index and
DB, and force-loads anything that's missing. Idempotent — safe to re-run.

Run manually after a daily-sync that left a backlog:
    python3 scripts/recover_missing_extractions.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.store.loader import load_single_email  # noqa: E402
from src.store.schema import get_connection  # noqa: E402

STAGING = REPO_ROOT / "data" / "staging"
EXTRACTED = REPO_ROOT / "data" / "extracted"
DB = REPO_ROOT / "data" / "brain.db"


def build_staging_index() -> dict[str, dict]:
    """Same shape as src/store/loader.py — keyed by str(message_id)."""
    idx: dict[str, dict] = {}
    for batch_file in sorted(STAGING.glob("batch-*.json")):
        try:
            with batch_file.open() as f:
                batch = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  skip unreadable batch {batch_file.name}: {e}")
            continue
        for email in batch.get("emails", []):
            idx[str(email["message_id"])] = email
    return idx


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}")
        return 1

    staging_index = build_staging_index()
    print(f"Staging index: {len(staging_index)} messages across batch-*.json")

    conn = get_connection(str(DB))
    conn.execute("PRAGMA busy_timeout = 60000")

    targets: list[str] = []
    for ext_file in sorted(EXTRACTED.glob("*.json")):
        mid = ext_file.stem
        if mid not in staging_index:
            continue
        iid = staging_index[mid].get("internet_message_id")
        row = conn.execute(
            "SELECT 1 FROM emails WHERE message_id = ? "
            "OR (internet_message_id IS NOT NULL AND internet_message_id = ?)",
            (mid, iid),
        ).fetchone()
        if not row:
            targets.append(mid)

    print(f"Recoverable: {len(targets)} extractions exist for messages not in DB")
    if not targets:
        conn.close()
        return 0

    loaded = dup = error = 0
    error_samples: list[tuple[str, str]] = []
    for i, mid in enumerate(targets, 1):
        with (EXTRACTED / f"{mid}.json").open() as f:
            extraction = json.load(f)
        metadata = staging_index[mid]
        try:
            if load_single_email(conn, metadata, extraction):
                loaded += 1
            else:
                dup += 1
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as e:
            error += 1
            if len(error_samples) < 5:
                error_samples.append((mid, f"{type(e).__name__}: {e}"))
        if i % 100 == 0:
            conn.commit()
            print(f"  progress: {i}/{len(targets)}  loaded={loaded}  dup={dup}  error={error}")

    conn.commit()
    conn.close()

    print(f"\nDone. loaded={loaded} dup={dup} error={error}")
    if error_samples:
        print("Sample errors:")
        for mid, msg in error_samples:
            print(f"  {mid[:50]}  →  {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
