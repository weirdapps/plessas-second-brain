#!/usr/bin/env python3
"""Reference exporter — the "bring your own source" contract.

The extract + load chain is source-agnostic: it only reads staging batches
from ``staging/batch-*.json`` under the data home (``data/`` in the repository
unless BRAIN_DATA_DIR moves it). This script writes one such batch with no
Microsoft 365 dependency. Adapt ``load_your_messages()`` to read from wherever
your data lives (Gmail API, IMAP, an .mbox, a CSV, a custom API), then run:

    python examples/example_exporter.py     # writes staging/batch-90000.json
    python -m src.extract.local && python -m src.cli load   # on a fresh store
    python -m src.cli sync                   # from then on, extract + load

The only contract is the JSON shape below. Required per-email fields:
message_id, date_received, subject, sender, to_recipients, cc_recipients,
mailbox_name, content. Optional: conversation_id, internet_message_id.
"""

import json
import os
from pathlib import Path

# The data home as src/config.py resolves it: BRAIN_DATA_DIR, else the repo's data/.
STAGING = (
    Path(os.environ.get("BRAIN_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    / "staging"
)


def load_your_messages() -> list[dict]:
    """Replace this with a reader for your own source."""
    return [
        {
            "message_id": "demo-0001",
            "date_received": "2026-01-15T09:00:00Z",
            "subject": "Kickoff notes",
            "sender": {"name": "Alice Smith", "address": "alice@example.com"},
            "to_recipients": [{"name": "You", "address": "you@example.com"}],
            "cc_recipients": [{"name": "Bob Jones", "address": "bob@example.com"}],
            "mailbox_name": "Inbox",
            "content": "Let's ship the pilot by end of quarter. Bob owns the rollout.",
            "conversation_id": "thread-demo-1",
            "internet_message_id": "<demo-0001@example.com>",
        }
    ]


def write_batch(emails: list[dict], batch_number: int = 90000) -> Path:
    STAGING.mkdir(parents=True, exist_ok=True)
    batch = {
        "batch_number": batch_number,
        "exported_at": "2026-01-15T09:05:00Z",
        "source": "example-exporter",
        "folder": "Inbox",
        "emails": emails,
    }
    path = STAGING / f"batch-{batch_number:05d}.json"
    path.write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


if __name__ == "__main__":
    out = write_batch(load_your_messages())
    print(f"Wrote {out}")
