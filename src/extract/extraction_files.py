"""Where an email's extraction is kept on disk, and how it is read back.

Outlook message ids are case-sensitive base64 built on a counter, so two messages
26 steps apart can have ids that differ in letter case alone. Extractions were
written as "<message_id>.json" and the loader read them back by the lowercased
name, because macOS folds case in file names. On the producer's Linux disk both
twins' files exist, and each email got whichever of the two the directory listed
last: one email carried the other's summary, decisions, action items and people.
On macOS the second write replaced the first twin's file.

The name now ends in a digest of the exact id, so case twins get two files on any
disk. A file written before keeps its old name, and is taken only when the id
stored inside it is the one asked for: on a disk that folds case, the old name
also finds the twin's file.
"""

import hashlib
import json
from pathlib import Path


def extraction_path(directory: Path, message_id: str) -> Path:
    """The file the extraction of exactly `message_id` is written to."""
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]
    return Path(directory) / f"{message_id}.{digest}.json"


def read_extraction(directory: Path, message_id: str) -> dict | None:
    """The extraction written for exactly `message_id`, or None when there is none.

    A file that stores another message id is not this email's. One that stores none
    (written before ids were stored) is taken by its exact name.
    """
    for path in (extraction_path(directory, message_id), Path(directory) / f"{message_id}.json"):
        if not path.is_file():
            continue
        with open(path, encoding="utf-8") as f:
            extraction = json.load(f)
        stored = extraction.get("message_id")
        if stored is not None and str(stored) != message_id:
            continue
        return extraction
    return None
