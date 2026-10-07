#!/usr/bin/env python3
"""Relabel failed and skipped attachment rows with the verdicts Phase 1 now reaches unread.

Phase 1 now records a file encrypted at rest as 'encrypted' (method rms, rms-message or
password) and gives every skip its reason. Rows written before carry the old labels: failures
(BadZipFile, "Can't find workbook", CompoundFileInvalidMagicError, "document closed or
encrypted"), skips blaming "a custom export format", skips with no reason at all, and SharePoint
page shells skipped as an unsupported type. This gives each failed or skipped row the verdict
Phase 1 reaches from the file's name, declared type and first bytes
(src/extract/attachment_extractors.verdict_without_reading), and reads no text:

  encrypted  an OLE2 container whose directory names rights management or a password, an
             MSIPC message, a PDF that needs a password, a legacy workbook xlrd found encrypted
  skipped    the reason of the skip list (audio, video, archives with no reader), an empty
             file, and a SharePoint page shell: a text-only row from a fetch that returned the
             page frame (an .aspx page, a sign-in or viewer frame), not the document

A row a reader would now read (.mso, .wmz, .emz, sniffed formats, damaged Office files) is left
to `python -m src.cli reextract --stale --formats`. A row the images pass owns
(extraction_method 'vision'), an extracted row and a row whose file is gone are never touched.
Only extraction_status, extraction_method and extraction_error change.

    python scripts/relabel_attachment_status.py            # dry run: what would change
    python scripts/relabel_attachment_status.py --apply    # write, on the producer

Exit codes: 0 done, 2 --apply refused on a replica.
"""

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import ATTACHMENTS_DIR, DEFAULT_DB, is_replica, replica_refusal  # noqa: E402
from src.extract.attachment_extractors import (  # noqa: E402
    encrypted_result,
    verdict_without_reading,
)
from src.store.file_hashes import locate_file  # noqa: E402
from src.store.schema import get_connection  # noqa: E402

SHELL_REASON = (
    "SharePoint page shell: the fetch returned the page frame (an .aspx page, or a sign-in or"
    " viewer frame), not the document; the SharePoint pass fetches it again"
)
# What xlrd says of a workbook with a FILEPASS record; the OLE walk cannot see that one.
XLS_ENCRYPTED = "xlrd open failed: Workbook is encrypted"
COMMIT_EVERY = 200

Label = tuple[str, str | None, str | None]


def _verdict(file_path: str, mime: str | None, label: Label, root: Path) -> Label | str | None:
    """The new label for one row, "missing" when its file is gone, or None to leave it."""
    _status, method, error = label
    if file_path.startswith("text:"):
        if file_path.startswith("text:sharepoint:") and (error or "").startswith(
            "Unsupported type:"
        ):
            return ("skipped", method, SHELL_REASON)
        return None
    path = locate_file(file_path, root)
    if path is None:
        return "missing"
    result: dict | None
    if error == XLS_ENCRYPTED:
        result = encrypted_result("password", path.suffix.lower())
    else:
        result = verdict_without_reading(str(path), mime or "")
    if result is None:
        return None
    return (result["status"], result["method"], result["error"])


def plan(conn: sqlite3.Connection, root: Path) -> tuple[list[tuple[int, Label, Label]], Counter]:
    """(content id, old label, new label) for every row whose label would change, and counts."""
    stats: Counter = Counter()
    changes = []
    rows = conn.execute(
        "SELECT ac.id, a.file_path, a.mime_type, ac.extraction_status, ac.extraction_method,"
        " ac.extraction_error FROM attachment_content ac"
        " JOIN attachments a ON a.id = ac.attachment_id"
        " WHERE ac.extraction_status IN ('failed', 'skipped')"
        " AND COALESCE(ac.extraction_method, '') != 'vision'"
        " ORDER BY ac.id"
    ).fetchall()
    for ac_id, file_path, mime, status, method, error in rows:
        stats["examined"] += 1
        old: Label = (status, method, error)
        new = _verdict(file_path or "", mime, old, root)
        if new == "missing":
            stats["missing"] += 1
        elif new is None or new == old:
            stats["unchanged"] += 1
        else:
            assert isinstance(new, tuple)
            changes.append((ac_id, old, new))
            kind = new[1] if new[0] == "encrypted" else (new[2] or "").split(":")[0]
            stats[f"{status} -> {new[0]}/{kind}"] += 1
    return changes, stats


def apply(conn: sqlite3.Connection, changes: list[tuple[int, Label, Label]]) -> int:
    """Write the new labels. A row that changed since the plan (reextract, or the images pass
    making it a 'vision' row) is left as it is now."""
    written = 0
    for n, (ac_id, old, new) in enumerate(changes, 1):
        written += conn.execute(
            "UPDATE attachment_content SET extraction_status = ?, extraction_method = ?,"
            " extraction_error = ? WHERE id = ? AND extraction_status = ?"
            " AND extraction_method IS ? AND extraction_error IS ?"
            " AND COALESCE(extraction_method, '') != 'vision'",
            (*new, ac_id, *old),
        ).rowcount
        if n % COMMIT_EVERY == 0:
            conn.commit()
    conn.commit()
    return written


def _transition_order(key: str) -> tuple[int, str]:
    return (0 if " -> " in key else 1, key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the labels (default: dry run)")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument(
        "--root", default=str(ATTACHMENTS_DIR), help="attachments root, for rows recorded elsewhere"
    )
    args = parser.parse_args(argv)
    if args.apply and is_replica():
        print(replica_refusal("the attachment relabel"), file=sys.stderr)
        return 2

    conn = get_connection(args.db)
    try:
        changes, stats = plan(conn, Path(args.root))
        written = apply(conn, changes) if args.apply else 0
    finally:
        conn.close()

    print(
        "relabel attachment status: "
        + ("APPLIED" if args.apply else "DRY RUN (pass --apply to write)")
    )
    print(f"  rows examined : {stats['examined']:,}")
    print(f"  files missing : {stats['missing']:,}")
    print(f"  unchanged     : {stats['unchanged']:,}")
    print(f"  to change     : {len(changes):,}")
    for key in sorted((k for k in stats if " -> " in k), key=_transition_order):
        print(f"    {key:<40}: {stats[key]:,}")
    if args.apply:
        print(f"  written       : {written:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
