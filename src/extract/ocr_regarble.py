"""Find the stored OCR texts that are Greek read as Latin look-alike letters, and mark them.

    python -m src.cli ocr-regarble            # count them by extraction method; write nothing
    python -m src.cli ocr-regarble --apply    # mark them, and clear what was summarised from them

Phase 1 marks such a text as it reads it (src/extract/attachment_extractors.py). The audit found
804 read before it did, nearly all OCR'd in one April batch, and 785 of their files already swept
(data-quality-03). This reads every stored text that holds an OCR reading (OCR_TEXT_METHODS) and
judges it with looks_like_garbled_greek; a row already marked counts too.

--apply, for each row found:

    mark     the ocr_garbled note in extraction_error, after what the error already said
    summary  cleared with its language, and llm_status 'skipped': Phase 2 does not summarise it
    rows     the decisions, action items and key facts its summary wrote, found by attachment_id,
             deleted, and its vector dropped from the embeddings index. A summary written before
             schema v28 tagged nothing with the attachment id, so its rows cannot be told from
             the email's own and stay; such summaries are counted
    text     kept, and searchable, until a better reading replaces it

Then the files, by attachment id, counts and ids only. On disk: an image is transcribed by the
nightly image pass (or `transcribe-images`), a scan is read again Greek first by
`reextract --garbled`. Swept: `refetch-garbled` fetches the original back from the mailbox, to
the path its row records (src/export/refetch_garbled.py). A text-only document kept no file.

Producer only: the CLI refuses it on a replica, as it does every command that writes.
"""

import sqlite3
import textwrap
from pathlib import Path

from src.config import ATTACHMENTS_DIR, DEFAULT_DB
from src.extract.attachment_extractors import OCR_TEXT_METHODS
from src.extract.ocr_garble import is_garbled, looks_like_garbled_greek, mark_garbled
from src.store.file_hashes import locate_file
from src.store.schema import get_connection

# The rows a summary writes onto its email, and how the report names them.
DERIVED = (("decisions", "decisions"), ("action_items", "action items"), ("key_facts", "key facts"))
FILE_STATES = ("on disk", "swept", "missing", "text only")

NEXT_STEPS = """\
Next: `refetch-garbled` fetches the swept originals back from the mailbox. Then a scan on disk is
read again with `reextract --garbled`, and an image on disk is transcribed by the nightly image
pass or `transcribe-images`."""


def _where_the_file_is(file_path: str, removed_at: str | None, root: Path) -> str:
    if file_path.startswith("text:"):
        return "text only"
    if locate_file(file_path, root) is not None:
        return "on disk"
    return "swept" if removed_at else "missing"


def find_garbled(conn: sqlite3.Connection, root: Path) -> tuple[dict, list[tuple]]:
    """Every stored OCR text judged. Returns the counts by extraction method, and each row found
    as (content id, attachment id, error, summary, llm_status, where its file is)."""
    methods = sorted(OCR_TEXT_METHODS)
    marks = ",".join("?" * len(methods))
    by_method: dict[str, dict[str, int]] = {}
    found = []
    for ac_id, att_id, method, error, text, summary, llm, file_path, removed_at in conn.execute(
        "SELECT ac.id, ac.attachment_id, ac.extraction_method, ac.extraction_error,"
        " ac.extracted_text, ac.summary, ac.llm_status, a.file_path, a.file_removed_at"
        " FROM attachment_content ac JOIN attachments a ON a.id = ac.attachment_id"
        f" WHERE ac.extraction_status = 'extracted' AND ac.extraction_method IN ({marks})"
        " ORDER BY ac.id",
        methods,
    ):
        counts = by_method.setdefault(method, {"scanned": 0, "garbled": 0, "marked before": 0})
        counts["scanned"] += 1
        marked = is_garbled(error)
        if not (marked or looks_like_garbled_greek(text)):
            continue
        counts["garbled"] += 1
        counts["marked before"] += marked
        where = _where_the_file_is(file_path or "", removed_at, root)
        found.append((ac_id, att_id, error, summary, llm, where))
    return by_method, found


def _apply(conn: sqlite3.Connection, found: list[tuple]) -> dict:
    """Mark each row found and clear what its summary wrote. Committed per row: the producer's
    other writers must not wait behind one long transaction."""
    stats = dict.fromkeys(("marked", "summaries cleared", "untagged", "vectors removed"), 0)
    stats.update(dict.fromkeys((label for _, label in DERIVED), 0))
    cleared: list[int] = []
    for ac_id, att_id, error, summary, llm, _where in found:
        tagged = 0
        for table, label in DERIVED:
            n = conn.execute(f"DELETE FROM {table} WHERE attachment_id = ?", (att_id,)).rowcount
            stats[label] += n
            tagged += n
        if not is_garbled(error) or summary or llm != "skipped":
            conn.execute(
                "UPDATE attachment_content SET extraction_error = ?, summary = NULL,"
                " language = NULL, llm_status = 'skipped', llm_error = NULL WHERE id = ?",
                (mark_garbled(error), ac_id),
            )
        conn.commit()
        stats["marked"] += not is_garbled(error)
        if summary:
            stats["summaries cleared"] += 1
            stats["untagged"] += not tagged
            cleared.append(ac_id)
    if cleared:
        from src.store.embeddings import remove_vectors

        # An attachment's vector is keyed on its negated content id (embeddings.build_index).
        stats["vectors removed"] = remove_vectors([-ac_id for ac_id in cleared])
    return stats


def regarble(db_path=None, apply: bool = False, root=None) -> dict:
    """Judge every stored OCR text, and with `apply` mark what is found. See the module docstring.

    Returns by_method (scanned, garbled and marked before, per extraction method), found, files
    (attachment ids by where their file is) and applied; with `apply`, the counts of what changed.
    """
    root = Path(root) if root else ATTACHMENTS_DIR
    conn = get_connection(str(db_path or DEFAULT_DB))
    try:
        by_method, found = find_garbled(conn, root)
        files = {state: [row[1] for row in found if row[5] == state] for state in FILE_STATES}
        stats: dict = {
            "by_method": by_method,
            "found": len(found),
            "files": files,
            "applied": apply,
        }
        if apply:
            stats.update(_apply(conn, found))
    finally:
        conn.close()
    return stats


def cmd(args) -> int:
    """`python -m src.cli ocr-regarble [--dry-run | --apply] [--root PATH]`."""
    stats = regarble(args.db, apply=args.apply, root=args.root)
    print(f"ocr-regarble {'APPLIED' if stats['applied'] else 'DRY RUN (nothing written)'}:")
    print(f"  {'method':<20}{'scanned':>10}{'garbled':>10}{'marked before':>15}")
    for method, counts in sorted(stats["by_method"].items()):
        print(
            f"  {method:<20}{counts['scanned']:>10,}{counts['garbled']:>10,}"
            f"{counts['marked before']:>15,}"
        )
    print(f"  {'found':<34}: {stats['found']:,}")
    for state, ids in stats["files"].items():
        print(f"  {'files ' + state:<34}: {len(ids):,}")
        if ids:
            print(
                textwrap.fill(
                    ", ".join(map(str, ids)), 96, initial_indent=" " * 4, subsequent_indent=" " * 4
                )
            )
    if stats["applied"]:
        for key, label in (
            ("marked", "marked"),
            ("summaries cleared", "summaries cleared"),
            ("decisions", "decisions removed"),
            ("action items", "action items removed"),
            ("key facts", "key facts removed"),
            ("vectors removed", "vectors removed"),
            ("untagged", "summaries with no tagged rows"),
        ):
            print(f"  {label:<34}: {stats[key]:,}")
        if stats["untagged"]:
            print(
                "  (written before schema v28: those decisions, actions and facts stay on the email)"
            )
    else:
        print("Run it again with --apply to mark them and clear their summaries.")
    print(NEXT_STEPS)
    return 0
