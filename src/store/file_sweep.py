"""Delete attachment files whose content is already in the database.

A file is an input. Once Phase 1 has extracted its text in full, and, for an image, once the
vision pass is done with it, nothing reads the file again: the MCP tools, Phase 2 and the
embeddings all read the database. This module finds those files and, when the policy allows,
deletes them.

Only a file whose content is HELD goes. A row that failed, was skipped (nothing extractable,
an unsupported type, an image with too little text) or holds an encrypted original records an
outcome, not the content: the file is the only copy of what the database lacks, and a password,
granted rights or a better reader may still open it. Neither goes a picture the vision pass
described in place of text.

A Phase 1 row is not always proof the bytes were read. Phase 1 opens the row's recorded
absolute path, while this module finds files by directory and name, so a row recorded on
another host says "File not found" about a file that is right here. A host missing a tool
(no legacy .doc converter, no tesseract) records a row without reading anything either. Such
files are UNREAD and kept.

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
UNREAD = "unread"
NOT_HELD = "not-held"
PENDING_IMAGE = "pending-image"
UNREGISTERED = "unregistered"
STATES = (DELETABLE, PENDING_TEXT, UNREAD, NOT_HELD, PENDING_IMAGE, UNREGISTERED)

# Phase 1 errors that mean the bytes were never read (src/extract/attachment_extractors.py).
# Shared with the orphan reaper, whose stored-hash rule must not count these rows either.
# "No such file or directory" is how a missing converter used to surface (the older
# "[Errno 2] ... 'textutil'" rows); a data file that vanished mid-read lands here too, which
# errs on the side of keeping it. "left unread" is a read a time budget cut short (an archive's
# members, a scan's pages), which reextract with no budget can finish. "file kept" is one no
# re-read can finish (the text ceiling, an archive's safety limits): the file stays for good.
UNREAD_SQL = (
    "(ac.extraction_error LIKE 'File not found%'"
    " OR ac.extraction_error LIKE 'No legacy .doc converter%'"
    " OR ac.extraction_error LIKE '%not installed%'"
    " OR ac.extraction_error LIKE '%No such file or directory%'"
    " OR ac.extraction_error LIKE '%left unread%'"
    " OR ac.extraction_error LIKE '%file kept%')"
)

# Rows the old readers read in part: spreadsheets stopped at 20 sheets of 51 rows, scans at 30
# pages, a multi-page TIFF at its first page, archives read their members with all of these,
# stored text stopped at 100,000 characters until PR B, and every read at 2,000,000 without
# saying so.
# They count as read in part until reextract --partial reads them again: it sets this
# sync_metadata row when it begins, and every row it reads gets a later extracted_at. Until the
# row exists every such row counts, since the new readers leave no mark of their own.
PARTIAL_SINCE_KEY = "reextract_partial_since"
PARTIAL_READERS = (
    "openpyxl",
    "xlrd",
    "xlrd (fallback from .xlsb)",
    "pyxlsb",
    "pymupdf+tesseract",
    "zip",
)
CUT_LENGTHS = (100_000, 2_000_000)
PARTIAL_SQL = (
    "((ac.extraction_method IN (" + ", ".join(f"'{m}'" for m in PARTIAL_READERS) + ")"
    " OR (ac.extraction_method = 'ocr'"
    " AND (lower(a.filename) LIKE '%.tif' OR lower(a.filename) LIKE '%.tiff'))"
    " OR length(ac.extracted_text) IN (" + ", ".join(str(n) for n in CUT_LENGTHS) + "))"
    " AND COALESCE(ac.extracted_at, '') < COALESCE("
    f"(SELECT value FROM sync_metadata WHERE key = '{PARTIAL_SINCE_KEY}'), '9999'))"
)

# Not stored in full: the sweep keeps the file, the reaper does not count the hash as stored,
# and Phase 1 does not copy the row to another attachment with the same bytes.
NOT_FULLY_READ_SQL = f"({UNREAD_SQL} OR {PARTIAL_SQL})"

# The content is held: Phase 1 extracted the file's text, all of it, and the text is the file's
# own rather than the image pass's description of a picture ('vision' rows keep their image).
# Keyed on the status, never on the error text: 'failed', 'skipped' and 'encrypted' (method
# 'rms', 'rms-message' or 'password') all leave the file as the only copy of its content.
FULLY_HELD_SQL = (
    "(ac.extraction_status = 'extracted'"
    f" AND NOT COALESCE({NOT_FULLY_READ_SQL}, 0)"
    " AND COALESCE(ac.extraction_method, '') <> 'vision')"
)

# The extractor OCRs these by extension whatever the mime says, and ingest_document used to
# record them as application/octet-stream, so an image is known by either.
IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".tiff", ".tif", ".bmp", ".jfif", ".heic", ".heif", ".webp"}
)


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
    attachment_ids: tuple[int, ...]


def parse_timestamp(value: str) -> datetime:
    """An ISO timestamp as an aware datetime; one with no zone is taken as UTC."""
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _read_policy(path: Path) -> SweepPolicy:
    """Parse the policy strictly; raise ValueError on anything but the two documented shapes.

    "only_newer_than" must be present and be either null (the backlog deletion) or a
    non-empty ISO timestamp. A missing key, "", 0 or false would otherwise read as "no
    cutoff", which is the one step the owner confirms by hand.
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or "only_newer_than" not in raw:
        raise ValueError("policy must be an object with an only_newer_than key")
    cutoff = raw["only_newer_than"]
    if cutoff is not None and not (isinstance(cutoff, str) and cutoff.strip()):
        raise ValueError(f"only_newer_than must be null or an ISO timestamp, not {cutoff!r}")
    return SweepPolicy(
        apply=raw.get("apply") is True,
        only_newer_than=parse_timestamp(cutoff) if cutoff is not None else None,
    )


def load_policy(path: Path | None = None) -> SweepPolicy:
    """Read the sweep policy file. Absent or unreadable means report-only.

    A malformed file must never stop the hourly sync, and must never delete anything: the
    safe reading of a policy nobody can parse is the policy that does nothing.
    policy_problem() says why, so the health row can warn about it.
    """
    path = SWEEP_POLICY_FILE if path is None else Path(path)
    try:
        return _read_policy(path)
    except (OSError, ValueError, AttributeError, TypeError):
        return SweepPolicy()


def policy_problem(path: Path | None = None) -> str | None:
    """Why the policy file exists but cannot be used, or None when it is absent or valid."""
    path = SWEEP_POLICY_FILE if path is None else Path(path)
    if not path.exists():
        return None
    try:
        _read_policy(path)
    except (OSError, ValueError, AttributeError, TypeError) as e:
        return f"{path} is unreadable, so the sweep is report-only: {e}"
    return None


def _is_image(mime: str | None, filename: str) -> bool:
    return (mime or "").lower().startswith("image/") or Path(filename).suffix.lower() in (
        IMAGE_SUFFIXES
    )


def _registered(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[tuple[int, ...], str]]:
    """(directory name, filename) -> (attachment ids, the state their rows allow).

    When several rows share a directory and name, the file is deletable only if every one of
    them allows it: the least-finished row decides.
    """
    rows = conn.execute(
        f"""
        SELECT a.id, a.file_path, a.mime_type, a.filename,
               ac.id IS NOT NULL,
               COALESCE({NOT_FULLY_READ_SQL}, 0),
               COALESCE({FULLY_HELD_SQL}, 0),
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
    rank = {DELETABLE: 0, PENDING_IMAGE: 1, NOT_HELD: 2, UNREAD: 3, PENDING_TEXT: 4}
    known: dict[tuple[str, str], tuple[tuple[int, ...], str]] = {}
    for row in rows:
        att_id, file_path, mime, filename, stored, unread, held = row[:7]
        seen, described, by_design, tries = row[7:]
        if not stored:
            state = PENDING_TEXT
        elif unread:
            state = UNREAD
        elif not held:
            state = NOT_HELD
        elif _is_image(mime, filename or file_path) and not (
            seen and (described or by_design or tries >= VISION_ATTEMPTS_LIMIT)
        ):
            state = PENDING_IMAGE
        else:
            state = DELETABLE
        p = Path(file_path)
        key = (p.parent.name, p.name)
        if key in known:
            ids, prev = known[key]
            state = max(state, prev, key=rank.__getitem__)
            known[key] = (ids + (att_id,), state)
        else:
            known[key] = ((att_id,), state)
    return known


def classify_files(conn: sqlite3.Connection, root: Path) -> list[FileState]:
    """Every file under root/<directory>/, with the state that decides its fate.

    A directory reached through a symlink is not walked: it can point outside the root.
    A file that disappears between the listing and its stat is skipped.
    """
    known = _registered(conn)
    found: list[FileState] = []
    try:
        dirs = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return found
    for d in dirs:
        try:
            entries = list(os.scandir(d.path))
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            ids, state = known.get((d.name, e.name), ((), UNREGISTERED))
            found.append(FileState(Path(e.path), state, st.st_size, st.st_mtime, ids))
    return found


def classify_sharepoint_files(conn: sqlite3.Connection, root: Path) -> list[FileState]:
    """Files earlier fetches left under data/sharepoint, by whether their content is stored.

    No row names these files (fetches were saved flat by name, and a later file could replace
    an earlier one), so they are matched by content. A file may go when a row with its bytes
    holds their content, or when its SharePoint document records what they hold: each is a
    copy of a document SharePoint still serves and the link can fetch again, which is not true
    of a mail attachment. Most are the script shells of intranet pages (1,112 of them, 907 MB,
    on the producer on 2026-10-07), whose text is read through `sharepoint-cli page` instead.
    """
    from src.store.file_hashes import sha256_of_file

    stored = {
        sha
        for (sha,) in conn.execute(
            "SELECT a.sha256 FROM attachments a JOIN attachment_content ac"
            " ON ac.attachment_id = a.id"
            f" WHERE a.sha256 IS NOT NULL AND (COALESCE({FULLY_HELD_SQL}, 0)"
            "   OR (a.file_path LIKE 'text:sharepoint:%'"
            f"       AND NOT COALESCE({NOT_FULLY_READ_SQL}, 0)))"
        )
    }
    found: list[FileState] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for e in entries:
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
            sha = sha256_of_file(Path(e.path))
        except FileNotFoundError:
            continue
        state = DELETABLE if sha in stored else PENDING_TEXT
        found.append(FileState(Path(e.path), state, st.st_size, st.st_mtime, ()))
    return found


def sweep_files(
    conn: sqlite3.Connection,
    root: Path,
    policy: SweepPolicy,
    now: datetime | None = None,
    sharepoint_root: Path | None = None,
) -> dict:
    """Delete the files whose content is stored, as far as the policy allows.

    Report-only unless policy.apply. With a cutoff, only files modified at or after it are
    deleted: that is how stage 4 turns deletion on for new downloads while the backlog waits
    for its own confirmation. A directory is removed only when this pass emptied it. A file
    that cannot be deleted is counted in "errors" and the pass carries on; the files it did
    delete are stamped either way. With `sharepoint_root`, the files earlier SharePoint fetches
    left there are swept too (classify_sharepoint_files); that directory itself stays.
    """
    now = now or datetime.now(UTC)
    stats: dict = dict.fromkeys(STATES, 0)
    stats.update(
        before_cutoff=0,
        to_delete=0,
        deleted=0,
        errors=0,
        bytes_freed=0,
        dirs_removed=0,
        applied=policy.apply,
    )
    cutoff = policy.only_newer_than.timestamp() if policy.only_newer_than else None
    removed: list[int] = []
    emptied: set[Path] = set()
    files = classify_files(conn, Path(root))
    if sharepoint_root is not None:
        files += classify_sharepoint_files(conn, Path(sharepoint_root))
    try:
        for f in files:
            stats[f.state] += 1
            if f.state != DELETABLE:
                continue
            if cutoff is not None and f.mtime < cutoff:
                stats["before_cutoff"] += 1
                continue
            stats["to_delete"] += 1
            if not policy.apply:
                stats["bytes_freed"] += f.size
                continue
            try:
                f.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                stats["errors"] += 1
                continue
            stats["deleted"] += 1
            stats["bytes_freed"] += f.size
            removed.extend(f.attachment_ids)
            if sharepoint_root is None or f.path.parent != Path(sharepoint_root):
                emptied.add(f.path.parent)
    finally:
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
