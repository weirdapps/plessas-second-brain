"""Export state management — track progress for resumable export.

Manages export state to enable resumption after crashes or interruptions.
Uses atomic writes (temp file + rename) to prevent corruption.
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TypedDict


def write_json_atomic(path: Path, payload, *, indent: int | None = 2) -> None:
    """Write JSON so a reader never sees a partial file.

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


class ExportState(TypedDict):
    """Export progress state."""

    last_batch_number: int
    last_exported_date: str | None  # ISO format date of last exported email
    total_exported: int
    last_updated: str  # ISO format timestamp


def get_state_file_path() -> Path:
    """Get path to the export state file."""
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "data" / "state" / "export_state.json"


def load_state() -> ExportState:
    """Load export state from file.

    Returns initial state if file doesn't exist or is invalid.
    """
    state_file = get_state_file_path()

    if not state_file.exists():
        return {
            "last_batch_number": 0,
            "last_exported_date": None,
            "total_exported": 0,
            "last_updated": datetime.now().isoformat(),
        }

    try:
        with open(state_file) as f:
            state = json.load(f)
            # Validate required fields
            required = {"last_batch_number", "total_exported"}
            if not all(k in state for k in required):
                raise ValueError(f"Missing required fields: {required - state.keys()}")
            return state
    except (json.JSONDecodeError, ValueError) as e:
        # Corrupted state — start fresh
        print(f"Warning: corrupted state file ({e}), starting fresh")
        return {
            "last_batch_number": 0,
            "last_exported_date": None,
            "total_exported": 0,
            "last_updated": datetime.now().isoformat(),
        }


def save_state(state: ExportState) -> None:
    """Save export state atomically.

    Writes to temp file then renames to prevent corruption on crash.
    """
    state_file = get_state_file_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Update timestamp
    state["last_updated"] = datetime.now().isoformat()

    # Write to temp file
    temp_file = state_file.with_suffix(".json.tmp")
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)

    # Atomic rename
    temp_file.replace(state_file)


def get_last_batch_number() -> int:
    """Get the last successfully exported batch number."""
    state = load_state()
    return state["last_batch_number"]


def update_progress(
    batch_number: int,
    last_email_date: str | None,
    emails_in_batch: int,
) -> None:
    """Update export progress after successful batch export.

    Args:
        batch_number: The batch number just completed
        last_email_date: ISO format date of the last email in this batch
        emails_in_batch: Number of emails exported in this batch
    """
    state = load_state()
    state["last_batch_number"] = batch_number
    state["total_exported"] = state["total_exported"] + emails_in_batch
    if last_email_date:
        state["last_exported_date"] = last_email_date
    save_state(state)


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
