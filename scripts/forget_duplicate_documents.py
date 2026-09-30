#!/usr/bin/env python3
"""Forget the synced documents that are byte-identical to a mail attachment.

Document text comes from mail only from 2026-09-30. A synced document whose exact bytes
also arrived as a mail attachment is stored twice, and search returns both. This forgets the
document copy and keeps the attachment, which carries the email it came with.

Needs attachments.sha256 filled first (`python -m src.cli hash-attachments`). Dry run by
default; --apply forgets. Refuses a replica.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DEFAULT_DB, DOCUMENT_TREES, is_replica, replica_refusal  # noqa: E402
from src.store.forget import forget_documents  # noqa: E402
from src.store.schema import get_connection  # noqa: E402


def _hash_to_message_id(sha256: str) -> int:
    """ingest_document's id for these bytes (src/extract/attachment_pipeline.py)."""
    return -abs(int(sha256[:15], 16))


def _mail_twins(conn, with_text: bool) -> set[int]:
    """ingest_document ids of the mail attachments' bytes; with_text: only those whose text
    and summary are stored, so forgetting the document loses nothing."""
    stored = (
        " AND ac.extraction_status = 'extracted' AND ac.llm_status = 'extracted'"
        if with_text
        else ""
    )
    return {
        _hash_to_message_id(sha)
        for (sha,) in conn.execute(
            "SELECT a.sha256 FROM attachments a JOIN emails e ON e.id = a.email_id"
            " LEFT JOIN attachment_content ac ON ac.attachment_id = a.id"
            " WHERE a.sha256 IS NOT NULL AND e.mailbox_name <> 'External'" + stored
        )
    }


def _synced_documents(conn) -> list[tuple[int, str]]:
    patterns = [f"[Document] {tree}%" for tree in DOCUMENT_TREES]
    where = " OR ".join("subject LIKE ?" for _ in patterns)
    return conn.execute(
        f"SELECT message_id, subject FROM emails WHERE mailbox_name = 'External' AND ({where})",
        patterns,
    ).fetchall()


def find_duplicate_documents(conn) -> list[tuple[int, str]]:
    """(message_id, subject) of synced documents identical to a mail attachment with text.

    The same bytes can extract differently: an extensionless mail part recorded as
    octet-stream is skipped while the same file as a .pdf document is extracted. So the
    mail twin must hold extracted text and a summary before its document copy may go.
    """
    with_text = _mail_twins(conn, with_text=True)
    return [(mid, subject) for mid, subject in _synced_documents(conn) if mid in with_text]


def count_kept_twins(conn) -> int:
    """Synced documents byte-identical to a mail attachment that has no stored text."""
    any_twin = _mail_twins(conn, with_text=False)
    with_text = _mail_twins(conn, with_text=True)
    return sum(1 for mid, _ in _synced_documents(conn) if mid in any_twin - with_text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="forget them")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()
    if args.apply and is_replica():
        print(replica_refusal("forgetting duplicate documents"), file=sys.stderr)
        return 2

    conn = get_connection(args.db)
    try:
        unhashed = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE sha256 IS NULL AND file_removed_at IS NULL"
        ).fetchone()[0]
        dupes = find_duplicate_documents(conn)
        print(f"Synced documents identical to a mail attachment: {len(dupes):,}")
        kept = count_kept_twins(conn)
        if kept:
            print(f"  kept: {kept:,} whose mail twin has no extracted text and summary")
        if unhashed:
            print(
                f"  note: {unhashed:,} attachments have no hash yet; run"
                " `python -m src.cli hash-attachments` first for a complete list"
            )
        for mid, subject in dupes[:20]:
            print(f"  {mid}  {subject}")
        if not args.apply:
            print("DRY RUN (pass --apply to forget them)")
            return 0
        stats = forget_documents(conn, [mid for mid, _ in dupes])
    finally:
        conn.close()
    print(
        f"Forgot {stats['emails']:,} documents, {stats['attachments']:,} attachment rows,"
        f" {stats['vectors']:,} vectors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
