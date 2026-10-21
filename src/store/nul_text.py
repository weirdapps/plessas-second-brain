"""`brain maint strip-nul`: take the NUL characters out of the attachment texts stored with them.

SQLite's length() ends a text at its first NUL and Python's len() does not, and the chunk pass
measured a text both ways (src/store/chunks.py). The audit of 2026-10-20 found 54 stored texts
with NULs: 28 UTF-16 exports read byte by byte (read again by `reextract --decoding`), 23 PDFs
whose fonts map glyphs to U+0000, and 3 archives holding Mac resource forks. Three documents
were cut into chunks again on every embedding pass, their chunk vectors dropped and paid for
each time, and eleven long ones were never cut, length() calling them short. Extraction stores
no NUL since (attachment_extractors.without_nul); this repairs what was stored before.

Each text found is written again without them, in place: the update triggers keep
attachment_content_fts in step and the sweep index's length() follows. The document's chunk
rows are deleted, since their offsets were measured in the old text, so the next embedding pass
cuts it again and drops its old chunk vectors. Its summary stays: a NUL carries no meaning.
Finding them reads every stored text once (about 3 GB on the producer, minutes). A dry run, the
default, counts; --apply writes, one row per commit. A second run finds nothing. Refused on a
replica (src/cli.py).
"""

import argparse
import sqlite3

# A stored text holds a NUL exactly when its UTF-8 bytes hold a zero byte.
_WITH_NUL = (
    "SELECT ac.id FROM attachment_content ac"
    " WHERE instr(CAST(ac.extracted_text AS BLOB), x'00') > 0 ORDER BY ac.id"
)


def strip_nul(conn: sqlite3.Connection, *, apply: bool) -> dict[str, int]:
    """Count the stored texts holding a NUL and, with `apply`, write each again without them.

    Counts: texts (holding a NUL), nuls (the characters), chunked (of those, documents with
    chunk rows, deleted when applied), stripped (rows written)."""
    stats = dict.fromkeys(("texts", "nuls", "chunked", "stripped"), 0)
    for (content_id,) in conn.execute(_WITH_NUL).fetchall():
        text, attachment_id = conn.execute(
            "SELECT extracted_text, attachment_id FROM attachment_content WHERE id = ?",
            (content_id,),
        ).fetchone()
        stats["texts"] += 1
        stats["nuls"] += text.count("\x00")
        try:
            chunked = conn.execute(
                "SELECT 1 FROM attachment_chunks WHERE attachment_id = ? LIMIT 1",
                (attachment_id,),
            ).fetchone()
        except sqlite3.OperationalError as e:  # a store before v39 cuts no chunks
            if "no such table" not in str(e):
                raise
            chunked = None
        stats["chunked"] += chunked is not None
        if not apply:
            continue
        conn.execute(
            "UPDATE attachment_content SET extracted_text = ? WHERE id = ?",
            (text.replace("\x00", ""), content_id),
        )
        if chunked is not None:
            conn.execute("DELETE FROM attachment_chunks WHERE attachment_id = ?", (attachment_id,))
        conn.commit()
        stats["stripped"] += 1
    return stats


def cmd_strip_nul(args) -> int:
    from src.store.schema import get_connection, run_migrations

    conn = get_connection(str(args.db))
    try:
        run_migrations(conn)
        stats = strip_nul(conn, apply=args.apply)
    finally:
        conn.close()
    print(
        f"{stats['texts']:,} stored text(s) hold {stats['nuls']:,} NUL character(s);"
        f" {stats['chunked']:,} of them cut into chunks"
    )
    if args.apply:
        print(
            f"stripped {stats['stripped']:,}; the next embedding pass cuts the documents again"
            " and drops their old chunk vectors"
        )
    else:
        print("dry run: nothing written; --apply strips them")
    return 0


def add_maint_parser(maint_subparsers) -> None:
    """`brain maint strip-nul`."""
    parser = maint_subparsers.add_parser(
        "strip-nul",
        help="Take the NUL characters out of the attachment texts stored with them",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Count only (default)")
    mode.add_argument("--apply", action="store_true", help="Write the texts again without them")
    parser.set_defaults(func=cmd_strip_nul)
