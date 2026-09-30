"""Content hashes for attachment files.

The hash has to live in the table because the file does not stay. Once its content is
stored, the sweep deletes it (src/store/file_sweep.py), and from then on the hash is the
only evidence of what it held. The orphan reaper matches re-downloads against it, and the
image rule matches inline_images on it.
"""

import sqlite3
from pathlib import Path

from src.config import ATTACHMENTS_DIR
from src.extract.image_classifier import sha256_of_file

__all__ = ["HASH_BATCH", "hash_attachments", "sha256_of_file"]

HASH_BATCH = 500


def _locate(file_path: str, root: Path) -> Path | None:
    """The file behind a row: its recorded path, else the same directory and name under root.

    Rows written by the retired Mac exporter carry Mac paths while their files sit in this
    host's tree, which is also how the sweep finds them (by directory and name).
    """
    recorded = Path(file_path)
    if recorded.is_file():
        return recorded
    here = root / recorded.parent.name / recorded.name
    return here if here.is_file() else None


def hash_attachments(
    conn: sqlite3.Connection, limit: int | None = None, root: Path | None = None
) -> dict:
    """Fill attachments.sha256 from the files still on disk.

    Resumable: it selects only rows without a hash, so an interrupted run carries on where
    it stopped. A row whose file cannot be found keeps a NULL hash and counts as missing. It
    then cannot be proved a duplicate of anything, which is the safe side for the reaper.
    """
    root = ATTACHMENTS_DIR if root is None else Path(root)
    stats = {"hashed": 0, "missing": 0}
    query = "SELECT id, file_path FROM attachments WHERE sha256 IS NULL ORDER BY id"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    for n, (att_id, file_path) in enumerate(conn.execute(query).fetchall(), 1):
        path = _locate(file_path, root) if file_path else None
        if path is None:
            stats["missing"] += 1
            continue
        conn.execute(
            "UPDATE attachments SET sha256 = ? WHERE id = ?", (sha256_of_file(path), att_id)
        )
        stats["hashed"] += 1
        if n % HASH_BATCH == 0:
            conn.commit()
    conn.commit()
    return stats
