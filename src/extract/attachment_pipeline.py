"""Attachment content extraction pipeline.

Phase 1: Local text extraction (free, no API calls).
Phase 2: Vertex AI structured extraction (LLM) — added in Task 4.
Ingest: Import standalone documents (not from email) into the knowledge store.
"""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from src.config import ATTACHMENTS_DIR, DATA_ROOT, DEFAULT_DB
from src.extract.attachment_digest import is_spreadsheet, spreadsheet_digest
from src.extract.attachment_extractors import extract_text_from_file
from src.extract.vertex_auth import touch_sentinel
from src.llm_cost import (
    ESTIMATE_MODEL,
    OUTPUT_TOKENS_PER_CALL,
    RATES,
    OverBudget,
    TokenBudget,
    configured_chars_per_token,
    cost_usd,
)
from src.redact import redact_secrets
from src.store.file_hashes import sha256_of_file
from src.store.file_sweep import NOT_FULLY_READ_SQL
from src.store.source_class import stored_class

# Processing constants
PHASE2_BATCH_SIZE = 10
PHASE2_COOLDOWN = 3  # seconds between LLM batches
LLM_MAX_TEXT = 50_000  # max chars sent to LLM

# The last element of a phase-2 worker's result when the service failed rather
# than the item (policy_bridge.is_transient): offered again, but no re-auth.
TRANSIENT = "transient"

# Phase 2 summarises a text longer than this in parts (spec: long files). The hourly sync
# leaves such texts to the nightly pass, whose budget can hold a document's many calls.
LONG_TEXT_CHARS = 50_000

# The last element of a phase-2 worker's result when the budget ran out between the parts of a
# long document: left pending for the next run, neither a failure nor a re-auth.
DEFERRED = "deferred"

# A row flagged for full parts (reextract --full-parts) is summarised from this many parts,
# spread evenly across it (2,000,000 characters at 40,000 a part). Every part of a
# 40M-character log would be 1,000 calls in a row, more than a night's budget. The whole text
# is still stored and searchable.
MAX_SUMMARY_PARTS = 50

# Any other long text is summarised from this many: its first part, its last, and the part
# richest in headings or table-of-contents lines between them (the middle part when none has
# any), then the merge. Fifty parts and a merge cost about $3.40 a document at Vertex eu prices,
# and their key facts were mostly cell and line dumps (audit 2026-10-11). A long spreadsheet
# takes one call over its digest instead (src/extract/attachment_digest.py).
CAPPED_SUMMARY_PARTS = 3

# The flag that lifts the cap for one attachment, a sync_metadata key per attachment id, so
# it needs no schema change: set by reextract --full-parts, it holds for every later summary.
FULL_PARTS_KEY = "attachment_summary_full:"

# The last element of a phase-2 worker's result when the run's token budget refused its next
# call: left pending for the next run, neither a failure nor a re-auth.
OVER_BUDGET = "over budget"

# Each finished part of a long document is kept here, one JSON file per attachment_content
# row, so a document the budget cuts short resumes where it stopped. Until 2026-10-04 a
# deferred text started over the next night: its finished parts were paid for and dropped,
# night after night. A part is reused only while its text and place are unchanged, and the
# file goes once the merge is done.
PARTS_DIR = DATA_ROOT / "state" / "attachment_parts"


def _connect(db_path: str) -> sqlite3.Connection:
    """Open brain.db with the same concurrency settings as schema.get_connection.

    sb-auth-watch's restoration trigger starts six DB-writing sb-* units in the
    same instant (observed 2026-08-24 11:00:28), and this module's own 30s
    timeout was half the 60s that get_connection deliberately sets for that
    case — sb-attachments died with "database is locked". get_connection itself
    is not reused here: it forces row_factory=Row and foreign_keys=ON, which
    this module's tuple-indexed queries and cross-table writes do not expect.
    """
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _build_mime_type_conditions(file_type: str | None) -> tuple[str, list]:
    """Build SQL WHERE clause fragment and parameters for file type filter.

    Returns:
        Tuple of (where_clause, params_list)
    """
    if not file_type:
        return "", []

    type_map = {
        "pdf": ("a.mime_type = ?", ["application/pdf"]),
        "word": (
            "a.mime_type IN (?, ?)",
            [
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/msword",
            ],
        ),
        "pptx": (
            "a.mime_type IN (?, ?)",
            [
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "application/vnd.ms-powerpoint",
            ],
        ),
        "excel": (
            "a.mime_type IN (?, ?)",
            [
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.ms-excel",
            ],
        ),
        "image": ("a.mime_type LIKE ?", ["image/%"]),
        "eml": ("a.mime_type = ?", ["message/rfc822"]),
        "rpmsg": ("a.mime_type = ?", ["application/encrypted"]),
    }

    condition, params = type_map.get(file_type, ("", []))
    if condition:
        return f"AND {condition}", params
    return "", []


# Another attachment with the same bytes whose row is fully finished: text extracted from the
# file itself and summarised. Only that is passed on. A failure or a skip may come from an older
# extractor (a zip the old code never unpacked, a converter a host lacked), and a copy that
# inherited it would never meet the current one; reading it again costs no model call.
_REUSABLE_SQL = f"""
    SELECT ac.extracted_text, ac.extraction_method, ac.extraction_status,
           ac.extraction_error, ac.summary, ac.language, ac.llm_status
    FROM attachments a
    JOIN attachment_content ac ON ac.attachment_id = a.id
    WHERE a.sha256 = ? AND a.id != ?
      AND NOT COALESCE({NOT_FULLY_READ_SQL}, 0)
      AND ac.extraction_status = 'extracted' AND ac.llm_status = 'extracted'
    ORDER BY ac.id
    LIMIT 1
"""


def _reuse_content(conn: sqlite3.Connection, att_id: int, sha256: str, now: str) -> str | None:
    """Copy the finished content row of another attachment with the same bytes.

    Returns the copied extraction status, or None when there is nothing to copy. The key facts
    are not copied: they describe the document, and the first email already carries them.
    """
    row = conn.execute(_REUSABLE_SQL, (sha256, att_id)).fetchone()
    if row is None:
        return None
    text, method, status, error, summary, language, llm_status = row
    conn.execute(
        """INSERT OR IGNORE INTO attachment_content
           (attachment_id, extracted_text, extraction_method, extraction_status,
            extraction_error, extracted_at, summary, language, llm_status, llm_extracted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (att_id, text, method, status, error, now, summary, language, llm_status, now),
    )
    return status


def run_phase1(
    db_path: str | None = None,
    limit: int = 0,
    file_type: str | None = None,
    attachment_ids: list[int] | None = None,
    deadline_s: float | None = None,
) -> dict:
    """Run Phase 1: local text extraction for all unprocessed attachments.

    Args:
        db_path: Path to database (defaults to DEFAULT_DB)
        limit: Max attachments to process (0 = no limit)
        file_type: Filter by type: 'pdf', 'word', 'pptx', 'excel', 'image', 'eml', 'rpmsg'
        attachment_ids: If given, only process these attachment IDs (for sync scoping)
        deadline_s: Optional wall-clock budget in seconds. Once spent, no further
            attachments are STARTED and the rest are returned as `deferred`; the
            one in flight runs to completion and is committed. None = no time box.

            `limit` cannot serve this purpose: per-attachment cost spans three
            orders of magnitude, from ~0.1 s for a text part to minutes for OCR
            (the most common extraction_method here) or a 30 MB workbook. A
            caller under an external timeout — sb-outlook-sync has
            TimeoutStartSec=10min — needs to bound TIME, not count, or it gets
            SIGTERMed mid-file. Same contract as run_backfill(deadline_s=...).

    Returns:
        Dict with processing stats: processed, extracted, failed, skipped, encrypted (no text
        without a key, src/extract/attachment_extractors.encrypted_result), deferred, reused.
    """
    db_path = db_path or str(DEFAULT_DB)
    conn = _connect(db_path)
    now = datetime.now().isoformat()

    # Build query with parameterized conditions
    type_condition, type_params = _build_mime_type_conditions(file_type)

    # Build base query
    query = """
        SELECT a.id, a.file_path, a.mime_type, a.filename, a.sha256
        FROM attachments a
        LEFT JOIN attachment_content ac ON a.id = ac.attachment_id
        WHERE ac.id IS NULL
    """

    params = list(type_params)

    # Scope to specific attachment IDs if provided
    if attachment_ids:
        placeholders = ",".join("?" * len(attachment_ids))
        query += f"\n        AND a.id IN ({placeholders})"
        params.extend(attachment_ids)

    # Add type filter if present
    if type_condition:
        query += f"\n        {type_condition}"

    query += "\n        ORDER BY a.id"

    # Add limit if specified (safe - integer validated)
    if limit > 0:
        query += f"\n        LIMIT {int(limit)}"

    # Find attachments not yet in attachment_content
    rows = conn.execute(query, params).fetchall()

    stats = {
        "processed": 0,
        "extracted": 0,
        "failed": 0,
        "skipped": 0,
        "encrypted": 0,
        "deferred": 0,
        "reused": 0,
    }
    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    for att_id, file_path, mime_type, _filename, sha256 in rows:
        # Checked before starting, never mid-file: extract_text_from_file has no
        # interruption point, and abandoning a partial parse would leave the row
        # unwritten and the work repeated next run.
        if deadline is not None and time.monotonic() >= deadline:
            stats["deferred"] += 1
            continue

        # The same bytes seen before: take that row, no extraction and no model call.
        if sha256:
            reused = _reuse_content(conn, att_id, sha256, now)
            if reused is not None:
                conn.commit()
                stats["processed"] += 1
                stats["reused"] += 1
                stats[reused] = stats.get(reused, 0) + 1
                continue

        result = extract_text_from_file(file_path, mime_type or "")
        # Attachments do NOT pass through data/staging, so the redaction applied
        # by write_json_atomic never sees them. A .env, a config dump or a
        # screenshot of a terminal reaches the store and then Vertex by this path
        # instead. Redact here, at the equivalent boundary.
        result["text"] = redact_secrets(result["text"])

        conn.execute(
            """INSERT OR IGNORE INTO attachment_content
               (attachment_id, extracted_text, extraction_method,
                extraction_status, extraction_error, extracted_at, llm_status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (
                att_id,
                result["text"],
                result["method"],
                result["status"],
                result["error"],
                now,
            ),
        )

        # Committed per row: the next extraction can take minutes (OCR, an archive), and an
        # open write transaction across it blocks every other writer until SQLite gives up
        # with "database is locked".
        conn.commit()

        stats["processed"] += 1
        stats[result["status"]] = stats.get(result["status"], 0) + 1

    conn.commit()
    conn.close()
    return stats


def _complete_and_parse(prompt: str, budget: TokenBudget | None = None) -> dict:
    """One Phase 2 model call, parsed into an extraction.

    With a budget the call is held against it before it is sent, and raises OverBudget instead
    of being sent when it does not fit; once answered it is charged what the response reports.
    """
    from src.extract.claude_extract import _response_text, complete
    from src.extract.parser import parse_extraction

    held = budget.reserve(len(prompt)) if budget is not None else 0
    try:
        response = complete(
            # Dense documents (large spreadsheets/decks) yield long extraction
            # JSON; 2048 truncated it mid-structure on ~40K-char docs, so every
            # such attachment failed with "Expecting ',' delimiter". Give the
            # structured output room to complete; parse_extraction additionally
            # salvages any residual truncation rather than dropping the summary.
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
    except BaseException:
        if budget is not None:
            budget.release(held)
        raise
    if budget is not None:
        budget.settle(held, getattr(response, "usage", None))

    # _response_text, never content[0]. With extended thinking the model leads the
    # content list with a ThinkingBlock, which carries .thinking and no .text, so
    # content[0].text raised "'ThinkingBlock' object has no attribute 'text'".
    # 134 attachments failed permanently that way between 2026-08-06 and 2026-08-25;
    # llm_status='failed' is terminal because run_phase2 only re-selects 'pending'.
    # A thinking-only response (max_tokens spent before any text) used to raise
    # "IndexError: list index out of range" here, 3 rows; it now raises a ValueError
    # naming stop_reason and the block types, which is diagnosable from llm_error.
    raw_text = _response_text(response)
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1])
    return parse_extraction(raw_text)


def _part_key(*inputs) -> str:
    """Fingerprint of what a part's prompt is built from: its text, place and metadata."""
    digest = hashlib.sha256()
    for value in inputs:
        digest.update(repr(value).encode())
        digest.update(b"\x00")
    return digest.hexdigest()


def _load_parts(ac_id) -> dict:
    """The parts a row has already finished, by index. Unreadable means none."""
    if ac_id is None:
        return {}
    try:
        return json.loads((PARTS_DIR / f"{ac_id}.json").read_text()).get("parts", {})
    except Exception:
        return {}


def _save_parts(ac_id, parts: dict) -> None:
    """Never raises: a lost save costs a part paid again, which is where it started."""
    if ac_id is None:
        return
    try:
        PARTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PARTS_DIR / f".{ac_id}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps({"parts": parts}))
        os.replace(tmp, PARTS_DIR / f"{ac_id}.json")
    except Exception:
        pass


def _drop_parts(ac_id) -> None:
    if ac_id is None:
        return
    try:
        (PARTS_DIR / f"{ac_id}.json").unlink(missing_ok=True)
    except Exception:
        pass


def _extract_in_parts(
    text,
    filename,
    mime_type,
    email_subject,
    email_date,
    out_of_time,
    ac_id=None,
    full=False,
    budget=None,
):
    """Summarise a long text part by part, then once over the parts. None when time ran out.

    The parts are the ones _choose_parts picks: three, or up to MAX_SUMMARY_PARTS when `full`.
    With ``ac_id`` every finished part is saved as it lands, and a part saved by an
    earlier run is reused while its text and place are unchanged, so a document the
    budget cut short resumes instead of starting over. A token budget that refuses a
    part raises OverBudget, with the finished parts saved."""
    from src.extract.attachment_prompt import (
        build_attachment_prompt,
        build_merge_prompt,
        split_text,
    )

    parts = split_text(text)
    chosen = _choose_parts(parts, full)
    saved = _load_parts(ac_id)
    extractions = []
    for i in chosen:
        key = _part_key(parts[i], i, len(parts), filename, mime_type, email_subject, email_date)
        entry = saved.get(str(i))
        if isinstance(entry, dict) and entry.get("key") == key:
            extractions.append(entry["extraction"])
            continue
        if out_of_time is not None and out_of_time():
            return None
        extraction = _complete_and_parse(
            build_attachment_prompt(
                extracted_text=parts[i],
                filename=filename,
                mime_type=mime_type,
                email_subject=email_subject,
                email_date=email_date,
                part=(i + 1, len(parts)),
            ),
            budget,
        )
        extractions.append(extraction)
        saved[str(i)] = {"key": key, "extraction": extraction}
        _save_parts(ac_id, saved)
    if out_of_time is not None and out_of_time():
        return None
    merged = _complete_and_parse(
        build_merge_prompt(
            extractions,
            filename=filename,
            mime_type=mime_type,
            email_subject=email_subject,
            email_date=email_date,
            covered=(len(chosen), len(parts)),
        ),
        budget,
    )
    _drop_parts(ac_id)
    return merged


def _spread(n: int, k: int) -> list[int]:
    """At most k of the indices 0..n-1, spread evenly, the first and the last included."""
    if n <= k:
        return list(range(n))
    return [round(j * (n - 1) / (k - 1)) for j in range(k)]


# What marks a part as the document's outline: a contents title, dotted contents lines ending
# in a page number, and headings (numbered, Markdown, or a short line in capitals).
_CONTENTS_TITLE = re.compile(
    r"^\s*(?:table of contents|contents|περιεχόμενα|πίνακας περιεχομένων)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CONTENTS_LINE = re.compile(r"^.{3,120}?(?:\.{3,}|…+)\s*\d{1,4}\s*$", re.MULTILINE)
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+\S.{0,100}|(?:\d{1,2}(?:\.\d{1,2}){0,3}|[IVX]{1,5})[.)]?\s+\S.{0,100})$",
    re.MULTILINE,
)


def _outline_score(text: str) -> int:
    """How much of a part reads as headings or a table of contents."""
    capitals = sum(
        1
        for line in text.splitlines()
        if 4 <= len(line.strip()) <= 80 and line.isupper() and sum(c.isalpha() for c in line) >= 3
    )
    return (
        10 * len(_CONTENTS_TITLE.findall(text))
        + 2 * len(_CONTENTS_LINE.findall(text))
        + len(_HEADING.findall(text))
        + capitals
    )


def _choose_parts(parts: list[str], full: bool) -> list[int]:
    """The parts of a long text that are summarised, by index, in order.

    Flagged (`full`): up to MAX_SUMMARY_PARTS spread evenly. Otherwise CAPPED_SUMMARY_PARTS:
    the first part, the last, and between them the one richest in headings or contents lines,
    the earliest on a tie and the middle one when none has any."""
    n = len(parts)
    if full:
        return _spread(n, MAX_SUMMARY_PARTS)
    if n <= CAPPED_SUMMARY_PARTS:
        return list(range(n))
    scores = {i: _outline_score(parts[i]) for i in range(1, n - 1)}
    best = max(scores, key=lambda i: (scores[i], -i))
    return [0, best if scores[best] else n // 2, n - 1]


class _Phase2Row(NamedTuple):
    """A Phase 2 candidate as run_phase2 selects it. The last two have defaults, so a bare
    eight-column row (an older caller's) reads as a text of unknown method, not flagged."""

    ac_id: int
    att_id: int
    text: str
    filename: str
    mime_type: str | None
    email_id: int | None
    email_subject: str | None
    email_date: str | None
    method: str | None = None
    full: bool = False


def _route(row: _Phase2Row) -> str:
    """How a row is summarised: 'single' (one call over the text), 'digest' (one call over a
    long spreadsheet's digest) or 'parts' (chosen parts, then a merge)."""
    if len(row.text or "") <= LONG_TEXT_CHARS:
        return "single"
    if not row.full and is_spreadsheet(row.method):
        return "digest"
    return "parts"


def _attachment_prompt(row: _Phase2Row, text: str, **kwargs) -> str:
    from src.extract.attachment_prompt import build_attachment_prompt

    return build_attachment_prompt(
        extracted_text=text,
        filename=row.filename,
        mime_type=row.mime_type or "",
        email_subject=row.email_subject,
        email_date=row.email_date,
        **kwargs,
    )


def mark_full_parts(conn: sqlite3.Connection, attachment_ids: Iterable[int]) -> None:
    """Flag attachments to be summarised from every part (up to MAX_SUMMARY_PARTS)."""
    now = datetime.now().isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        [(f"{FULL_PARTS_KEY}{int(att)}", now) for att in attachment_ids],
    )
    conn.commit()


def full_parts_ids(conn: sqlite3.Connection) -> set[int]:
    """The attachment ids flagged for full parts."""
    return {
        int(key[len(FULL_PARTS_KEY) :])
        for (key,) in conn.execute(
            "SELECT key FROM sync_metadata WHERE key LIKE ?", (f"{FULL_PARTS_KEY}%",)
        )
    }


def _extract_one_attachment(row, out_of_time=None, budget=None):
    """Worker: call LLM for a single attachment.

    Returns ``(ac_id, email_id, extraction, error, auth_error)``. On success ``error`` is
    None and ``extraction`` is the parsed dict; on failure the reverse, with ``error``
    serialised for the DB column. ``auth_error`` is True for a re-authable failure,
    ``TRANSIENT`` for a failure of the service, ``OVER_BUDGET`` when the run's token budget
    refused a call, and False otherwise.

    ``auth_error`` EXISTS BECAUSE THE VERDICT CANNOT BE RECOVERED FROM THE STRING. The
    caller writes ``pending`` for a re-authable failure and ``failed`` for a permanent
    one, and only ``pending`` is ever selected again by run_phase2 — so a
    misclassification here is not a cosmetic label, it is an item that is never retried.
    The classifier answers from the exception TYPE, which exists only inside this except
    block; two of the three auth types (anthropic.AuthenticationError and
    PermissionDeniedError) carry nothing in their message that a pattern list can match,
    so a string test applied downstream calls a recoverable 401 permanent. Decide it here,
    with the exception in hand, and hand the answer on.
    """
    from src.extract.policy_bridge import classify_exception, is_item_timeout, is_transient
    from src.llm_policy import Outcome

    r = _Phase2Row(*row)

    try:
        route = _route(r)
        # A long text goes in parts, and `out_of_time` is asked between them: the deadline is
        # checked before an item is dispatched, and a long document is many calls long.
        if route == "parts":
            extraction = _extract_in_parts(
                r.text,
                r.filename,
                r.mime_type,
                r.email_subject,
                r.email_date,
                out_of_time,
                ac_id=r.ac_id,
                full=r.full,
                budget=budget,
            )
            if extraction is None:
                return (r.ac_id, r.email_id, None, "deferred: out of time between parts", DEFERRED)
            return (r.ac_id, r.email_id, extraction, None, False)

        if route == "digest":
            extraction = _complete_and_parse(
                _attachment_prompt(r, spreadsheet_digest(r.text), digest=True), budget
            )
            # Parts an older run saved for this row, when long spreadsheets still went in parts.
            _drop_parts(r.ac_id)
            return (r.ac_id, r.email_id, extraction, None, False)

        extraction = _complete_and_parse(_attachment_prompt(r, r.text), budget)
        return (r.ac_id, r.email_id, extraction, None, False)

    except OverBudget as e:
        return (r.ac_id, r.email_id, None, f"deferred: {e}", OVER_BUDGET)
    except Exception as e:
        verdict: bool | str = classify_exception(e, None) is Outcome.AUTH_REAUTH_REQUIRED
        # The service failed, not the item, the same split as the calendar path in
        # cli.py. Written 'failed', an outage during the nightly pass would drop
        # the summaries of the whole queue for good. Still False for an item's
        # own fault, so the tuple keeps its shape for every other caller. An item
        # that ran out of time will again, and attachment_content keeps no attempt
        # count to cap it with: pending, it would be sent every night and fail the
        # nightly stage every night, so it stays terminal as before.
        if not verdict and is_transient(e) and not is_item_timeout(e):
            verdict = TRANSIENT
        return (r.ac_id, r.email_id, None, f"{type(e).__name__}: {str(e)[:500]}", verdict)


def _held(conn: sqlite3.Connection, table: str, column: str, email_id: int, value: str) -> bool:
    """Whether the email already carries this row, from its body or an earlier summary."""
    return (
        conn.execute(
            f"SELECT 1 FROM {table} WHERE email_id = ? AND {column} = ? LIMIT 1",
            (email_id, value),
        ).fetchone()
        is not None
    )


_PHASE2_FROM = """
        FROM attachment_content ac
        JOIN attachments a ON a.id = ac.attachment_id
        LEFT JOIN emails e ON e.id = a.email_id
"""
# The columns of a _Phase2Row, bar the flag.
_PHASE2_SELECT = (
    """
        SELECT ac.id, ac.attachment_id, ac.extracted_text,
               a.filename, a.mime_type, a.email_id,
               e.subject, e.date_received, ac.extraction_method"""
    + _PHASE2_FROM
)


def _phase2_candidates(
    file_type: str | None,
    attachment_ids: list[int] | None,
    max_text_chars: int | None,
    limit: int,
    select: str = _PHASE2_SELECT,
) -> tuple[str, list]:
    """The query (and its parameters) for the rows Phase 2 summarises, in the order it takes
    them. `select` is the SELECT and FROM it starts with."""
    type_condition, type_params = _build_mime_type_conditions(file_type)

    query = (
        select
        + """        WHERE ac.extraction_status = 'extracted'
          AND ac.llm_status = 'pending'
    """
    )

    params = list(type_params)

    # Scope to specific attachment IDs if provided
    if attachment_ids:
        placeholders = ",".join("?" * len(attachment_ids))
        query += f"\n        AND ac.attachment_id IN ({placeholders})"
        params.extend(attachment_ids)

    if type_condition:
        query += f"\n        {type_condition}"
    if max_text_chars is not None:
        query += "\n        AND length(ac.extracted_text) <= ?"
        params.append(max_text_chars)
    # Short texts first, then by id. A long document is many calls long, and in id order
    # the long rows of 10-01 came first every night, so 743 short rows waited behind them
    # for days (2026-10-04). A long one cut short now resumes, so going last costs it nothing.
    query += f"\n        ORDER BY length(ac.extracted_text) > {LONG_TEXT_CHARS}, ac.id"
    if limit > 0:
        query += f"\n        LIMIT {int(limit)}"
    return query, params


def run_phase2(
    db_path: str | None = None,
    limit: int = 0,
    deadline_s: float | None = None,
    file_type: str | None = None,
    attachment_ids: list[int] | None = None,
    workers: int = 1,
    max_text_chars: int | None = None,
    token_budget: TokenBudget | None = None,
) -> dict:
    """Run Phase 2: Vertex AI structured extraction on extracted text.

    Processes attachments where extraction_status='extracted' and llm_status='pending'.
    Stores summary in attachment_content, and key_facts/topics/decisions/action_items
    in existing tables linked via email_id.

    Args:
        db_path: Path to database (defaults to DEFAULT_DB)
        limit: Max attachments to process (0 = no limit)
        deadline_s: Optional wall-clock budget in seconds. Once spent, nothing
            further is DISPATCHED and the rest are returned as `deferred`, left
            at llm_status='pending' so the nightly drain still finds them.

            A count limit is not a budget for this stage: it is network-bound,
            so 25 calls read as modest and cost 6-8 minutes at ~15-25 s each.
            That is what kept SIGTERMing sb-outlook-sync (TimeoutStartSec=600)
            even after Phase 1 was bounded. Same contract as run_phase1.
        file_type: Filter by original MIME type
        attachment_ids: If given, only process these attachment IDs (for sync scoping)
        workers: Number of concurrent LLM workers (default 1)
        max_text_chars: Leave texts longer than this pending. The hourly sync passes
            LONG_TEXT_CHARS: a long text is summarised in many calls, which belong in the
            nightly pass's budget, not in a 600 s unit.
        token_budget: Optional TokenBudget, input and output tokens together. Each call is
            estimated before it is sent, and the first that would pass the budget stops the
            run: nothing further is dispatched, and the rest are returned as `over_budget`,
            left at llm_status='pending' for the next run. None = no limit.

    Returns:
        Dict with processing stats: processed, extracted, failed, deferred, over_budget.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from src.store.normalizer import find_or_create_topic

    db_path = db_path or str(DEFAULT_DB)
    conn = _connect(db_path)

    query, params = _phase2_candidates(file_type, attachment_ids, max_text_chars, limit)
    flagged = full_parts_ids(conn)
    rows = [_Phase2Row._make((*r, r[1] in flagged)) for r in conn.execute(query, params)]

    stats = {"processed": 0, "extracted": 0, "failed": 0, "deferred": 0, "over_budget": 0}
    deadline = None if deadline_s is None else time.monotonic() + deadline_s

    def _out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    def _over_budget() -> bool:
        return token_budget is not None and token_budget.exhausted

    def _store_result(ac_id, email_id, extraction, error, auth_error):
        """Store a single result in the DB (called from main thread).

        ``auth_error`` is the worker's verdict, taken from the same classifier the retry
        policy uses. Re-deriving it here from ``error`` — which is a string by the time it
        arrives — is what made a surviving 401 permanent: see _extract_one_attachment.
        """
        if auth_error == DEFERRED:
            # Out of time between the parts of a long document: still pending, and not a
            # failure, so the next run takes it from the start.
            stats["deferred"] += 1
            return
        if auth_error == OVER_BUDGET:
            # The run's token budget refused its next call: still pending, and not a failure.
            # The parts a long document finished are saved, so the next run resumes it.
            stats["over_budget"] += 1
            return
        now = datetime.now().isoformat()
        if error:
            if auth_error == TRANSIENT:
                # Offered again next run, and still counted, so a run during an
                # outage that lasts reads as failed rather than as a quiet night.
                conn.execute(
                    """UPDATE attachment_content
                       SET llm_status = 'pending', llm_error = ?, llm_extracted_at = ?
                       WHERE id = ?""",
                    (error, now, ac_id),
                )
                stats["failed"] += 1
            elif auth_error:
                # Vertex ADC expired. Mark pending so the next cron retries
                # automatically once the user re-auths (auth-watch clears
                # the sentinel on its next probe). See vertex_auth.py.
                touch_sentinel()
                conn.execute(
                    """UPDATE attachment_content
                       SET llm_status = 'pending', llm_error = ?, llm_extracted_at = ?
                       WHERE id = ?""",
                    (
                        f"deferred (gcloud reauth needed): {str(error)[:200]}",
                        now,
                        ac_id,
                    ),
                )
                stats.setdefault("deferred", 0)
                stats["deferred"] += 1
            else:
                conn.execute(
                    """UPDATE attachment_content
                       SET llm_status = 'failed', llm_error = ?, llm_extracted_at = ?
                       WHERE id = ?""",
                    (error, now, ac_id),
                )
                stats["failed"] += 1
        else:
            # llm_error = NULL matters as much as the summary. The deferral
            # branch above writes "deferred (gcloud reauth needed)" into it, and
            # this success path used to leave that string behind, so a row that
            # was retried and extracted correctly still LOOKED deferred forever.
            # The health check counts that string, so 16 rows deferred on
            # 2026-07-11 were reported as a live ADC backlog every day for seven
            # weeks, and got re-attributed to whichever gcloud incident was most
            # recent. A monitor that cannot go back to zero is not a monitor.
            conn.execute(
                """UPDATE attachment_content
                   SET summary = ?, language = ?, llm_status = 'extracted',
                       llm_error = NULL, llm_extracted_at = ?
                   WHERE id = ?""",
                (extraction.get("summary"), extraction.get("language"), now, ac_id),
            )

            att_id = conn.execute(
                "SELECT attachment_id FROM attachment_content WHERE id = ?", (ac_id,)
            ).fetchone()[0]
            if email_id:
                # The attachment's own rows are replaced by a new summary of it. Rows written
                # before v28 carry no attachment_id, so a row already on the email is not added
                # twice: it may be an older summary of this same attachment.
                for table in ("decisions", "action_items", "key_facts"):
                    conn.execute(f"DELETE FROM {table} WHERE attachment_id = ?", (att_id,))

                for topic_name in extraction.get("topics", []):
                    topic_id = find_or_create_topic(conn, topic_name)
                    conn.execute(
                        "INSERT OR IGNORE INTO email_topics (email_id, topic_id) VALUES (?, ?)",
                        (email_id, topic_id),
                    )

                # The texts are masked as the loader masks an email's
                # (src/store/loader.py), and compared masked with what the email holds.
                for decision in extraction.get("decisions", []):
                    if isinstance(decision, dict) and decision.get("decision"):
                        text = redact_secrets(decision["decision"])
                        if _held(conn, "decisions", "decision", email_id, text):
                            continue
                        conn.execute(
                            "INSERT INTO decisions (email_id, decision, decided_by, attachment_id)"
                            " VALUES (?, ?, ?, ?)",
                            (
                                email_id,
                                text,
                                decision.get("decided_by"),
                                att_id,
                            ),
                        )

                for action in extraction.get("action_items", []):
                    if isinstance(action, dict) and action.get("task"):
                        task = redact_secrets(action["task"])
                        if _held(conn, "action_items", "task", email_id, task):
                            continue
                        conn.execute(
                            "INSERT INTO action_items"
                            " (email_id, task, owner, deadline, status, attachment_id)"
                            " VALUES (?, ?, ?, ?, 'open', ?)",
                            (
                                email_id,
                                task,
                                action.get("owner"),
                                action.get("deadline"),
                                att_id,
                            ),
                        )

                for fact in map(redact_secrets, extraction.get("key_facts", [])):
                    if fact and not _held(conn, "key_facts", "fact", email_id, fact):
                        conn.execute(
                            "INSERT INTO key_facts (email_id, fact, attachment_id) VALUES (?, ?, ?)",
                            (email_id, fact, att_id),
                        )

            stats["extracted"] += 1

        stats["processed"] += 1
        # Committed per result: the next one may be minutes away (a long text in parts), and
        # an open write transaction meanwhile blocks every other writer.
        conn.commit()

    if workers <= 1:
        # Sequential mode (original behavior)
        for row in rows:
            # Checked before dispatch: the row stays llm_status='pending', so the
            # nightly drain picks it up rather than the work being lost.
            if _out_of_time():
                stats["deferred"] += 1
                continue
            if _over_budget():
                stats["over_budget"] += 1
                continue
            ac_id, email_id, extraction, error, auth_error = _extract_one_attachment(
                row, _out_of_time, token_budget
            )
            _store_result(ac_id, email_id, extraction, error, auth_error)
            if stats["processed"] % PHASE2_BATCH_SIZE == 0:
                time.sleep(PHASE2_COOLDOWN)
    else:
        # Concurrent mode: LLM calls in parallel, DB writes serialized
        with ThreadPoolExecutor(max_workers=workers) as executor:
            # Checked inside the task, not while submitting: a pre-flight filter
            # evaluates the budget once at t=0, dispatches everything, and lets
            # the pool run to completion — the deadline becomes a no-op, which is
            # how sb-attachments ran 35 minutes into a 30-minute cap. Queued
            # tasks now drain instantly once the budget is spent, so the pool
            # closes promptly. Same shape as run_backfill's worker().
            def _run_or_defer(row):
                if _out_of_time():
                    return None
                if _over_budget():
                    return (row[0], row[5], None, "deferred: token budget spent", OVER_BUDGET)
                return _extract_one_attachment(row, _out_of_time, token_budget)

            futures = {executor.submit(_run_or_defer, row): row for row in rows}
            for future in as_completed(futures):
                outcome = future.result()
                if outcome is None:
                    stats["deferred"] += 1
                    continue
                ac_id, email_id, extraction, error, auth_error = outcome
                _store_result(ac_id, email_id, extraction, error, auth_error)

    conn.commit()
    conn.close()
    return stats


def _planned_calls(row: _Phase2Row) -> list[tuple[int, int]]:
    """Each call Phase 2 would make for a row, as (prompt characters, part answers it carries).

    The prompts are built as the run builds them; the merge's carries the chosen parts'
    answers, which do not exist before the run and are counted at OUTPUT_TOKENS_PER_CALL each.
    """
    from src.extract.attachment_prompt import build_merge_prompt, split_text

    route = _route(row)
    if route == "single":
        return [(len(_attachment_prompt(row, row.text)), 0)]
    if route == "digest":
        return [(len(_attachment_prompt(row, spreadsheet_digest(row.text), digest=True)), 0)]
    parts = split_text(row.text)
    chosen = _choose_parts(parts, row.full)
    calls = [(len(_attachment_prompt(row, parts[i], part=(i + 1, len(parts)))), 0) for i in chosen]
    merge = build_merge_prompt(
        [],
        filename=row.filename,
        mime_type=row.mime_type or "",
        email_subject=row.email_subject,
        email_date=row.email_date,
        covered=(len(chosen), len(parts)),
    )
    return [*calls, (len(merge), len(chosen))]


def estimate_rows(rows: Iterable[_Phase2Row], chars_per_token: float | None = None) -> dict:
    """The calls, tokens and cost of summarising these rows, asking the model nothing.

    Input tokens are the prompts' characters at `chars_per_token` (BRAIN_CHARS_PER_TOKEN,
    1.6 by default); output tokens are OUTPUT_TOKENS_PER_CALL a call. Priced at ESTIMATE_MODEL,
    online and as a batch job. A cut-short document's saved parts are counted again, so for
    one of those the estimate is an upper bound."""
    cpt = chars_per_token or configured_chars_per_token()
    count = calls = input_tokens = 0
    for row in rows:
        count += 1
        for chars, answers in _planned_calls(row):
            calls += 1
            input_tokens += math.ceil(chars / cpt) + answers * OUTPUT_TOKENS_PER_CALL
    output_tokens = calls * OUTPUT_TOKENS_PER_CALL
    rates = RATES[ESTIMATE_MODEL]
    return {
        "rows": count,
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "chars_per_token": cpt,
        "model": ESTIMATE_MODEL,
        "cost_usd": cost_usd(rates, input_tokens=input_tokens, output_tokens=output_tokens),
        "batch_cost_usd": cost_usd(
            rates, input_tokens=input_tokens, output_tokens=output_tokens, batch=True
        ),
    }


def estimate_phase2(
    db_path: str | None = None,
    limit: int = 0,
    file_type: str | None = None,
    attachment_ids: list[int] | None = None,
    chars_per_token: float | None = None,
) -> dict:
    """What run_phase2 would spend on the rows it would take now: see estimate_rows.

    The rows are put in Phase 2's order by id alone and then read one at a time: sorting them
    whole would hold every pending text in SQLite's sorter at once."""
    conn = _connect(db_path or str(DEFAULT_DB))
    try:
        query, params = _phase2_candidates(
            file_type, attachment_ids, None, limit, select="SELECT ac.id" + _PHASE2_FROM
        )
        ids = [ac_id for (ac_id,) in conn.execute(query, params)]
    finally:
        conn.close()
    return estimate_summaries(db_path, ids, (), chars_per_token)


def estimate_summaries(
    db_path: str | None,
    ac_ids: Iterable[int],
    full_parts: Iterable[int] = (),
    chars_per_token: float | None = None,
) -> dict:
    """What summarising these content rows again would spend, whatever their status now:
    see estimate_rows. `full_parts` are attachment ids to count as flagged though they are
    not flagged yet. Reads one stored text at a time."""
    conn = _connect(db_path or str(DEFAULT_DB))
    try:
        flagged = full_parts_ids(conn) | set(full_parts)

        def rows():
            for ac_id in ac_ids:
                r = conn.execute(_PHASE2_SELECT + " WHERE ac.id = ?", (ac_id,)).fetchone()
                if r is not None and r[2]:
                    yield _Phase2Row._make((*r, r[1] in flagged))

        return estimate_rows(rows(), chars_per_token)
    finally:
        conn.close()


def _sha256_to_message_id(sha256: str) -> int:
    """A stable negative message_id from a content hash.

    Negative so it cannot collide with a real mail message id. Documents are identified by
    their bytes, so the same file ingested twice collides and is skipped.
    """
    return -abs(int(sha256[:15], 16))


def _file_to_message_id(file_path: str) -> int:
    """Generate a stable negative message_id from file content hash."""
    return _sha256_to_message_id(sha256_of_file(Path(file_path)))


def _guess_mime_type(file_path: str) -> str:
    """Guess MIME type from file extension."""
    ext = Path(file_path).suffix.lower()
    mime_map = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".html": "text/html",
        ".htm": "text/html",
        # Images too: recorded as octet-stream, an ingested image was OCRed by extension
        # but never selected by the vision pass (mime_type LIKE 'image/%').
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".jfif": "image/jpeg",
        ".gif": "image/gif",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".bmp": "image/bmp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".webp": "image/webp",
    }
    return mime_map.get(ext, "application/octet-stream")


def ingest_document(
    file_path: str,
    db_path: str | None = None,
    source: str | None = None,
    sender_name: str | None = None,
    source_url: str | None = None,
) -> dict:
    """Ingest a standalone document into the knowledge store.

    Creates a synthetic email entry as anchor, copies the file to the
    attachments directory, and returns IDs for pipeline processing.

    Args:
        file_path: Absolute path to the document.
        db_path: Database path (defaults to DEFAULT_DB).
        source: Optional source label (e.g. "Revolut"). Defaults to filename.
        sender_name: Sender for synthetic email (default "External Document").
        source_url: Original URL if fetched from the web.

    Returns:
        Dict with email_id, attachment_id, message_id, or 'skipped' if duplicate.
    """
    db_path = db_path or str(DEFAULT_DB)
    file_path = str(Path(file_path).resolve())
    filename = Path(file_path).name

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    sha256 = sha256_of_file(Path(file_path))
    message_id = _sha256_to_message_id(sha256)
    mime_type = _guess_mime_type(file_path)
    file_size = os.path.getsize(file_path)
    file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat()
    now = datetime.now().isoformat()

    label = source or Path(file_path).stem.replace("_", " ").replace("-", " ").title()

    # Parse frontmatter from markdown files for richer metadata
    if Path(file_path).suffix.lower() == ".md" and not source:
        try:
            from src.extract.web_ingest import parse_frontmatter

            with open(file_path, encoding="utf-8") as f:
                meta, _ = parse_frontmatter(f.read())
            if meta.get("title"):
                label = meta["title"]
            if meta.get("source") and not source_url:
                source_url = meta["source"]
        except Exception:
            pass  # Fall back to filename-based label

    sender = sender_name or "External Document"
    content_text = f"Ingested document: {filename}"
    if source_url:
        content_text += f"\nSource: {source_url}"

    conn = _connect(db_path)

    # Idempotency: skip if this file was already ingested. The path we already
    # hold goes back with it, because a caller that deletes its source on a skip
    # has to know whether the row it collided with points at that very file.
    existing = conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()
    if existing:
        held = conn.execute(
            "SELECT file_path FROM attachments WHERE message_id = ? AND file_path IS NOT NULL",
            (message_id,),
        ).fetchone()
        conn.close()
        return {
            "skipped": True,
            "message_id": message_id,
            "reason": "already ingested",
            "file_path": held[0]
            if held
            else str(ATTACHMENTS_DIR / str(abs(message_id)) / filename),
        }

    # Create synthetic email entry, with its class (src/store/source_class.py)
    source_class = stored_class(conn, "External", "external@documents.local", f"[Document] {label}")
    conn.execute(
        """INSERT INTO emails
           (message_id, date_received, sender_name, sender_address,
            subject, mailbox_name, content, source_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            file_mtime,
            sender,
            "external@documents.local",
            f"[Document] {label}",
            "External",
            content_text,
            source_class,
        ),
    )
    email_id = conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[
        0
    ]

    # Copy file to attachments directory.
    #
    # NOTE the abs(): message_id is NEGATIVE for reverse-ingested documents, but
    # the directory drops the sign, so this directory's NAME can never be found
    # in emails.message_id. That is harmless, because the attachments row written
    # just below carries the real (negative) id and an absolute file_path, and
    # file_path is what the pipeline resolves. It is however extremely misleading
    # to anyone auditing the attachments tree by directory name: doing so counted
    # 1,468 perfectly healthy document directories as leaked, a 44% over-report.
    # If you are looking for unregistered attachments, ask whether an attachments
    # row REFERENCES the directory, never whether its name is a known message_id.
    dest_dir = ATTACHMENTS_DIR / str(abs(message_id))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename
    # A file already sitting at its own destination is registered where it lies.
    # copy2 raises SameFileError on that, and on the VPS it is not hypothetical:
    # data/attachments is a symlink to /mnt/data, the directory is named for the
    # hash of the file's own content, so anything an earlier reverse-ingest put
    # there hashes straight back to itself. samefile() rather than == because
    # those two paths spell the same inode differently.
    if not (dest_path.exists() and os.path.samefile(file_path, dest_path)):
        shutil.copy2(file_path, dest_path)

    # Create attachment record
    conn.execute(
        """INSERT INTO attachments
           (email_id, message_id, filename, mime_type, file_size, file_path, is_inline,
            exported_at, sha256)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (email_id, message_id, filename, mime_type, file_size, str(dest_path), now, sha256),
    )
    attachment_id = conn.execute(
        "SELECT id FROM attachments WHERE email_id = ? AND filename = ?",
        (email_id, filename),
    ).fetchone()[0]

    conn.commit()
    conn.close()

    return {
        "skipped": False,
        "email_id": email_id,
        "attachment_id": attachment_id,
        "message_id": message_id,
        "filename": filename,
        "file_path": str(dest_path),
    }


def ingest_text_document(
    conn: sqlite3.Connection,
    *,
    source: str,
    key: str,
    filename: str,
    mime_type: str,
    text: str | None,
    sha256: str,
    method: str | None,
    status: str,
    error: str | None,
    subject: str,
    sender_name: str,
    date: str,
) -> dict:
    """Store a document that keeps no file: its email anchor, attachment row and content row.

    The file a SharePoint link or a session note came from is not kept, so Phase 1's work is
    done by the caller and the content row is complete from the start. Phase 1 selects only
    attachments without a content row and never sees this document; Phase 2 selects extracted
    rows still pending and summarises it.

    The message id is the hash of the source bytes, as for ingest_document: the same file
    ingested twice collides and the second is skipped. The three rows are written in one
    transaction.
    """
    message_id = _sha256_to_message_id(sha256)
    existing = conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()
    if existing:
        return {"skipped": True, "message_id": message_id, "email_id": existing[0]}
    now = datetime.now().isoformat()
    # Its class (src/store/source_class.py): a session note or a document
    source_class = stored_class(conn, "External", f"{source}@documents.local", subject)
    with conn:
        email_id = conn.execute(
            """INSERT INTO emails
               (message_id, date_received, sender_name, sender_address, subject,
                mailbox_name, content, source_class)
               VALUES (?, ?, ?, ?, ?, 'External', ?, ?)""",
            (
                message_id,
                date,
                sender_name,
                f"{source}@documents.local",
                subject,
                f"Ingested document: {filename}\nSource: {source}",
                source_class,
            ),
        ).lastrowid
        attachment_id = conn.execute(
            """INSERT INTO attachments
               (email_id, message_id, filename, mime_type, file_size, file_path, is_inline,
                exported_at, sha256)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                email_id,
                message_id,
                filename,
                mime_type,
                len((text or "").encode("utf-8")),
                f"text:{source}:{key}",
                now,
                sha256,
            ),
        ).lastrowid
        conn.execute(
            """INSERT INTO attachment_content
               (attachment_id, extracted_text, extraction_method, extraction_status,
                extraction_error, extracted_at, llm_status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (attachment_id, redact_secrets(text) if text else text, method, status, error, now),
        )
    return {
        "skipped": False,
        "message_id": message_id,
        "email_id": email_id,
        "attachment_id": attachment_id,
    }
