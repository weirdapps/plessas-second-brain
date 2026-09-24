"""Staging and sync-state files: atomic writes, quarantine of unreadable JSON,
and the Outlook sync cursor.

Uses atomic writes (temp file + rename) to prevent corruption.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def write_json_atomic(path: Path, payload, *, indent: int | None = 2, redact: bool = False) -> None:
    """Write JSON so a reader never sees a partial file.

    `redact=True` strips credential-shaped strings first, and every staging BATCH
    writer passes it. This is the one boundary all four sources cross on the way
    into the store, so it is the right place: past it, a secret is in brain.db, in
    the Vertex extraction request, on every replica and in every offsite snapshot
    taken since. It is off by default because this function also writes state
    files, where the pattern set has nothing to match and the walk is waste.

    The state file has always been written this way; the staging BATCH files,
    which are two orders of magnitude larger and therefore far likelier to be
    caught mid-write, were not. All four batch writers opened the destination
    with mode "w", which truncates immediately, so a job killed during the dump
    (a systemd RuntimeMaxSec, a reboot, a full disk) left `batch-NNNNN.json`
    truncated on disk. Every downstream reader does a bare json.load over the
    whole `batch-*.json` glob, so that one file raised JSONDecodeError and
    wedged extract and load for EVERY source until someone deleted it by hand.

    fsync before the rename: the rename is atomic with respect to other
    processes, but without the flush the contents can still be lost on a power
    failure while the rename survives, which is the same corrupt file by a
    slower road.
    """
    if redact:
        from src.redact import redact_payload

        payload = redact_payload(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=indent, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json_or_quarantine(path: Path, logger=None):
    """Parse a staging batch, moving it aside instead of raising.

    Every reader walks the whole `batch-*.json` glob with a bare json.load, so
    one unparseable file used to stop extract and load for EVERY source until a
    human deleted it: the textbook poison item. write_json_atomic above removes
    the cause going forward; this bounds the blast radius of a file already on
    disk, or of one truncated by something outside this code.

    Returns the parsed object, or None if the file was quarantined.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        quarantine = path.parent / "quarantine"
        quarantine.mkdir(parents=True, exist_ok=True)
        dest = quarantine / path.name
        try:
            os.replace(path, dest)
        except OSError:
            dest = path  # could not move it; still skip it
        msg = "Quarantined unparseable staging batch %s -> %s: %s"
        if logger is not None:
            logger.error(msg, path, dest, e)
        else:
            print(msg % (path, dest, e))
        return None


# ---------------------------------------------------------------------------
# Outlook ingestion sync state
# ---------------------------------------------------------------------------

OUTLOOK_STATE_SCHEMA_VERSION = 2


@dataclass
class OutlookSyncState:
    last_sync_started_at: str | None = None
    last_sync_completed_at: str | None = None
    last_seen_received_at: str | None = None
    last_seen_message_id: str | None = None
    messages_in_last_run: int = 0
    consecutive_failures: int = 0
    schema_version: int = OUTLOOK_STATE_SCHEMA_VERSION


def load_outlook_sync_state(path: Path) -> OutlookSyncState:
    if not path.exists():
        return OutlookSyncState()
    with open(path) as f:
        raw = json.load(f)
    return OutlookSyncState(
        **{k: v for k, v in raw.items() if k in OutlookSyncState.__dataclass_fields__}
    )


def save_outlook_sync_state(path: Path, state: OutlookSyncState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(asdict(state), f, indent=2)
    os.replace(tmp, path)
