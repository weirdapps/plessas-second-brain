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
import filecmp
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import ATTACHMENTS_DIR, DEFAULT_DB, is_replica, replica_refusal  # noqa: E402
from src.export.outlook_attachments import ORPHAN_GRACE_DAYS, is_abandoned_orphan  # noqa: E402
from src.extract.attachment_pipeline import ingest_document  # noqa: E402
from src.store.file_hashes import sha256_of_file  # noqa: E402
from src.store.file_sweep import UNREAD_SQL, load_policy  # noqa: E402
from src.store.schema import get_connection  # noqa: E402

# outlook_attachments skips it on the way in and sharepoint_links tracks it with
# its own fetch state and its own given-up rule. Not this script's to delete.
SHAREPOINT_SUBDIR = "sharepoint"

# Attachments download right after their message is staged, and its email row lands at the
# next load, normally within the hour. So a directory still unregistered after a day, while
# mail keeps loading, belongs to a message that will never load (a message moved to Archive
# gets a new id, and the loader drops its email as a duplicate). A file in it whose content is
# already stored can go then. While loading is stalled (no email received in the last day),
# duplicates wait the full ORPHAN_GRACE_DAYS like everything else. A unique file always does.
DUPLICATE_GRACE_DAYS = 1.0


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


def _survey(
    db_path: str,
) -> tuple[set[str], dict[tuple[str, int], list[str]], set[str], str | None]:
    """Read the table once: claimed directories, what is held where, stored hashes, newest mail.

    A hash counts only when its row's content was read by Phase 1 (src/store/file_sweep.py's
    UNREAD_SQL): a row whose file vanished before Phase 1, or that this host could not read,
    proves nothing about the bytes, and the orphan may be the last copy.

    Closes before anything writes. Adoption goes through `ingest_document`, which opens its
    own connection, and holding a second one across those writes is how six concurrent sb-*
    units produced "database is locked" on 2026-08-24.
    """
    conn = get_connection(db_path)
    try:
        referenced = {
            Path(fp).parent.name
            for (fp,) in conn.execute(
                "SELECT file_path FROM attachments WHERE file_path IS NOT NULL"
            )
        }
        hashes = {
            sha
            for (sha,) in conn.execute(
                "SELECT a.sha256 FROM attachments a"
                " JOIN attachment_content ac ON ac.attachment_id = a.id"
                f" WHERE a.sha256 IS NOT NULL AND NOT COALESCE({UNREAD_SQL}, 0)"
            )
        }
        newest = conn.execute(
            "SELECT MAX(date_received) FROM emails"
            " WHERE mailbox_name IS NULL OR mailbox_name <> 'External'"
        ).fetchone()[0]
        return referenced, _registered_elsewhere(conn), hashes, newest
    finally:
        conn.close()


def _is_duplicate(
    f: Path, size: int, known: dict[tuple[str, int], list[str]], hashes: set[str]
) -> bool:
    """Whether these bytes are already stored.

    First by the stored hash, which survives the sweep deleting the registered copy. Then,
    for rows that have no hash yet, by the byte comparison this used to rely on alone: a
    matching name and size is not enough (402 registered (filename, size) groups on the VPS
    hold more than one distinct content), and a row whose copy has vanished proves nothing.
    """
    if hashes and sha256_of_file(f) in hashes:
        return True
    return any(
        Path(p).exists() and filecmp.cmp(f, p, shallow=False) for p in known.get((f.name, size), [])
    )


def _reap_one_dir(
    msg_dir: Path,
    known: dict[tuple[str, int], list[str]],
    hashes: set[str],
    apply: bool,
    db_path: str,
    stats: dict,
    adopt: bool,
    adopt_only: bool,
) -> None:
    for f in sorted(msg_dir.iterdir()):
        if f.is_dir():
            continue
        size = f.stat().st_size
        if _is_duplicate(f, size, known, hashes):
            if adopt_only:
                continue
            stats["deleted"] += 1
            stats["bytes_freed"] += size
            if apply:
                f.unlink()
            continue
        if not adopt:
            stats["waiting"] += 1
            continue
        if not apply:
            stats["adopted"] += 1
            continue
        # Copies the file under its own synthetic id, so the original is ours to
        # remove afterwards. The id is a hash of the CONTENT, so identical bytes
        # seen twice come back skipped: recognised, already searchable, and not
        # a rescue worth claiming in the count. The copy is registered, so the
        # sweep deletes it once Phase 1 has read it.
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


def _mail_is_flowing(newest: str | None, days: float) -> bool:
    """Whether an email was received within `days`. Unknown or unparseable reads as stalled."""
    if not newest:
        return False
    try:
        ts = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return datetime.now(UTC) - ts <= timedelta(days=days)


def _husk_is_empty(msg_dir: Path) -> bool:
    """Nothing left but, at most, someone else's subtree."""
    return not any(p.name != SHAREPOINT_SUBDIR for p in msg_dir.iterdir())


def reap_orphan_attachments(
    db_path: str | Path,
    base_dir: Path | str,
    apply: bool = False,
    limit: int | None = None,
    grace_days: float = ORPHAN_GRACE_DAYS,
    duplicate_grace_days: float = DUPLICATE_GRACE_DAYS,
    adopt_only: bool = False,
    only_newer_than: float | None = None,
) -> dict:
    """Delete stored duplicates, adopt unique files, remove the husk.

    A directory becomes eligible once it is `duplicate_grace_days` old: its stored
    duplicates go. Its unique files are adopted only once it is `grace_days` old, because a
    fresher deferral may still be a live race with the registrar. `adopt_only` adopts and
    deletes nothing else (stage 2, before the backlog deletion is confirmed).
    `only_newer_than` (a POSIX time) leaves older directories alone (stage 4).

    Deleting a file changes its directory's mtime, which is the grace clock; it is put back,
    or a duplicate deleted today would restart the wait for the unique file beside it.

    Takes a path and not a live connection; see `_survey`.
    """
    db_path = str(db_path)
    base_dir = Path(base_dir)
    stats: dict[str, float] = {
        "scanned": 0,
        "deleted": 0,
        "adopted": 0,
        "already_ingested": 0,
        "waiting": 0,
        "dirs_removed": 0,
        "bytes_freed": 0,
    }
    if not base_dir.is_dir():
        return stats

    referenced, known, hashes, newest = _survey(db_path)
    if not _mail_is_flowing(newest, duplicate_grace_days):
        duplicate_grace_days = grace_days
    stats["duplicate_grace_days"] = min(duplicate_grace_days, grace_days)

    for msg_dir in sorted(base_dir.iterdir()):
        if not msg_dir.is_dir() or msg_dir.name in referenced:
            continue
        if not is_abandoned_orphan(msg_dir, min(duplicate_grace_days, grace_days)):
            continue
        st = msg_dir.stat()
        if only_newer_than is not None and st.st_mtime < only_newer_than:
            continue
        if limit is not None and stats["dirs_removed"] >= limit:
            break

        stats["scanned"] += 1
        adopt = is_abandoned_orphan(msg_dir, grace_days)
        _reap_one_dir(msg_dir, known, hashes, apply, db_path, stats, adopt, adopt_only)

        if not _husk_is_empty(msg_dir):
            if apply:
                os.utime(msg_dir, (st.st_atime, st.st_mtime))
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
    ap.add_argument(
        "--duplicate-grace-days",
        type=float,
        default=DUPLICATE_GRACE_DAYS,
        help="age at which stored duplicates in an orphan directory are deleted",
    )
    ap.add_argument(
        "--adopt-only", action="store_true", help="adopt unique files, delete nothing else"
    )
    ap.add_argument(
        "--policy",
        action="store_true",
        help="take apply and the cutoff from the sweep policy file (src/store/file_sweep.py)",
    )
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--root", default=str(ATTACHMENTS_DIR))
    args = ap.parse_args()
    # Read before the replica check below, so a policy that says apply meets it too.
    only_newer_than = None
    if args.policy:
        policy = load_policy()
        args.apply = args.apply or policy.apply
        if policy.only_newer_than is not None:
            only_newer_than = policy.only_newer_than.timestamp()
    if args.apply and is_replica():
        # The dry run reads only; applying adopts files into the database, and a
        # replica's copy is replaced by the next pull (see src/config.py).
        print(replica_refusal("the orphan attachment reap"), file=sys.stderr)
        return 2

    stats = reap_orphan_attachments(
        args.db,
        args.root,
        apply=args.apply,
        limit=args.limit,
        grace_days=args.grace_days,
        duplicate_grace_days=args.duplicate_grace_days,
        adopt_only=args.adopt_only,
        only_newer_than=only_newer_than,
    )

    mode = "APPLIED" if args.apply else "DRY RUN (pass --apply to act)"
    print(f"orphan attachment reap: {mode}")
    print(f"  directories scanned : {stats['scanned']:,}")
    print(f"  duplicates deleted  : {stats['deleted']:,}  ({stats['bytes_freed'] / 2**20:.0f} MiB)")
    print(f"  unique files adopted: {stats['adopted']:,}")
    print(f"  content already held : {stats['already_ingested']:,}")
    print(f"  unique, still waiting : {stats['waiting']:,}")
    if "duplicate_grace_days" in stats:
        print(f"  duplicate grace (days): {stats['duplicate_grace_days']:g}")
    print(f"  directories removed : {stats['dirs_removed']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
