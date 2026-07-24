"""Attachment export state — tracks progress for resumable attachment extraction.

Separate from email export state. Uses atomic writes (temp file + rename).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import TypedDict


class AttachmentExportState(TypedDict):
    """Attachment export progress state."""

    last_processed_index: int
    total_messages_scanned: int
    total_attachments_saved: int
    total_bytes_saved: int
    last_updated: str


def _get_state_path() -> Path:
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "data" / "state" / "attachment_state.json"


def load_state() -> AttachmentExportState:
    """Load attachment export state. Returns initial state if file missing."""
    path = _get_state_path()
    if not path.exists():
        return {
            "last_processed_index": 0,
            "total_messages_scanned": 0,
            "total_attachments_saved": 0,
            "total_bytes_saved": 0,
            "last_updated": datetime.now().isoformat(),
        }
    try:
        with open(path) as f:
            state = json.load(f)
            if "last_processed_index" not in state:
                raise ValueError("Missing last_processed_index")
            return state
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Warning: corrupted attachment state ({e}), starting fresh")
        return {
            "last_processed_index": 0,
            "total_messages_scanned": 0,
            "total_attachments_saved": 0,
            "total_bytes_saved": 0,
            "last_updated": datetime.now().isoformat(),
        }


def save_state(state: AttachmentExportState) -> None:
    """Save state atomically (temp file + rename)."""
    path = _get_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.now().isoformat()
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)
