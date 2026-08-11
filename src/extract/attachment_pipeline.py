"""Attachment content extraction pipeline.

Phase 1: Local text extraction (free, no API calls).
Phase 2: Vertex AI structured extraction (LLM) — added in Task 4.
Ingest: Import standalone documents (not from email) into the knowledge store.
"""

import hashlib
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from src.config import ATTACHMENTS_DIR, DEFAULT_DB
from src.extract.attachment_extractors import extract_text_from_file
from src.extract.vertex_auth import touch_sentinel

# Processing constants
PHASE1_BATCH_SIZE = 50
PHASE2_BATCH_SIZE = 10
PHASE2_COOLDOWN = 3  # seconds between LLM batches
LLM_MAX_TEXT = 50_000  # max chars sent to LLM


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


def run_phase1(
    db_path: str | None = None,
    limit: int = 0,
    file_type: str | None = None,
    attachment_ids: list[int] | None = None,
) -> dict:
    """Run Phase 1: local text extraction for all unprocessed attachments.

    Args:
        db_path: Path to database (defaults to DEFAULT_DB)
        limit: Max attachments to process (0 = no limit)
        file_type: Filter by type: 'pdf', 'word', 'pptx', 'excel', 'image', 'eml', 'rpmsg'
        attachment_ids: If given, only process these attachment IDs (for sync scoping)

    Returns:
        Dict with processing stats: processed, extracted, failed, skipped.
    """
    db_path = db_path or str(DEFAULT_DB)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")  # see schema.get_connection
    conn.execute("PRAGMA synchronous = NORMAL")
    now = datetime.now().isoformat()

    # Build query with parameterized conditions
    type_condition, type_params = _build_mime_type_conditions(file_type)

    # Build base query
    query = """
        SELECT a.id, a.file_path, a.mime_type, a.filename
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

    stats = {"processed": 0, "extracted": 0, "failed": 0, "skipped": 0}

    for att_id, file_path, mime_type, _filename in rows:
        result = extract_text_from_file(file_path, mime_type or "")

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

        stats["processed"] += 1
        stats[result["status"]] = stats.get(result["status"], 0) + 1

        if stats["processed"] % PHASE1_BATCH_SIZE == 0:
            conn.commit()

    conn.commit()
    conn.close()
    return stats


def _extract_one_attachment(row):
    """Worker: call LLM for a single attachment.

    Returns ``(ac_id, email_id, extraction, error, auth_error)``. On success ``error`` is
    None and ``extraction`` is the parsed dict; on failure the reverse, with ``error``
    serialised for the DB column.

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
    from src.extract.attachment_prompt import build_attachment_prompt
    from src.extract.claude_extract import _get_client_and_model, call_with_policy
    from src.extract.parser import parse_extraction
    from src.extract.policy_bridge import classify_exception
    from src.llm_policy import Outcome

    ac_id, att_id, text, filename, mime_type, email_id, email_subject, email_date = row

    try:
        prompt = build_attachment_prompt(
            extracted_text=text,
            filename=filename,
            mime_type=mime_type,
            email_subject=email_subject,
            email_date=email_date,
        )

        # _do_call re-fetches the client on every attempt so a successful reauth
        # (which calls reset_client_cache) is picked up by the retry rather than
        # silently reusing the stale credential.
        def _do_call():
            cur_client, cur_model = _get_client_and_model()
            return cur_client.messages.create(
                model=cur_model,
                # Dense documents (large spreadsheets/decks) yield long extraction
                # JSON; 2048 truncated it mid-structure on ~40K-char docs, so every
                # such attachment failed with "Expecting ',' delimiter". Give the
                # structured output room to complete; parse_extraction additionally
                # salvages any residual truncation rather than dropping the summary.
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )

        response = call_with_policy(_do_call, max_call_seconds=120.0)
        # Do not close: this is the shared client from _get_client_and_model.

        raw_text = response.content[0].text
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            raw_text = "\n".join(lines[1:-1])

        extraction = parse_extraction(raw_text)
        return (ac_id, email_id, extraction, None, False)

    except Exception as e:
        auth_error = classify_exception(e, None) is Outcome.AUTH_REAUTH_REQUIRED
        return (ac_id, email_id, None, f"{type(e).__name__}: {str(e)[:500]}", auth_error)


def run_phase2(
    db_path: str | None = None,
    limit: int = 0,
    file_type: str | None = None,
    attachment_ids: list[int] | None = None,
    workers: int = 1,
) -> dict:
    """Run Phase 2: Vertex AI structured extraction on extracted text.

    Processes attachments where extraction_status='extracted' and llm_status='pending'.
    Stores summary in attachment_content, and key_facts/topics/decisions/action_items
    in existing tables linked via email_id.

    Args:
        db_path: Path to database (defaults to DEFAULT_DB)
        limit: Max attachments to process (0 = no limit)
        file_type: Filter by original MIME type
        attachment_ids: If given, only process these attachment IDs (for sync scoping)
        workers: Number of concurrent LLM workers (default 1)

    Returns:
        Dict with processing stats.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from src.store.normalizer import find_or_create_topic

    db_path = db_path or str(DEFAULT_DB)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")  # see schema.get_connection
    conn.execute("PRAGMA synchronous = NORMAL")

    type_condition, type_params = _build_mime_type_conditions(file_type)

    # Build query for Phase 2 candidates
    query = """
        SELECT ac.id, ac.attachment_id, ac.extracted_text,
               a.filename, a.mime_type, a.email_id,
               e.subject, e.date_received
        FROM attachment_content ac
        JOIN attachments a ON a.id = ac.attachment_id
        LEFT JOIN emails e ON e.id = a.email_id
        WHERE ac.extraction_status = 'extracted'
          AND ac.llm_status = 'pending'
    """

    params = list(type_params)

    # Scope to specific attachment IDs if provided
    if attachment_ids:
        placeholders = ",".join("?" * len(attachment_ids))
        query += f"\n        AND ac.attachment_id IN ({placeholders})"
        params.extend(attachment_ids)

    if type_condition:
        query += f"\n        {type_condition}"
    query += "\n        ORDER BY ac.id"
    if limit > 0:
        query += f"\n        LIMIT {int(limit)}"

    rows = conn.execute(query, params).fetchall()

    stats = {"processed": 0, "extracted": 0, "failed": 0}

    def _store_result(ac_id, email_id, extraction, error, auth_error):
        """Store a single result in the DB (called from main thread).

        ``auth_error`` is the worker's verdict, taken from the same classifier the retry
        policy uses. Re-deriving it here from ``error`` — which is a string by the time it
        arrives — is what made a surviving 401 permanent: see _extract_one_attachment.
        """
        now = datetime.now().isoformat()
        if error:
            if auth_error:
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
            conn.execute(
                """UPDATE attachment_content
                   SET summary = ?, language = ?, llm_status = 'extracted',
                       llm_extracted_at = ?
                   WHERE id = ?""",
                (extraction.get("summary"), extraction.get("language"), now, ac_id),
            )

            if email_id:
                for topic_name in extraction.get("topics", []):
                    topic_id = find_or_create_topic(conn, topic_name)
                    conn.execute(
                        "INSERT OR IGNORE INTO email_topics (email_id, topic_id) VALUES (?, ?)",
                        (email_id, topic_id),
                    )

                for decision in extraction.get("decisions", []):
                    if isinstance(decision, dict) and decision.get("decision"):
                        conn.execute(
                            "INSERT INTO decisions (email_id, decision, decided_by) VALUES (?, ?, ?)",
                            (
                                email_id,
                                decision["decision"],
                                decision.get("decided_by"),
                            ),
                        )

                for action in extraction.get("action_items", []):
                    if isinstance(action, dict) and action.get("task"):
                        conn.execute(
                            "INSERT INTO action_items (email_id, task, owner, deadline, status) "
                            "VALUES (?, ?, ?, ?, 'open')",
                            (
                                email_id,
                                action["task"],
                                action.get("owner"),
                                action.get("deadline"),
                            ),
                        )

                for fact in extraction.get("key_facts", []):
                    if fact:
                        conn.execute(
                            "INSERT INTO key_facts (email_id, fact) VALUES (?, ?)",
                            (email_id, fact),
                        )

            stats["extracted"] += 1

        stats["processed"] += 1

        if stats["processed"] % PHASE2_BATCH_SIZE == 0:
            conn.commit()

    if workers <= 1:
        # Sequential mode (original behavior)
        for row in rows:
            ac_id, email_id, extraction, error, auth_error = _extract_one_attachment(row)
            _store_result(ac_id, email_id, extraction, error, auth_error)
            if stats["processed"] % PHASE2_BATCH_SIZE == 0:
                time.sleep(PHASE2_COOLDOWN)
    else:
        # Concurrent mode: LLM calls in parallel, DB writes serialized
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_extract_one_attachment, row): row for row in rows}
            for future in as_completed(futures):
                ac_id, email_id, extraction, error, auth_error = future.result()
                _store_result(ac_id, email_id, extraction, error, auth_error)

    conn.commit()
    conn.close()
    return stats


def _file_to_message_id(file_path: str) -> int:
    """Generate a stable negative message_id from file content hash.

    Uses negative IDs to avoid collision with real Apple Mail message IDs.
    """
    with open(file_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return -abs(int(digest[:15], 16))


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

    message_id = _file_to_message_id(file_path)
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

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode = WAL")  # see schema.get_connection
    conn.execute("PRAGMA synchronous = NORMAL")

    # Idempotency: skip if this file was already ingested
    existing = conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()
    if existing:
        conn.close()
        return {"skipped": True, "message_id": message_id, "reason": "already ingested"}

    # Create synthetic email entry
    conn.execute(
        """INSERT INTO emails
           (message_id, date_received, sender_name, sender_address,
            subject, mailbox_name, content)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            file_mtime,
            sender,
            "external@documents.local",
            f"[Document] {label}",
            "External",
            content_text,
        ),
    )
    email_id = conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[
        0
    ]

    # Copy file to attachments directory
    dest_dir = ATTACHMENTS_DIR / str(abs(message_id))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename
    shutil.copy2(file_path, dest_path)

    # Create attachment record
    conn.execute(
        """INSERT INTO attachments
           (email_id, message_id, filename, mime_type, file_size, file_path, is_inline, exported_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
        (email_id, message_id, filename, mime_type, file_size, str(dest_path), now),
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
    }
