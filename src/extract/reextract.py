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
    garbled OCR text marked as Greek read as Latin look-alike letters (src/extract/ocr_garble.py),
            read again Greek first. A reading no longer marked replaces the stored text whatever
            its length and is summarised; one still marked stays unsummarised. The marks come
            from Phase 1 and from `ocr-regarble --apply` (src/extract/ocr_regarble.py)
    wide    a long workbook summarised from its digest (since DIGEST_SINCE) when the digest leaves
            out columns of a sheet (attachment_digest.cuts_columns): until 2026-10-16 those were
            its newest, so a report with a column a day was summarised for its oldest days.
            Summarised again, from the stored text; the file is not read. The first run records
            when it began (sync_metadata reextract_wide_since), and a row summarised since is
            not taken again
    decoding text a reader decoded wrongly, read again with the decoding of 2026-10-21
            (attachment_extractors._text_encodings, _decode_output): a UTF-16 export read as
            Latin-1, its byte-order mark stored as "ÿþ" or "þÿ" and a NUL after every Latin
            letter (28 rows, audit 2026-10-20), and a Word file whose converter output failed to
            decode (UnicodeDecodeError). The text read now replaces the stored one whatever its
            length, as for garbled, and is summarised
    failed  a summary that failed for good under a call shape before v2-text, the default since
            V2_TEXT_SINCE: an old parser's IndexError, a refusal, a reply that was not JSON (14
            rows, audit 2026-10-20). Each failure belongs to the request it was made from, and
            that request is no longer sent. Summarised again from the stored text; one that fails
            again carries a later stamp and is not taken again

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

    no spreadsheets  --no-spreadsheets: leave workbooks out of whatever the selectors choose,
            by the test Phase 2 routes them by (attachment_digest.is_spreadsheet, the reader's
            extraction method). A row with no text yet is judged by its re-read, and one that
            turns out a workbook is left as it was (counted as spreadsheets)

Copies of one document are summarised once. The rows to summarise again are grouped by their
content (duplicate_groups: the attachment's sha256, or the stored text when it has none), and
only the lowest content id of each group goes to Phase 2; the others are never set pending. Once
that one has its new summary, the others take it (copy_summaries, counted as duplicates), as
Phase 1 gives a copy of a file the content of the first (attachment_pipeline._reuse_content),
key facts aside, and their vectors are dropped too. A copy whose representative failed or was
left pending keeps its old summary; one read again with it keeps its new text and the summary
status it had. A batch job records its groups, and its collect copies the same way. --dry-run
and --estimate count one row per group, and the others as duplicates.

--token-budget bounds the Phase 2 run (src/llm_cost.py TokenBudget); --estimate prints the calls,
tokens and cost the chosen rows would take and changes nothing (estimate_reextract). --batch sends
the summaries as a Vertex AI batch job at half price, --batch-collect stores its replies, and
--batch --probe sends ten requests and stores nothing (src/extract/vertex_batch.py).
"""

import hashlib
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config import ATTACHMENTS_DIR, DEFAULT_DB
from src.extract.attachment_digest import SPREADSHEET_METHODS, cuts_columns, is_spreadsheet
from src.extract.attachment_extractors import extract_text_from_file
from src.extract.attachment_pipeline import (
    FULL_PARTS_KEY,
    LONG_TEXT_CHARS,
    _connect,
    estimate_summaries,
    llm_status_for,
    mark_full_parts,
    run_phase2,
)
from src.extract.ocr_garble import is_garbled
from src.llm_cost import TokenBudget
from src.redact import redact_secrets
from src.store.file_hashes import locate_file
from src.store.file_sweep import OCR_GARBLED_SQL, PARTIAL_SINCE_KEY, PARTIAL_SQL, UNREAD_SQL
from src.store.stamps import instant, instant_sql, utc_now

OLD_TEXT_CAP = 100_000

# When v2-text, the call shape of today's summaries, reached the producer: #224 merged at 11:48
# UTC on 2026-10-16 and the hourly update pulls within the hour. The failed selector takes the
# failures stamped before it.
V2_TEXT_SINCE = datetime(2026, 10, 16, 13, tzinfo=UTC)
# A UTF-16 byte-order mark, as a reader that took the file for Latin-1 stored it.
_MARKS_READ_AS_LATIN1 = ("ÿþ", "þÿ")

# When the spreadsheet digest reached the producer: a long workbook summarised since was
# summarised from its digest (attachment_pipeline._route), one summarised before from its parts,
# which read every column. The wide selector takes the first kind only.
DIGEST_SINCE = datetime(2026, 10, 10, 21, tzinfo=UTC)  # midnight of 11 October in Athens
# Set when the first wide run begins, as reextract_partial_since is for the partial selector.
WIDE_SINCE_KEY = "reextract_wide_since"
# A workbook reader's method, as is_spreadsheet tells it.
_SPREADSHEET_SQL = (
    "(ac.extraction_method IN ("
    + ", ".join(f"'{m}'" for m in sorted(SPREADSHEET_METHODS))
    + ")"
    + "".join(f" OR ac.extraction_method LIKE '{m} %'" for m in sorted(SPREADSHEET_METHODS))
    + ")"
)

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
    # A text-only document kept no file to read again.
    "garbled": (f"{OCR_GARBLED_SQL} AND a.file_path NOT LIKE 'text:%'", True),
    # Narrowed here, as text, to rows summarised from the day before the digest landed (both
    # stamp forms start with their date); _wide_rows keeps the ones its digest cut.
    "wide": (
        f"ac.llm_status = 'extracted' AND {_SPREADSHEET_SQL}"
        f" AND ac.llm_extracted_at >= '{DIGEST_SINCE.date().isoformat()}'"
        f" AND length(ac.extracted_text) > {LONG_TEXT_CHARS}"
        " AND NOT EXISTS (SELECT 1 FROM sync_metadata f"
        f" WHERE f.key = '{FULL_PARTS_KEY}' || ac.attachment_id)",
        False,
    ),
    # A text-only document kept no file to read again.
    "decoding": (
        "a.file_path NOT LIKE 'text:%' AND ((ac.extraction_method = 'direct_read'"
        " AND substr(ac.extracted_text, 1, 2) IN ("
        + ", ".join(f"'{mark}'" for mark in _MARKS_READ_AS_LATIN1)
        + "))"
        " OR (ac.extraction_status = 'failed'"
        " AND ac.extraction_error LIKE 'UnicodeDecodeError%'))",
        True,
    ),
    "failed": (
        "ac.llm_status = 'failed' AND ac.extraction_status = 'extracted'"
        f" AND {instant_sql('ac.llm_extracted_at')}"
        f" < julianday('{V2_TEXT_SINCE.isoformat()}')",
        False,
    ),
}


def _read_as_latin1(text: str | None, method: str | None) -> bool:
    """Whether a stored text is a UTF-16 file the plain-text reader took for Latin-1."""
    return method == "direct_read" and text is not None and text.startswith(_MARKS_READ_AS_LATIN1)


# The images pass owns these rows (src/extract/image_*.py): never selected, never written.
NOT_VISION = "COALESCE(extraction_method, '') != 'vision'"


def select_rows(
    conn,
    which: set[str],
    limit: int | None = None,
    after_id: int | None = None,
    full_parts: list[int] | None = None,
    no_spreadsheets: bool = False,
) -> list[tuple]:
    """(content id, attachment id, file path, mime type, whether Phase 1 runs, whether the row
    holds text) per chosen row, lowest content id first, above `after_id` when given.

    `full_parts` adds those attachments' rows that hold text, to be summarised again.
    `no_spreadsheets` leaves out the rows a workbook reader read (is_spreadsheet)."""
    rows: dict[int, tuple] = {}
    chosen = [(name, *SELECTORS[name]) for name in sorted(which)]
    if full_parts:
        ids = ",".join(str(int(att)) for att in full_parts)
        chosen.append(
            (
                "full parts",
                f"ac.attachment_id IN ({ids}) AND ac.extraction_status = 'extracted'",
                False,
            )
        )
    for name, where, reread in chosen:
        keep = _wide_rows(conn) if name == "wide" else None
        for ac_id, att_id, file_path, mime, has_text, method in conn.execute(
            "SELECT ac.id, a.id, a.file_path, a.mime_type, ac.extracted_text IS NOT NULL,"
            " ac.extraction_method FROM attachment_content ac"
            f" JOIN attachments a ON a.id = ac.attachment_id WHERE ({where}) AND ac.id > ?"
            " AND COALESCE(ac.extraction_method, '') != 'vision'",
            (after_id or 0,),
        ):
            if no_spreadsheets and is_spreadsheet(method):
                continue
            if keep is not None and not keep(ac_id):
                continue
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


def _wide_rows(conn) -> Callable[[int], bool]:
    """Whether a row the wide selector's SQL found was summarised from a digest that left out
    columns: summarised since DIGEST_SINCE and, once a wide run has begun, before it began, by
    the instant whatever the stamp's form (src/store/stamps.py), and wide by cuts_columns."""
    mark = conn.execute(
        "SELECT value FROM sync_metadata WHERE key = ?", (WIDE_SINCE_KEY,)
    ).fetchone()
    since = instant(mark[0]) if mark else None

    def keep(ac_id: int) -> bool:
        text, stamp = conn.execute(
            "SELECT extracted_text, llm_extracted_at FROM attachment_content WHERE id = ?",
            (ac_id,),
        ).fetchone()
        when = instant(stamp)
        if when is None or when < DIGEST_SINCE or (since is not None and when >= since):
            return False
        return cuts_columns(text)

    return keep


def duplicate_groups(conn, ac_ids) -> dict[int, list[int]]:
    """The content rows grouped by identical content: the attachment's sha256, or, for one
    recorded without it, a hash of the text it holds now (a row with neither is alone).
    Returns {lowest content id of a group: the others in it, lowest first} for every group."""
    ids = sorted(set(ac_ids))
    groups: dict[tuple, list[int]] = {}
    # In chunks: an estimate can take tens of thousands of rows, more than one statement binds.
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for ac_id, sha256, text in conn.execute(
            "SELECT ac.id, a.sha256, ac.extracted_text FROM attachment_content ac"
            f" JOIN attachments a ON a.id = ac.attachment_id WHERE ac.id IN ({marks})",
            chunk,
        ):
            if sha256:
                key: tuple = ("sha256", sha256)
            elif text is not None:
                key = ("text", hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest())
            else:
                key = ("row", ac_id)
            groups.setdefault(key, []).append(ac_id)
    return {min(members): sorted(members)[1:] for members in groups.values()}


def copy_summaries(conn, groups: dict[int, list[int]]) -> list[int]:
    """Give the other rows of each group the new summary of its representative, when it has one
    (llm_status 'extracted'), as Phase 1 gives a copy of a file the first one's content
    (attachment_pipeline._reuse_content). Key facts, decisions and action items are not copied:
    the representative's email carries them. Returns the rows that took a copy, whose vectors
    the caller drops."""
    copied: list[int] = []
    for rep, others in groups.items():
        if not others:
            continue
        found = conn.execute(
            "SELECT summary, language, llm_status, llm_extracted_at FROM attachment_content"
            " WHERE id = ? AND llm_status = 'extracted'",
            (rep,),
        ).fetchone()
        if found is None:
            continue
        for ac_id in others:
            if conn.execute(
                "UPDATE attachment_content SET summary = ?, language = ?, llm_status = ?,"
                f" llm_extracted_at = ?, llm_error = NULL WHERE id = ? AND {NOT_VISION}",
                (*found, ac_id),
            ).rowcount:
                copied.append(ac_id)
    conn.commit()
    return copied


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
    batch: bool = False,
    no_spreadsheets: bool = False,
) -> dict:
    """Read again and summarise again the rows the selectors choose. See the module docstring.

    `full_parts` flags those attachments to be summarised from every part, now and on every
    later summary (src/extract/attachment_pipeline.py CAPPED_SUMMARY_PARTS), and summarises them
    again. `token_budget` bounds the Phase 2 run at the end: a row it leaves pending keeps its
    summary and its vector, and the nightly pass takes it. With `batch` the summaries go as a
    Vertex AI batch job (src/extract/vertex_batch.py), described in stats["batch"]; its collect
    stores them and drops the vectors of what it stores. `no_spreadsheets` leaves workbooks out.
    Copies of one document are summarised once (duplicate_groups, copy_summaries)."""
    db_path = str(db_path or DEFAULT_DB)
    root = Path(root) if root else ATTACHMENTS_DIR
    stats: dict[str, Any] = dict.fromkeys(
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
            "spreadsheets",
            "summarised",
            "duplicates",
            "failed",
            "over_budget",
            "highest_id",
        ),
        0,
    )
    touched: list[tuple[int, int]] = []
    # The rows to summarise again without reading them, set pending once grouped, and the
    # summary status each row read again had, for a copy that waits for its representative.
    resummarise: list[tuple[int, int]] = []
    llm_before: dict[int, tuple] = {}
    copies: dict[int, list[int]] = {}
    conn = _connect(db_path)
    try:
        now = utc_now()  # extracted_at and the partial mark, in UTC (src/store/stamps.py)
        for selector, key in (("partial", PARTIAL_SINCE_KEY), ("wide", WIDE_SINCE_KEY)):
            if selector in (which or set()) and not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO sync_metadata (key, value) VALUES (?, ?)", (key, now)
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
        rows = select_rows(conn, which or set(), limit, after_id, full_parts, no_spreadsheets)
        stats["selected"] = len(rows)
        stats["highest_id"] = rows[-1][0] if rows else 0
        for ac_id, att_id, file_path, mime, reread, has_text in rows:
            if not reread:
                resummarise.append((ac_id, att_id))
                continue
            path = None
            if file_path and not file_path.startswith("text:"):
                path = locate_file(file_path, root)
            if path is None:
                stats["missing"] += 1
                continue
            stats["reread"] += 1
            if dry_run:
                touched.append((ac_id, att_id))
                continue
            result = extract_text_from_file(
                str(path), mime or "", zip_seconds=math.inf, ocr_seconds=math.inf
            )
            if no_spreadsheets and is_spreadsheet(result["method"]):
                stats["spreadsheets"] += 1
                continue
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
            stored, stored_method, stored_error, *llm_status = conn.execute(
                "SELECT extracted_text, extraction_method, extraction_error, llm_status,"
                " llm_error FROM attachment_content WHERE id = ?",
                (ac_id,),
            ).fetchone()
            # A reading no longer marked as Greek read as Latin look-alikes replaces one that was,
            # whatever its length: the stored text's words were wrong, not too few. So does a
            # UTF-16 file read as such where it was stored as Latin-1, twice as long.
            mended = (is_garbled(stored_error) and not is_garbled(result["error"])) or (
                _read_as_latin1(stored, stored_method)
                and not _read_as_latin1(text, result["method"])
            )
            # A reader that says it stopped early (pages left unread) has not read it again.
            close = (
                bool(stored)
                and not mended
                and not result["error"]
                and ocr_close(stored_method, result["method"], stored, text)
            )
            if stored and len(text) < len(stored) and not close and not mended:
                stats["kept"] += 1
                continue
            if has_text and not mended and (text == stored or close):
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
                       extraction_error = ?, extracted_at = ?, llm_status = ?,
                       llm_error = NULL
                   WHERE id = ? AND {NOT_VISION}""",
                (
                    text,
                    result["method"],
                    result["status"],
                    result["error"],
                    now,
                    llm_status_for(result["error"]),
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
            llm_before[ac_id] = tuple(llm_status)
        conn.commit()
        # One row per document goes to Phase 2. A copy read again goes back to the summary
        # status it had, until its representative's new summary reaches it.
        groups = duplicate_groups(conn, dict(resummarise + touched))
        copies = {rep: others for rep, others in groups.items() if others}
        waiting = {ac_id for others in copies.values() for ac_id in others}
        stats["resummarise"] = sum(1 for ac_id, _ in resummarise if ac_id not in waiting)
        if dry_run:
            stats["duplicates"] = len(waiting)
            touched = []
        else:
            for ac_id in waiting & llm_before.keys():
                conn.execute(
                    "UPDATE attachment_content SET llm_status = ?, llm_error = ?"
                    f" WHERE id = ? AND {NOT_VISION}",
                    (*llm_before[ac_id], ac_id),
                )
            for ac_id, att_id in resummarise:
                if (
                    ac_id not in waiting
                    and conn.execute(
                        "UPDATE attachment_content SET llm_status = 'pending', llm_error = NULL"
                        f" WHERE id = ? AND {NOT_VISION}",
                        (ac_id,),
                    ).rowcount
                ):
                    touched.append((ac_id, att_id))
            conn.commit()
            touched = sorted(row for row in touched if row[0] not in waiting)
            sent = {ac_id for ac_id, _ in touched}
            copies = {rep: others for rep, others in copies.items() if rep in sent}
    finally:
        conn.close()
    if touched and batch:
        from src.extract.vertex_batch import submit

        stats["batch"] = submit(
            db_path,
            attachment_ids=[att for _, att in touched],
            token_budget=token_budget,
            origin="reextract",
            copies=copies,
        )
        # Copied when the collect stores their representatives' summaries.
        stats["duplicates_waiting"] = sum(len(others) for others in copies.values())
    elif touched:
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
            copied = copy_summaries(conn, copies)
        finally:
            conn.close()
        stats["duplicates"] = len(copied)
        redone += copied
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
    no_spreadsheets: bool = False,
) -> dict:
    """What reextract would spend on the rows it would take now: their calls, tokens and cost
    (src/extract/attachment_pipeline.py estimate_rows). Writes nothing, flags nothing and asks
    the model nothing.

    A row is estimated from the text it holds now. A row to be read again first may come back
    longer, so for those the estimate is a floor; one that holds no text yet is not estimated
    and is counted in `unknown`. Copies of one document are estimated once and counted in
    `duplicates` (duplicate_groups); `no_spreadsheets` leaves workbooks out."""
    db_path = str(db_path or DEFAULT_DB)
    conn = _connect(db_path)
    try:
        rows = select_rows(conn, which or set(), limit, after_id, full_parts, no_spreadsheets)
        with_text = [row[0] for row in rows if row[5]]
        reps = sorted(duplicate_groups(conn, with_text))
    finally:
        conn.close()
    est = estimate_summaries(db_path, reps, full_parts or (), chars_per_token)
    return {
        **est,
        "selected": len(rows),
        "unknown": len(rows) - len(with_text),
        "duplicates": len(with_text) - len(reps),
    }
