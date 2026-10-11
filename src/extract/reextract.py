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
    stale   skipped as an unsupported type before the byte sniff and the text sniff landed,
            though today's readers take most of them: extensionless and dot-lost names,
            calendar invites, saved mail, SVG, XML
    formats the formats the readers of 2026-10 added: .mso, .wmz and .emz from the skip list,
            .docx and .pptx the library readers failed on, images that would not open
    ocr     scans and images whose OCR found too little text, read again for the grayscale
            second pass. Thousands of rows on the producer: run it in batches

A row is updated in place, so its id, and the vector keyed on it, stay its own. A re-read that
comes back with no text leaves a row that already had text untouched (counted as kept): a
timeout or a parser error must not replace what was stored. A row that holds no text takes the
verdict of a re-read with none, 'encrypted' or a skip with its reason, unless that verdict is a
failure (counted as relabelled when it changed, unchanged when not). A re-read that comes back
with the same text only records when it was read (counted as unchanged): nothing new to
summarise. A row the images pass owns (extraction_method 'vision') is never selected, and no
write lands on one, even a row that became one while the file was being read.
OCR is the exception to "shorter is kept": the same scan read again on another machine or another
tesseract drifts by a few characters either way, so an OCR re-read within the OCR drift of what is
stored is the same text read again (counted as ocr_close): the row is read, its text and summary
stay. A batch runner passes after_id, the highest id the previous batch handled (the command
prints it as "highest id"), so a row kept unread is offered once per pass, not to every batch
after it. The vector is dropped once Phase 2 has given a row a new summary, so the next index
build embeds it; a row whose new summary failed, or that Phase 2 left pending (a token budget,
a re-auth, an outage), keeps its old summary and vector. Phase 2 replaces the attachment's own
key facts, decisions and action items, and does not repeat one an older summary already put on
the email.

    full parts  --full-parts ID...: flag those attachments to be summarised from every part
            (up to 50) rather than three, now and on every later summary, and summarise them
            again. The flag is a sync_metadata row per attachment (attachment_summary_full:<id>);
            delete the row to lift it

--token-budget bounds the Phase 2 run (src/llm_cost.py TokenBudget); --estimate prints the calls,
tokens and cost the chosen rows would take and changes nothing (estimate_reextract).
"""

import math
from datetime import datetime
from pathlib import Path

from src.config import ATTACHMENTS_DIR, DEFAULT_DB
from src.extract.attachment_extractors import extract_text_from_file
from src.extract.attachment_pipeline import (
    LONG_TEXT_CHARS,
    _connect,
    estimate_summaries,
    mark_full_parts,
    run_phase2,
)
from src.llm_cost import TokenBudget
from src.redact import redact_secrets
from src.store.file_hashes import locate_file
from src.store.file_sweep import PARTIAL_SINCE_KEY, PARTIAL_SQL, UNREAD_SQL

OLD_TEXT_CAP = 100_000

# How far an OCR re-read of an unchanged scan drifts from the stored text: the larger of a few
# characters and a small share of its length. Measured 2026-10-04 on the producer (tesseract 5.5.0)
# against scans first read elsewhere (5.5.3): eleven scans the partial pass had kept as shorter
# came back 1 to 14 characters short (215 -> 201, 253 -> 243, 3,255 -> 3,254, 3,973 -> 3,963), never
# more than 0.25% of a text over 900 characters. 50 characters and 1% clear that with room, and
# the call is safe even when the margin is generous: a close read keeps the stored text, a reader
# that says it stopped early never counts, and a scan the old reader cut at 30 pages comes back
# longer by whole pages.
OCR_METHODS = frozenset({"pymupdf+tesseract", "ocr"})
OCR_DRIFT_CHARS = 50
OCR_DRIFT_SHARE = 0.01

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
    # A row whose file is kept for good (the text ceiling, an archive's limits) cannot be
    # finished by reading it again.
    "unread": (
        f"COALESCE({UNREAD_SQL}, 0) AND COALESCE(ac.extraction_error NOT LIKE '%file kept%', 1)",
        True,
    ),
    # A text-only document kept no file to read again.
    "partial": (f"COALESCE({PARTIAL_SQL}, 0) AND a.file_path NOT LIKE 'text:%'", True),
    "stale": (
        "ac.extraction_status = 'skipped' AND ac.extraction_error LIKE 'Unsupported type:%'"
        " AND a.file_path NOT LIKE 'text:%'",
        True,
    ),
    "formats": (
        "a.file_path NOT LIKE 'text:%' AND ("
        "(ac.extraction_status = 'skipped' AND (lower(a.filename) LIKE '%.mso'"
        " OR lower(a.filename) LIKE '%.wmz' OR lower(a.filename) LIKE '%.emz'))"
        " OR (ac.extraction_status = 'failed' AND (lower(a.filename) LIKE '%.docx'"
        " OR lower(a.filename) LIKE '%.pptx'"
        " OR ac.extraction_error LIKE 'UnidentifiedImageError%'"
        " OR ac.extraction_error LIKE 'DecompressionBombError%')))",
        True,
    ),
    "ocr": (
        "ac.extraction_status = 'skipped'"
        " AND (ac.extraction_error = 'OCR returned insufficient text'"
        " OR (ac.extraction_method = 'pymupdf'"
        " AND ac.extraction_error = 'Insufficient text extracted'))",
        True,
    ),
}

# The images pass owns these rows (src/extract/image_*.py): never selected, never written.
NOT_VISION = "COALESCE(extraction_method, '') != 'vision'"


def select_rows(
    conn,
    which: set[str],
    limit: int | None = None,
    after_id: int | None = None,
    full_parts: list[int] | None = None,
) -> list[tuple]:
    """(content id, attachment id, file path, mime type, whether Phase 1 runs, whether the row
    holds text) per chosen row, lowest content id first, above `after_id` when given.

    `full_parts` adds those attachments' rows that hold text, to be summarised again."""
    rows: dict[int, tuple] = {}
    chosen = [SELECTORS[name] for name in sorted(which)]
    if full_parts:
        ids = ",".join(str(int(att)) for att in full_parts)
        chosen.append(
            (f"ac.attachment_id IN ({ids}) AND ac.extraction_status = 'extracted'", False)
        )
    for where, reread in chosen:
        for ac_id, att_id, file_path, mime, has_text in conn.execute(
            "SELECT ac.id, a.id, a.file_path, a.mime_type, ac.extracted_text IS NOT NULL"
            " FROM attachment_content ac"
            f" JOIN attachments a ON a.id = ac.attachment_id WHERE ({where}) AND ac.id > ?"
            " AND COALESCE(ac.extraction_method, '') != 'vision'",
            (after_id or 0,),
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


def ocr_close(old_method: str | None, new_method: str | None, stored: str, text: str) -> bool:
    """Whether an OCR re-read is the stored OCR text read again, give or take the drift."""
    if old_method not in OCR_METHODS or new_method not in OCR_METHODS:
        return False
    return abs(len(text) - len(stored)) <= max(OCR_DRIFT_CHARS, len(stored) * OCR_DRIFT_SHARE)


def reextract(
    db_path=None,
    which: set[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    workers: int = 1,
    root=None,
    after_id: int | None = None,
    full_parts: list[int] | None = None,
    token_budget: TokenBudget | None = None,
) -> dict:
    """Read again and summarise again the rows the selectors choose. See the module docstring.

    `full_parts` flags those attachments to be summarised from every part, now and on every
    later summary (src/extract/attachment_pipeline.py CAPPED_SUMMARY_PARTS), and summarises them
    again. `token_budget` bounds the Phase 2 run at the end: a row it leaves pending keeps its
    summary and its vector, and the nightly pass takes it."""
    db_path = str(db_path or DEFAULT_DB)
    root = Path(root) if root else ATTACHMENTS_DIR
    stats = dict.fromkeys(
        (
            "selected",
            "flagged",
            "reread",
            "resummarise",
            "missing",
            "kept",
            "unchanged",
            "relabelled",
            "ocr_close",
            "summarised",
            "failed",
            "over_budget",
            "highest_id",
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
        if full_parts and not dry_run:
            marks = ",".join("?" * len(full_parts))
            known = [
                att
                for (att,) in conn.execute(
                    f"SELECT id FROM attachments WHERE id IN ({marks})", list(full_parts)
                )
            ]
            mark_full_parts(conn, known)
            stats["flagged"] = len(known)
        rows = select_rows(conn, which or set(), limit, after_id, full_parts)
        stats["selected"] = len(rows)
        stats["highest_id"] = rows[-1][0] if rows else 0
        for ac_id, att_id, file_path, mime, reread, has_text in rows:
            if not reread:
                stats["resummarise"] += 1
                if not dry_run:
                    done = conn.execute(
                        "UPDATE attachment_content SET llm_status = 'pending', llm_error = NULL"
                        f" WHERE id = ? AND {NOT_VISION}",
                        (ac_id,),
                    ).rowcount
                    conn.commit()
                    if done:
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
            # A re-read with no text, or with less than was stored (a reader that suddenly reads
            # less), replaces no text and does not mark a row with text read.
            if not result["text"]:
                if has_text or result["status"] not in ("skipped", "encrypted"):
                    stats["kept"] += 1
                    continue
                # No text before and none now: the row takes the re-read's verdict, encrypted or
                # a skip with its reason, so an old label ("failed: BadZipFile") does not stand.
                verdict = (result["status"], result["method"], result["error"])
                before = conn.execute(
                    "SELECT extraction_status, extraction_method, extraction_error"
                    " FROM attachment_content WHERE id = ?",
                    (ac_id,),
                ).fetchone()
                done = conn.execute(
                    "UPDATE attachment_content SET extraction_status = ?, extraction_method = ?,"
                    f" extraction_error = ?, extracted_at = ? WHERE id = ? AND {NOT_VISION}",
                    (*verdict, now, ac_id),
                ).rowcount
                conn.commit()
                if not done:
                    stats["kept"] += 1
                else:
                    stats["unchanged" if tuple(before) == verdict else "relabelled"] += 1
                continue
            text = redact_secrets(result["text"])
            stored, stored_method = conn.execute(
                "SELECT extracted_text, extraction_method FROM attachment_content WHERE id = ?",
                (ac_id,),
            ).fetchone()
            # A reader that says it stopped early (pages left unread) has not read it again.
            close = (
                bool(stored)
                and not result["error"]
                and ocr_close(stored_method, result["method"], stored, text)
            )
            if stored and len(text) < len(stored) and not close:
                stats["kept"] += 1
                continue
            if has_text and (text == stored or close):
                # Read again and found the same: only when, and by what, is new.
                conn.execute(
                    "UPDATE attachment_content SET extraction_method = ?, extraction_status = ?,"
                    f" extraction_error = ?, extracted_at = ? WHERE id = ? AND {NOT_VISION}",
                    (result["method"], result["status"], result["error"], now, ac_id),
                )
                conn.commit()
                stats["unchanged" if text == stored else "ocr_close"] += 1
                continue
            done = conn.execute(
                f"""UPDATE attachment_content
                   SET extracted_text = ?, extraction_method = ?, extraction_status = ?,
                       extraction_error = ?, extracted_at = ?, llm_status = 'pending',
                       llm_error = NULL
                   WHERE id = ? AND {NOT_VISION}""",
                (
                    text,
                    result["method"],
                    result["status"],
                    result["error"],
                    now,
                    ac_id,
                ),
            ).rowcount
            # Per row: the next re-read can take minutes, and an open write transaction
            # meanwhile blocks every other writer.
            conn.commit()
            if not done:  # it became a 'vision' row while the file was read
                stats["kept"] += 1
                continue
            touched.append((ac_id, att_id))
        conn.commit()
    finally:
        conn.close()
    if touched:
        from src.store.embeddings import remove_vectors

        p2 = run_phase2(
            db_path,
            attachment_ids=[att for _, att in touched],
            workers=workers,
            token_budget=token_budget,
        )
        stats["summarised"] = p2["extracted"]
        stats["failed"] = p2["failed"]
        stats["over_budget"] = p2.get("over_budget", 0)
        conn = _connect(db_path)
        try:
            marks = ",".join("?" * len(touched))
            # Only a row with a new summary: one left pending (the budget, a re-auth, an
            # outage) still has its old summary, and dropping its vector would take it out of
            # semantic search until the next summary.
            redone = [
                ac_id
                for (ac_id,) in conn.execute(
                    f"SELECT id FROM attachment_content WHERE id IN ({marks})"
                    " AND llm_status = 'extracted'",
                    [ac_id for ac_id, _ in touched],
                )
            ]
        finally:
            conn.close()
        if redone:
            remove_vectors([-ac_id for ac_id in redone])
    return stats


def estimate_reextract(
    db_path=None,
    which: set[str] | None = None,
    limit: int | None = None,
    after_id: int | None = None,
    full_parts: list[int] | None = None,
    chars_per_token: float | None = None,
) -> dict:
    """What reextract would spend on the rows it would take now: their calls, tokens and cost
    (src/extract/attachment_pipeline.py estimate_rows). Writes nothing, flags nothing and asks
    the model nothing.

    A row is estimated from the text it holds now. A row to be read again first may come back
    longer, so for those the estimate is a floor; one that holds no text yet is not estimated
    and is counted in `unknown`."""
    db_path = str(db_path or DEFAULT_DB)
    conn = _connect(db_path)
    try:
        rows = select_rows(conn, which or set(), limit, after_id, full_parts)
    finally:
        conn.close()
    with_text = [row[0] for row in rows if row[5]]
    est = estimate_summaries(db_path, with_text, full_parts or (), chars_per_token)
    return {**est, "selected": len(rows), "unknown": len(rows) - len(with_text)}
