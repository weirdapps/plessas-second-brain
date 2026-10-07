"""Extract again the emails loaded as stubs, and give each its extraction in place.

An email whose extraction failed in EMAIL_MAX_ATTEMPTS runs is loaded without one
(src/extract/local.py logs "GAVE UP"): an empty summary and nothing else, and no
path ever offered it to the model again. On 2026-10-07 the producer held 33 such
emails from September and October, some of them failed on a day the cloud provider
refused every request.

A stub is a mail row (not news, not a document) whose summary is empty. Each is
rebuilt from the store as the extraction prompt reads it (loader.stored_email), sent
through the normal extraction (local.extract_inline), and given the extraction in
place of the stub (loader.replace_extraction), the way scripts/repair_case_twins.py
repairs a crossed one: the row keeps its id, attachments, HTML and thread, and the
extraction file on disk is rewritten. A stub has no vector (the index embeds only
rows with a summary), so the next index build embeds the new summary. A failure
leaves the stub as it was; quota or an expired credential ends the run.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from src.export.state import write_json_atomic
from src.extract import local
from src.extract.extraction_files import extraction_path
from src.redact import redact_payload
from src.store.loader import replace_extraction, stored_email

STUB_SQL = "COALESCE(summary, '') = '' AND COALESCE(mailbox_name, '') NOT IN ('News', 'External')"


def select_stubs(
    conn: sqlite3.Connection, since: str | None = None, limit: int = 0
) -> list[tuple[int, str]]:
    """(email id, message_id) of each stub, newest first."""
    sql = f"SELECT id, message_id FROM emails WHERE {STUB_SQL}"
    params: list = []
    if since:
        sql += " AND date_received >= ?"
        params.append(since)
    sql += " ORDER BY date_received DESC"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [(int(r[0]), str(r[1])) for r in conn.execute(sql, params)]


def retry_stubs(
    conn: sqlite3.Connection,
    extracted_dir: Path,
    engine: str,
    since: str | None = None,
    limit: int = 0,
) -> dict:
    """Extract each stub again; one commit per email, as the producer's syncs write too."""
    stubs = select_stubs(conn, since, limit)
    stats = {"stubs": len(stubs), "replaced": 0, "failed": 0, "stopped": False}
    api_key = os.environ.get("GEMINI_API_KEY")
    for email_id, message_id in stubs:
        email = stored_email(conn, email_id)
        _, extraction, is_quota, _ = local.extract_inline(email, api_key, engine=engine)
        if extraction is None:
            stats["failed"] += 1
            if is_quota or local._shutdown:
                stats["stopped"] = True
                break
            continue
        write_json_atomic(extraction_path(extracted_dir, message_id), extraction)
        # Redacted on the way in, as staging is.
        replace_extraction(
            conn,
            email_id,
            email,
            wrong=local._stub_extraction(message_id),
            right=redact_payload(extraction),
        )
        conn.commit()
        stats["replaced"] += 1
    return stats
