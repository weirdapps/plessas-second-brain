#!/usr/bin/env python3
"""Resolve attachment directories the registrar can never claim.

`outlook-cli` writes binaries to data/attachments/<message_id>/ and a later pass
registers them against `emails`. A Graph message id encodes the folder holding
the message, so triaging one into Archive-<year> mints a NEW id and retires the
old; deleting the message retires the id outright. The directory already written
under the old id is then unmatchable for ever, and the registrar deferred it on
every run with nothing to bound the wait.

By 2026-08-31 that was 1,955 directories and 3.88 GiB, accruing at roughly 400 a
month, and, because the health check counted every one of them, a WARN that no
amount of fixing could clear.

The files are not uniform, and the difference decides what may be done to them:

  * 4,678 of 5,989 were byte-identical to attachments already registered. The
    sync re-exported the moved message under its new id and re-downloaded the
    same files, so these are pure duplication and safe to delete.
  * 954 existed nowhere else. Their message was deleted from the mailbox, so
    nothing re-downloaded them and no row references them. This directory holds
    the only copy, and reaping it blind would have destroyed them. They are
    adopted instead, through the same reverse-ingest path standalone documents
    use, which gives each a synthetic email anchor and makes it searchable.

Defaults to a dry run. Pass --apply to actually touch the disk.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import ATTACHMENTS_DIR, DEFAULT_DB  # noqa: E402
from src.export.outlook_attachments import ORPHAN_GRACE_DAYS, is_abandoned_orphan  # noqa: E402
from src.extract.attachment_pipeline import ingest_document  # noqa: E402
from src.store.schema import get_connection  # noqa: E402

# outlook_attachments skips it on the way in and sharepoint_links tracks it with
# its own fetch state and its own given-up rule. Not this script's to delete.
SHAREPOINT_SUBDIR = "sharepoint"


def _registered_elsewhere(conn) -> dict[tuple[str, int], list[str]]:
    """Map (filename, size) to the paths of attachments already in the table."""
    index: dict[tuple[str, int], list[str]] = {}
    for name, size, path in conn.execute(
        "SELECT filename, file_size, file_path FROM attachments WHERE file_path IS NOT NULL"
    ):
        if size is None:
            continue
        index.setdefault((name, size), []).append(path)
    return index


def _survey(db_path: str) -> tuple[set[str], dict[tuple[str, int], list[str]]]:
    """Read the table once: which directories are claimed, and what is held where.

    Closes before anything writes. Adoption goes through `ingest_document`, which
    opens its own connection, and holding a second one across those writes is how
    six concurrent sb-* units produced "database is locked" on 2026-08-24.
    """
    conn = get_connection(db_path)
    try:
        referenced = {
            Path(fp).parent.name
            for (fp,) in conn.execute(
                "SELECT file_path FROM attachments WHERE file_path IS NOT NULL"
            )
        }
        return referenced, _registered_elsewhere(conn)
    finally:
        conn.close()


def _is_duplicate(f: Path, size: int, known: dict[tuple[str, int], list[str]]) -> bool:
    """Whether these bytes are already registered somewhere that still exists.

    A row is not bytes. If every registered copy has vanished from disk, this
    orphan is the last one and the row is no licence to delete it.
    """
    return any(Path(p).exists() for p in known.get((f.name, size), []))


def _reap_one_dir(
    msg_dir: Path,
    known: dict[tuple[str, int], list[str]],
    apply: bool,
    db_path: str,
    stats: dict,
) -> None:
    for f in sorted(msg_dir.iterdir()):
        if f.is_dir():
            continue
        size = f.stat().st_size
        if _is_duplicate(f, size, known):
            stats["deleted"] += 1
            stats["bytes_freed"] += size
            if apply:
                f.unlink()
            continue
        if not apply:
            stats["adopted"] += 1
            continue
        # Copies the file under its own synthetic id, so the original is ours to
        # remove afterwards. The id is a hash of the CONTENT, so identical bytes
        # seen twice come back skipped: recognised, already searchable, and not
        # a rescue worth claiming in the count.
        outcome = ingest_document(
            str(f),
            db_path=db_path,
            sender_name="Recovered Attachment",
            source=f.stem.replace("_", " ").replace("-", " ").strip() or f.name,
        )
        stats["already_ingested" if outcome.get("skipped") else "adopted"] += 1
        # Adoption files the document under a directory named for the hash of
        # its own content, inside this same tree, so a file an earlier
        # reverse-ingest already settled there resolves to where it lies. It was
        # not consumed, it was registered, and unlinking it now would delete the
        # copy the row points at.
        settled = outcome.get("file_path")
        if settled and Path(settled).exists() and os.path.samefile(settled, f):
            continue
        f.unlink()


def _husk_is_empty(msg_dir: Path) -> bool:
    """Nothing left but, at most, someone else's subtree."""
    return not any(p.name != SHAREPOINT_SUBDIR for p in msg_dir.iterdir())


def reap_orphan_attachments(
    db_path: str | Path,
    base_dir: Path | str,
    apply: bool = False,
    limit: int | None = None,
    grace_days: float = ORPHAN_GRACE_DAYS,
) -> dict:
    """Delete duplicated orphan files, adopt unique ones, remove the husk.

    Only directories past `grace_days` are eligible. A fresh deferral is a live
    race with the registrar rather than garbage, and reaping one would destroy an
    attachment whose email simply has not committed yet.

    Takes a path and not a live connection; see `_survey`.
    """
    db_path = str(db_path)
    base_dir = Path(base_dir)
    stats = {
        "scanned": 0,
        "deleted": 0,
        "adopted": 0,
        "already_ingested": 0,
        "dirs_removed": 0,
        "bytes_freed": 0,
    }
    if not base_dir.is_dir():
        return stats

    referenced, known = _survey(db_path)

    for msg_dir in sorted(base_dir.iterdir()):
        if not msg_dir.is_dir() or msg_dir.name in referenced:
            continue
        if not is_abandoned_orphan(msg_dir, grace_days):
            continue
        if limit is not None and stats["dirs_removed"] >= limit:
            break

        stats["scanned"] += 1
        _reap_one_dir(msg_dir, known, apply, db_path, stats)

        if not _husk_is_empty(msg_dir):
            continue
        if not apply:
            stats["dirs_removed"] += 1
        elif not (msg_dir / SHAREPOINT_SUBDIR).is_dir():
            msg_dir.rmdir()
            stats["dirs_removed"] += 1

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually delete and adopt")
    ap.add_argument("--limit", type=int, default=None, help="max directories per run")
    ap.add_argument("--grace-days", type=float, default=ORPHAN_GRACE_DAYS)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--root", default=str(ATTACHMENTS_DIR))
    args = ap.parse_args()

    stats = reap_orphan_attachments(
        args.db,
        args.root,
        apply=args.apply,
        limit=args.limit,
        grace_days=args.grace_days,
    )

    mode = "APPLIED" if args.apply else "DRY RUN (pass --apply to act)"
    print(f"orphan attachment reap: {mode}")
    print(f"  directories scanned : {stats['scanned']:,}")
    print(f"  duplicates deleted  : {stats['deleted']:,}  ({stats['bytes_freed'] / 2**20:.0f} MiB)")
    print(f"  unique files adopted: {stats['adopted']:,}")
    print(f"  content already held : {stats['already_ingested']:,}")
    print(f"  directories removed : {stats['dirs_removed']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
