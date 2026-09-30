"""Redo rows that earlier code capped, skipped or could not read, while their files exist.

    capped  the text stopped at exactly the old 100,000-character cap: read the file again in
            full, then summarise it again (in parts, as Phase 2 now does for long texts). A row
            read in full is longer than the cap, so a repeated run does not select it again
    long    a text over 50,000 characters was summarised from its first 50,000 only: summarise
            it again
    zip     a zip archive Phase 1 skipped before it could unpack archives, or one its time budget
            cut short: read it again, with no budget
    unread  Phase 1 recorded the row without reading the bytes (the file was not found, or
            this host lacked the tool): read it again
    partial the old readers read it in part (a spreadsheet to 20 sheets of 51 rows, a scan to
            30 pages, an archive with both, a text to 2,000,000 characters): read it again in
            full. The first run records when it began (sync_metadata reextract_partial_since);
            a row it has read carries a later extracted_at, so a repeated run takes only what
            is left. Until then the sweep keeps the file (src/store/file_sweep.py PARTIAL_SQL)

A row is updated in place, so its id, and the vector keyed on it, stay its own. A re-read that
comes back with no text leaves a row that already had text untouched (counted as kept): a
timeout or a parser error must not replace what was stored. A re-read that comes back with the
same text only records when it was read (counted as unchanged): nothing new to summarise. The vector is dropped once Phase 2
has run, for every row it did not fail, so the next index build embeds the new summary; a row
whose new summary failed keeps its old summary and vector. Phase 2 replaces the attachment's own
key facts, decisions and action items, and does not repeat one an older summary already put on
the email.
"""

import math
from datetime import datetime
from pathlib import Path

from src.config import ATTACHMENTS_DIR, DEFAULT_DB
from src.extract.attachment_extractors import extract_text_from_file
from src.extract.attachment_pipeline import LONG_TEXT_CHARS, _connect, run_phase2
from src.redact import redact_secrets
from src.store.file_hashes import locate_file
from src.store.file_sweep import PARTIAL_SINCE_KEY, PARTIAL_SQL, UNREAD_SQL

OLD_TEXT_CAP = 100_000

# selector -> (the rows it takes, whether Phase 1 runs again)
SELECTORS: dict[str, tuple[str, bool]] = {
    "capped": (f"length(ac.extracted_text) = {OLD_TEXT_CAP}", True),
    "long": (
        f"length(ac.extracted_text) > {LONG_TEXT_CHARS}"
        f" AND length(ac.extracted_text) < {OLD_TEXT_CAP} AND ac.llm_status = 'extracted'",
        False,
    ),
    "zip": (
        "(lower(a.filename) LIKE '%.zip'"
        " OR a.mime_type IN ('application/zip', 'application/x-zip-compressed'))"
        " AND ((ac.extraction_status = 'skipped' AND ac.extracted_text IS NULL)"
        " OR ac.extraction_error LIKE '%members left unread%')",
        True,
    ),
    "unread": (f"COALESCE({UNREAD_SQL}, 0)", True),
    # A text-only document kept no file to read again.
    "partial": (f"COALESCE({PARTIAL_SQL}, 0) AND a.file_path NOT LIKE 'text:%'", True),
}


def select_rows(conn, which: set[str], limit: int | None = None) -> list[tuple]:
    """(content id, attachment id, file path, mime type, whether Phase 1 runs, whether the row
    holds text) per chosen row."""
    rows: dict[int, tuple] = {}
    for name in sorted(which):
        where, reread = SELECTORS[name]
        for ac_id, att_id, file_path, mime, has_text in conn.execute(
            "SELECT ac.id, a.id, a.file_path, a.mime_type, ac.extracted_text IS NOT NULL"
            " FROM attachment_content ac"
            f" JOIN attachments a ON a.id = ac.attachment_id WHERE {where}"
        ):
            prior = rows.get(ac_id)
            rows[ac_id] = (
                ac_id,
                att_id,
                file_path,
                mime,
                reread or bool(prior and prior[4]),
                bool(has_text),
            )
    ordered = [rows[k] for k in sorted(rows)]
    return ordered[:limit] if limit else ordered


def reextract(
    db_path=None,
    which: set[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    workers: int = 1,
    root=None,
) -> dict:
    """Read again and summarise again the rows the selectors choose. See the module docstring."""
    db_path = str(db_path or DEFAULT_DB)
    root = Path(root) if root else ATTACHMENTS_DIR
    stats = dict.fromkeys(
        (
            "selected",
            "reread",
            "resummarise",
            "missing",
            "kept",
            "unchanged",
            "summarised",
            "failed",
        ),
        0,
    )
    touched: list[tuple[int, int]] = []
    conn = _connect(db_path)
    try:
        now = datetime.now().isoformat()
        if "partial" in (which or set()) and not dry_run:
            conn.execute(
                "INSERT OR IGNORE INTO sync_metadata (key, value) VALUES (?, ?)",
                (PARTIAL_SINCE_KEY, now),
            )
            conn.commit()
        rows = select_rows(conn, which or set(), limit)
        stats["selected"] = len(rows)
        for ac_id, att_id, file_path, mime, reread, has_text in rows:
            if not reread:
                stats["resummarise"] += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE attachment_content SET llm_status = 'pending', llm_error = NULL"
                        " WHERE id = ?",
                        (ac_id,),
                    )
                    conn.commit()
                    touched.append((ac_id, att_id))
                continue
            path = None
            if file_path and not file_path.startswith("text:"):
                path = locate_file(file_path, root)
            if path is None:
                stats["missing"] += 1
                continue
            stats["reread"] += 1
            if dry_run:
                continue
            result = extract_text_from_file(
                str(path), mime or "", zip_seconds=math.inf, ocr_seconds=math.inf
            )
            if has_text and not result["text"]:
                stats["kept"] += 1
                continue
            text = redact_secrets(result["text"]) if result["text"] else result["text"]
            (stored,) = conn.execute(
                "SELECT extracted_text FROM attachment_content WHERE id = ?", (ac_id,)
            ).fetchone()
            if has_text and text == stored:
                conn.execute(
                    "UPDATE attachment_content SET extraction_method = ?, extraction_status = ?,"
                    " extraction_error = ?, extracted_at = ? WHERE id = ?",
                    (result["method"], result["status"], result["error"], now, ac_id),
                )
                conn.commit()
                stats["unchanged"] += 1
                continue
            conn.execute(
                """UPDATE attachment_content
                   SET extracted_text = ?, extraction_method = ?, extraction_status = ?,
                       extraction_error = ?, extracted_at = ?, llm_status = 'pending',
                       llm_error = NULL
                   WHERE id = ?""",
                (
                    text,
                    result["method"],
                    result["status"],
                    result["error"],
                    now,
                    ac_id,
                ),
            )
            # Per row: the next re-read can take minutes, and an open write transaction
            # meanwhile blocks every other writer.
            conn.commit()
            touched.append((ac_id, att_id))
        conn.commit()
    finally:
        conn.close()
    if touched:
        from src.store.embeddings import remove_vectors

        p2 = run_phase2(db_path, attachment_ids=[att for _, att in touched], workers=workers)
        stats["summarised"] = p2["extracted"]
        stats["failed"] = p2["failed"]
        conn = _connect(db_path)
        try:
            marks = ",".join("?" * len(touched))
            redone = [
                ac_id
                for (ac_id,) in conn.execute(
                    f"SELECT id FROM attachment_content WHERE id IN ({marks})"
                    " AND llm_status != 'failed'",
                    [ac_id for ac_id, _ in touched],
                )
            ]
        finally:
            conn.close()
        if redone:
            remove_vectors([-ac_id for ac_id in redone])
    return stats
