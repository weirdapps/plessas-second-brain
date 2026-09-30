"""Delete attachment files whose content is already in the database.

A file is an input. Once Phase 1 has recorded its text (or recorded it as unreadable), and,
for an image, once the vision pass is done with it, nothing reads the file again: the MCP
tools, Phase 2 and the embeddings all read the database. This module finds those files and,
when the policy allows, deletes them.

It owns REGISTERED files only. A directory no attachments row references belongs to the
orphan reaper (scripts/reap_orphan_attachments.py), which knows the grace periods an
unregistered download needs.

Files are matched to rows by (directory name, filename), the registrar's own identity, never
by absolute path. On the producer data/attachments is a symlink into /mnt/data, so one file is
spelled two ways, and ingest_document files a document under abs(message_id) while its row
keeps the negative id.
"""

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import DATA_ROOT

SWEEP_POLICY_FILE = DATA_ROOT / "state" / "sweep-policy.json"
VISION_ATTEMPTS_LIMIT = 3
# Read by scripts/health_check.py's "Files on disk" row.
STALL_HOURS = 6
IMAGE_WAIT_HOURS = 72

DELETABLE = "deletable"
PENDING_TEXT = "pending-text"
PENDING_IMAGE = "pending-image"
UNREGISTERED = "unregistered"
STATES = (DELETABLE, PENDING_TEXT, PENDING_IMAGE, UNREGISTERED)


@dataclass(frozen=True)
class SweepPolicy:
    """What the sweep may do. The default deletes nothing."""

    apply: bool = False
    only_newer_than: datetime | None = None


@dataclass(frozen=True)
class FileState:
    path: Path
    state: str
    size: int
    mtime: float
    attachment_id: int | None


def parse_timestamp(value: str) -> datetime:
    """An ISO timestamp as an aware datetime; one with no zone is taken as UTC."""
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def load_policy(path: Path | None = None) -> SweepPolicy:
    """Read the sweep policy file. Absent or unreadable means report-only.

    A malformed file must never stop the hourly sync, and must never delete anything: the
    safe reading of a policy nobody can parse is the policy that does nothing. The health
    row prints the mode, so a sweep stuck in report-only shows up there.
    """
    path = SWEEP_POLICY_FILE if path is None else Path(path)
    try:
        raw = json.loads(path.read_text())
        cutoff = raw.get("only_newer_than")
        return SweepPolicy(
            apply=raw.get("apply") is True,
            only_newer_than=parse_timestamp(cutoff) if cutoff else None,
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return SweepPolicy()


def _registered(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[int, bool, bool]]:
    """(directory name, filename) -> (attachment id, content stored, image still owed)."""
    rows = conn.execute(
        """
        SELECT a.id, a.file_path, a.mime_type,
               ac.id IS NOT NULL,
               ii.sha256 IS NOT NULL,
               ii.vision_description IS NOT NULL,
               COALESCE(ii.classification IN ('signature', 'noise'), 0),
               COALESCE(ii.vision_attempts, 0)
        FROM attachments a
        LEFT JOIN attachment_content ac ON ac.attachment_id = a.id
        LEFT JOIN inline_images ii ON ii.sha256 = a.sha256
        WHERE a.file_path IS NOT NULL
        """
    ).fetchall()
    known: dict[tuple[str, str], tuple[int, bool, bool]] = {}
    for att_id, file_path, mime, stored, seen, described, by_design, attempts in rows:
        is_image = (mime or "").startswith("image/")
        image_done = seen and (described or by_design or attempts >= VISION_ATTEMPTS_LIMIT)
        p = Path(file_path)
        known[(p.parent.name, p.name)] = (att_id, bool(stored), is_image and not image_done)
    return known


def classify_files(conn: sqlite3.Connection, root: Path) -> list[FileState]:
    """Every file under root/<directory>/, with the state that decides its fate."""
    known = _registered(conn)
    found: list[FileState] = []
    try:
        dirs = [e for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return found
    for d in dirs:
        try:
            entries = list(os.scandir(d.path))
        except OSError:
            continue
        for e in entries:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
            hit = known.get((d.name, e.name))
            if hit is None:
                state, att_id = UNREGISTERED, None
            else:
                att_id, stored, image_owed = hit
                if not stored:
                    state = PENDING_TEXT
                elif image_owed:
                    state = PENDING_IMAGE
                else:
                    state = DELETABLE
            found.append(FileState(Path(e.path), state, st.st_size, st.st_mtime, att_id))
    return found


def sweep_files(
    conn: sqlite3.Connection, root: Path, policy: SweepPolicy, now: datetime | None = None
) -> dict:
    """Delete the files whose content is stored, as far as the policy allows.

    Report-only unless policy.apply. With a cutoff, only files modified at or after it are
    deleted: that is how stage 4 turns deletion on for new downloads while the backlog waits
    for its own confirmation. A directory is removed only when this pass emptied it.
    """
    now = now or datetime.now(UTC)
    stats: dict = dict.fromkeys(STATES, 0)
    stats.update(
        before_cutoff=0,
        to_delete=0,
        deleted=0,
        bytes_freed=0,
        dirs_removed=0,
        applied=policy.apply,
    )
    cutoff = policy.only_newer_than.timestamp() if policy.only_newer_than else None
    removed: list[int] = []
    emptied: set[Path] = set()
    for f in classify_files(conn, Path(root)):
        stats[f.state] += 1
        if f.state != DELETABLE:
            continue
        if cutoff is not None and f.mtime < cutoff:
            stats["before_cutoff"] += 1
            continue
        stats["to_delete"] += 1
        stats["bytes_freed"] += f.size
        if not policy.apply:
            continue
        try:
            f.path.unlink()
        except FileNotFoundError:
            pass
        stats["deleted"] += 1
        if f.attachment_id is not None:
            removed.append(f.attachment_id)
        emptied.add(f.path.parent)
    if policy.apply and removed:
        conn.executemany(
            "UPDATE attachments SET file_removed_at = ? WHERE id = ?",
            [(now.isoformat(), att_id) for att_id in removed],
        )
        conn.commit()
    for d in emptied:
        try:
            d.rmdir()
        except OSError:
            continue
        stats["dirs_removed"] += 1
    return stats
