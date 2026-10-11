"""Load extracted email data into SQLite database.

Reads JSON files from data/extracted/ and data/staging/, normalizes entities,
and inserts into the knowledge store with proper deduplication.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from src.export.state import load_json_or_quarantine
from src.extract.extraction_files import read_extraction
from src.extract.parser import _as_string_list
from src.redact import redact_secrets

from .email_html import save_html, split_body
from .normalizer import find_or_create_person, find_or_create_topic, normalize_topic
from .schema import normalize_subject
from .source_class import class_insert


def load_extractions(db_path: str, extracted_dir: str, staging_dir: str) -> int:
    """Load all extracted emails from JSON files into the database.

    Reads batch files from staging_dir to get email metadata, then loads
    corresponding extraction files from extracted_dir. Uses transactions
    for batch commits (every 100 emails).

    Args:
        db_path: Path to SQLite database
        extracted_dir: Directory containing extracted JSON files (see extraction_files.py)
        staging_dir: Directory containing staged batch files (batch-NNNNN.json)

    Returns:
        Number of emails successfully loaded

    Raises:
        FileNotFoundError: If directories don't exist
        json.JSONDecodeError: If JSON files are malformed
    """
    extracted_path = Path(extracted_dir)
    staging_path = Path(staging_dir)

    if not extracted_path.exists():
        raise FileNotFoundError(f"Extracted directory not found: {extracted_dir}")
    if not staging_path.exists():
        raise FileNotFoundError(f"Staging directory not found: {staging_dir}")

    # Open database connection, at the schema this code writes (email_html, v23)
    from .schema import get_connection, run_migrations

    conn = get_connection(db_path)
    run_migrations(conn)

    # Build index of staged emails by message_id.
    # Also remember which message_ids each batch contains, so we can prune
    # fully-loaded batch files at the end (no extra IO — same pass).
    staging_index: dict[str, dict] = {}
    batch_to_msgids: dict[Path, set] = {}
    staging_im: dict[str, str] = {}  # message_id -> RFC822 internet_message_id
    for batch_file in sorted(staging_path.glob("batch-*.json")):
        batch = load_json_or_quarantine(batch_file)
        if batch is None:
            continue
        emails = batch.get("emails", batch) if isinstance(batch, dict) else batch
        msgids = set()
        for email in emails:
            msgid = str(email["message_id"])
            staging_index[msgid] = email
            staging_im[msgid] = email.get("internet_message_id") or ""
            msgids.add(msgid)
        batch_to_msgids[batch_file] = msgids

    loaded_count = 0
    batch_count = 0
    unextracted: list[dict] = []

    for message_id, metadata in staging_index.items():
        # By the exact id, never the lowercased one: two ids can differ in case
        # alone, and matching them folded handed one email its twin's extraction
        # (see src/extract/extraction_files.py).
        extraction = read_extraction(extracted_path, message_id)
        if extraction is None:
            unextracted.append(metadata)  # staged but not yet extracted
            continue

        # Load into database
        if load_single_email(conn, metadata, extraction):
            loaded_count += 1
            batch_count += 1

            # Commit every 100 emails
            if batch_count >= 100:
                conn.commit()
                batch_count = 0

    # A copy of a stored email gets no extraction (src/extract/local.py skips it),
    # so its move and its alias are noted from the staged batch alone. After the
    # loads above, so a copy staged beside its original finds it stored.
    for metadata in unextracted:
        _note_stored(conn, metadata)

    # Final commit, whatever loaded: a stored email's move to another folder is
    # written too, and whatever transaction a write opened is closed.
    conn.commit()

    # Prune fully-resolved batch files — keeps staging dir from growing
    # forever. A staged email is "resolved" when it's already represented in
    # the emails table, either by its source message_id OR by its RFC822
    # internet_message_id (a cross-source duplicate captured via another
    # export path under a different message_id — which load_single_email
    # correctly declines to re-insert, but whose batch must still drain).
    db_msgids = {str(row[0]) for row in conn.execute("SELECT message_id FROM emails")}
    db_imids = {
        str(row[0])
        for row in conn.execute(
            "SELECT internet_message_id FROM emails "
            "WHERE internet_message_id IS NOT NULL AND internet_message_id != ''"
        )
    }
    resolved = set(db_msgids)
    for msgid, im in staging_im.items():
        if im and im in db_imids:
            resolved.add(msgid)
    pruned, freed = _prune_loaded_batches(batch_to_msgids, resolved)
    if pruned > 0:
        print(f"  Pruned {pruned} fully-loaded batch files ({freed / 1024 / 1024:.1f} MB freed)")

    conn.close()
    return loaded_count


def _prune_loaded_batches(
    batch_to_msgids: dict[Path, set],
    db_msgids: set,
) -> tuple[int, int]:
    """Delete batch files whose message_ids are ALL present in the DB.

    Args:
        batch_to_msgids: {Path: set of message_ids in that batch}
        db_msgids: set of message_ids currently in the emails table

    Returns:
        (count_pruned, bytes_freed)
    """
    pruned_count = 0
    pruned_bytes = 0
    for batch_file, msgids in batch_to_msgids.items():
        if not msgids:
            continue  # empty batch — skip rather than risk false-positive prune
        if msgids.issubset(db_msgids):
            try:
                pruned_bytes += batch_file.stat().st_size
                batch_file.unlink()
                pruned_count += 1
            except OSError:
                # Race with another process or file already gone — non-fatal.
                pass
    return pruned_count, pruned_bytes


def prune_staged_batches(db_path: str, staging_dir: str) -> tuple[int, int]:
    """One-shot prune of historical staging directory.

    Used by the `prune-staged` CLI command for backfill. Reads every batch
    once to build the {batch: msgids} map, queries DB for loaded msgids,
    deletes anything fully covered.

    Returns:
        (count_pruned, bytes_freed)
    """
    from .schema import get_connection

    staging_path = Path(staging_dir)
    if not staging_path.exists():
        return 0, 0

    batch_to_msgids: dict[Path, set] = {}
    for batch_file in sorted(staging_path.glob("batch-*.json")):
        try:
            with open(batch_file, encoding="utf-8") as f:
                batch = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        emails = batch.get("emails", batch) if isinstance(batch, dict) else batch
        if not emails:
            continue
        batch_to_msgids[batch_file] = {str(e["message_id"]) for e in emails if "message_id" in e}

    conn = get_connection(db_path)
    db_msgids = {str(row[0]) for row in conn.execute("SELECT message_id FROM emails")}
    conn.close()

    return _prune_loaded_batches(batch_to_msgids, db_msgids)


def _take_the_write_lock(conn: sqlite3.Connection) -> bool:
    """Take the write lock before a loader writes, unless its transaction has it;
    True when this call took it, and the loader then checks again under it. The
    sync units overlap (the noon catch-up and the hourly one), and each found an
    item not stored, stored it, and failed on the other's copy of its unique key,
    which ended that sync. The second now waits for the first (busy_timeout) and
    finds the item stored; an item already stored, in its folder, waits for no one."""
    if conn.in_transaction:
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


def _moved(stored: str | None, staged: str | None) -> bool:
    """Whether a staged copy moves a stored email to another folder. Never into the
    Inbox: its export takes new arrivals, bar a bootstrap, so a staged Inbox copy of
    a stored email is almost always an old one (a batch stays staged while any
    email in it is unextracted), and a move out of the Inbox is inbox_reconcile's
    to record. The cost: an email moved back into the Inbox and staged again by a
    bootstrap keeps the folder it had."""
    return bool(staged) and staged != stored and (staged != "Inbox" or not stored)


def record_alias(conn: sqlite3.Connection, alias: str, email_id: int) -> bool:
    """Note that the stored email `email_id` also went by the Graph id `alias`;
    whether it was new.

    A move to another folder mints a new id, and the attachment registrar resolves
    a directory named by it through this (src/export/outlook_attachments.py).
    Written once: a pass over copies already noted waits for no writer.
    """
    known = conn.execute(
        "SELECT 1 FROM email_aliases WHERE message_id = ?", (str(alias),)
    ).fetchone()
    if known:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO email_aliases (message_id, email_id, recorded_at)"
        " VALUES (?, ?, datetime('now'))",
        (str(alias), email_id),
    )
    return cur.rowcount > 0


def _note_stored(conn: sqlite3.Connection, metadata: dict) -> bool:
    """Whether the store holds this staged email already, noting what the copy says.

    By its own message_id, or by its RFC822 Message-ID under another one: the same
    message from another source (AppleScript vs outlook-cli), or a copy the Archive
    export staged under the new id a move gave it, which is recorded as an alias.
    A copy in another folder records the move (e.g. Inbox to Archive by
    /triage-inbox), so the store does not go stale on the original folder.
    """
    message_id = metadata["message_id"]
    internet_message_id = metadata.get("internet_message_id") or None
    new_mailbox = metadata.get("mailbox_name") or metadata.get("mailbox")

    row = conn.execute(
        "SELECT id, mailbox_name FROM emails WHERE message_id = ?", (message_id,)
    ).fetchone()
    if row is None and internet_message_id:
        row = conn.execute(
            "SELECT id, mailbox_name FROM emails WHERE internet_message_id = ?",
            (internet_message_id,),
        ).fetchone()
        if row is not None:
            record_alias(conn, message_id, row[0])
    if row is None:
        return False
    if _moved(row[1], new_mailbox):
        conn.execute(
            "UPDATE emails SET mailbox_name = ? WHERE id = ?",
            (new_mailbox, row[0]),
        )
    return True


def load_single_email(conn: sqlite3.Connection, metadata: dict, extraction: dict) -> bool:
    """Load a single email with its extraction into the database.

    Args:
        conn: Database connection
        metadata: Email metadata from staging (message_id, date_received, sender, etc.)
        extraction: Extracted data from LLM (summary, topics, decisions, etc.)

    Returns:
        True if loaded successfully, False if duplicate (message_id exists)
    """
    message_id = metadata["message_id"]
    internet_message_id = metadata.get("internet_message_id") or None

    # A duplicate: its move and alias are noted (see _note_stored).
    if _note_stored(conn, metadata):
        return False

    # Not stored: about to write, so the checks again under the write lock.
    if _take_the_write_lock(conn):
        return load_single_email(conn, metadata, extraction)

    # Extract sender info
    sender_name = metadata.get("sender", {}).get("name")
    sender_address = metadata.get("sender", {}).get("address")

    # Compute conversation_id.
    # Primary: Outlook ConversationId (authoritative — set by the Outlook server).
    # Fallback: references/in_reply_to/subject normalization (handles Greek
    # Re:/Fwd:/Απ: when ConversationId is missing — rare for Outlook, common for
    # legacy Apple Mail exports).
    in_reply_to = metadata.get("in_reply_to", "")
    references = metadata.get("references", "")
    outlook_conv_id = metadata.get("conversation_id", "")
    if outlook_conv_id:
        conversation_id = outlook_conv_id
    elif references:
        root_msg_id = references.strip().split()[0]
        conversation_id = hashlib.sha256(root_msg_id.encode()).hexdigest()[:16]
    elif in_reply_to:
        conversation_id = hashlib.sha256(in_reply_to.strip().encode()).hexdigest()[:16]
    else:
        normalized = normalize_subject(metadata.get("subject", ""))
        conversation_id = hashlib.sha256(normalized.encode()).hexdigest()[:16]

    # The body as the text a reader sees; an HTML body is kept beside it.
    body, html = split_body(metadata.get("content"))

    # What the row is (src/store/source_class.py), stored with it so no read has to work it out.
    mailbox = metadata.get("mailbox_name", metadata.get("mailbox"))
    class_column, class_slot, class_value = class_insert(
        conn, mailbox, sender_address, metadata.get("subject"), _header_addresses(metadata)
    )

    # Insert email record
    cursor = conn.execute(
        f"""
        INSERT INTO emails (
            message_id, internet_message_id, date_received, sender_name, sender_address,
            subject, summary, sentiment, urgency, language, mailbox_name, content,
            in_reply_to, "references", conversation_id{class_column}
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{class_slot})
        """,
        (
            message_id,
            internet_message_id,
            metadata.get("date_received"),
            sender_name,
            sender_address,
            metadata.get("subject"),
            extraction.get("summary"),
            extraction.get("sentiment"),
            extraction.get("urgency"),
            extraction.get("language"),
            mailbox,
            body,
            in_reply_to or None,
            references or None,
            conversation_id,
            *class_value,
        ),
    )
    email_id = cursor.lastrowid
    assert email_id is not None  # mypy: INSERT always sets it
    if html is not None and email_id is not None:
        save_html(conn, email_id, html)

    _write_extraction(conn, email_id, metadata, extraction)

    # Also add sender as a person. The header names below are the only ones that
    # may rename a person found by address (display_name=True): the model's
    # people_roles names above never do.
    if sender_name and sender_address:
        sender_id = find_or_create_person(conn, sender_name, sender_address, display_name=True)
        conn.execute(
            "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
            (email_id, sender_id, "sender"),
        )

    # Add all recipients
    for recipient in metadata.get("to_recipients", metadata.get("to", [])):
        if recipient.get("address"):
            recipient_id = find_or_create_person(
                conn,
                recipient.get("name") or recipient["address"],
                recipient["address"],
                display_name=True,
            )
            conn.execute(
                "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
                (email_id, recipient_id, "recipient"),
            )

    for cc_recipient in metadata.get("cc_recipients", metadata.get("cc", [])):
        if cc_recipient.get("address"):
            cc_id = find_or_create_person(
                conn,
                cc_recipient.get("name") or cc_recipient["address"],
                cc_recipient["address"],
                display_name=True,
            )
            conn.execute(
                "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
                (email_id, cc_id, "cc"),
            )

    # Forward hook: Stage 1 image classification for inline images
    try:
        from src.extract.image_pipeline import (
            compute_position_in_body,
            process_single_image,
        )

        image_attachments = conn.execute(
            """SELECT id, file_path, filename, message_id
               FROM attachments
               WHERE email_id = ? AND mime_type LIKE 'image/%' AND file_path IS NOT NULL""",
            (email_id,),
        ).fetchall()

        for att in image_attachments:
            img_path = Path(att[1]) if att[1] else None
            if img_path and img_path.exists():
                position = compute_position_in_body(metadata.get("content"), att[2])
                process_single_image(
                    conn=conn,
                    attachment_id=att[0],
                    img_path=img_path,
                    sender_email=metadata.get("sender", {}).get("address", ""),
                    message_id=str(att[3]),
                    position_in_body=position,
                    run_vision=False,
                )
    except Exception as e:
        import logging

        logging.getLogger(__name__).debug("Image hook: %s", e)

    return True


def _write_extraction(
    conn: sqlite3.Connection, email_id: int, metadata: dict, extraction: dict
) -> None:
    """Write the rows an extraction gives a stored email: its topics, decisions,
    action items, commitments, the people the model named with their roles, and
    its key facts. The header people (sender, recipients) are the caller's.

    The texts of decisions, action items, commitments and key facts are masked
    (src/redact.py) as they are stored: the model may copy a card number, an IBAN
    or a password out of text read before the masking, or despite its prompt."""
    # Load topics
    for topic_name in extraction.get("topics", []):
        topic_id = find_or_create_topic(conn, topic_name)
        conn.execute(
            "INSERT OR IGNORE INTO email_topics (email_id, topic_id) VALUES (?, ?)",
            (email_id, topic_id),
        )

    # Load decisions
    for decision in extraction.get("decisions", []):
        if isinstance(decision, dict):
            decision_text = decision.get("decision")
            if not decision_text:
                continue
            decided_by = decision.get("decided_by")
            if isinstance(decided_by, list):
                decided_by = ", ".join(str(d) for d in decided_by)
            decision_date = decision.get("decision_date")
            if isinstance(decision_date, list):
                decision_date = decision_date[0] if decision_date else None
            conn.execute(
                """
                INSERT INTO decisions (email_id, decision, decided_by, decision_date)
                VALUES (?, ?, ?, ?)
                """,
                (email_id, redact_secrets(decision_text), decided_by, decision_date),
            )
        elif decision:
            conn.execute(
                "INSERT INTO decisions (email_id, decision) VALUES (?, ?)",
                (email_id, redact_secrets(decision)),
            )

    # Load action items
    for action in extraction.get("action_items", []):
        if isinstance(action, dict):
            task_text = action.get("task")
            if not task_text:
                continue
            owner = action.get("owner")
            if isinstance(owner, list):
                owner = ", ".join(str(o) for o in owner)
            deadline = action.get("deadline")
            if isinstance(deadline, list):
                deadline = deadline[0] if deadline else None
            status = action.get("status", "open")
            if isinstance(status, list):
                status = status[0] if status else "open"
            conn.execute(
                """
                INSERT INTO action_items (email_id, task, owner, deadline, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (email_id, redact_secrets(task_text), owner, deadline, status),
            )
        elif action:
            conn.execute(
                "INSERT INTO action_items (email_id, task) VALUES (?, ?)",
                (email_id, redact_secrets(action)),
            )

    # Load commitments (extracted by the LLM; previously parsed then dropped)
    for commitment in extraction.get("commitments", []):
        if isinstance(commitment, dict):
            commitment_text = commitment.get("commitment")
            if not commitment_text:
                continue
            by_person = commitment.get("by")
            if isinstance(by_person, list):
                by_person = ", ".join(str(b) for b in by_person)
            to_person = commitment.get("to")
            if isinstance(to_person, list):
                to_person = ", ".join(str(t) for t in to_person)
            conn.execute(
                """
                INSERT INTO commitments (email_id, commitment, by_person, to_person)
                VALUES (?, ?, ?, ?)
                """,
                (email_id, redact_secrets(commitment_text), by_person, to_person),
            )
        elif commitment:
            conn.execute(
                "INSERT INTO commitments (email_id, commitment) VALUES (?, ?)",
                (email_id, redact_secrets(commitment)),
            )

    # Load people and their roles
    people_roles = extraction.get("people_roles", {})
    if isinstance(people_roles, dict):
        for person_name, roles in people_roles.items():
            # Try to get email from recipients
            person_email = _find_person_email(person_name, metadata)
            person_id = find_or_create_person(conn, person_name, person_email)

            # Add roles (could be string or list)
            if isinstance(roles, str):
                roles = [roles]

            for role in roles:
                if isinstance(role, dict):
                    role = role.get("role", str(role))
                if not isinstance(role, str):
                    role = str(role)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email)
                    VALUES (?, ?, ?)
                    """,
                    (email_id, person_id, role),
                )

    # Load key facts
    for fact in extraction.get("key_facts", []):
        if fact:
            conn.execute(
                "INSERT INTO key_facts (email_id, fact) VALUES (?, ?)",
                (email_id, redact_secrets(fact)),
            )


def _stored_texts(items, key: str | None) -> list:
    """The text _write_extraction stores for each item: a dict's `key`, or the item,
    masked; and the text as it was, which rows stored before the masking hold."""
    texts = []
    for item in items or []:
        text = item.get(key) if isinstance(item, dict) else item
        if text:
            texts.append(text)
            masked = redact_secrets(text)
            if masked != text:
                texts.append(masked)
    return texts


def replace_extraction(
    conn: sqlite3.Connection, email_id: int, metadata: dict, *, wrong: dict, right: dict
) -> None:
    """Give a stored email the extraction `right` in place of `wrong`, the one it holds.

    For emails the loader handed a case twin's extraction (extraction_files.py).
    The rows `wrong` wrote go, matched by their text and topic names, because an
    attachment's decisions, key facts and topics sit on the same email and those
    written before attachment_id existed carry none; an attachment's row with the
    same text or topic goes with them. The model's people go, and so does a person
    it named sender, recipient or cc who has no address: the headers' people always
    have one, and they stay. The email keeps its id, and with it its attachments,
    HTML and thread; its vector is the caller's to drop.
    """
    conn.execute(
        "UPDATE emails SET summary = ?, sentiment = ?, urgency = ?, language = ? WHERE id = ?",
        (
            right.get("summary"),
            right.get("sentiment"),
            right.get("urgency"),
            right.get("language"),
            email_id,
        ),
    )
    for table, column, key, own_rows in (
        ("decisions", "decision", "decision", " AND attachment_id IS NULL"),
        ("action_items", "task", "task", " AND attachment_id IS NULL"),
        ("commitments", "commitment", "commitment", ""),
        ("key_facts", "fact", None, " AND attachment_id IS NULL"),
    ):
        for text in _stored_texts(wrong.get(table), key):
            conn.execute(
                f"DELETE FROM {table} WHERE email_id = ? AND {column} = ?{own_rows}",
                (email_id, text),
            )
    for topic_name in wrong.get("topics") or []:
        conn.execute(
            "DELETE FROM email_topics WHERE email_id = ? AND topic_id IN"
            " (SELECT id FROM topics WHERE name = ?)",
            (email_id, normalize_topic(topic_name)),
        )
    conn.execute(
        "DELETE FROM email_people WHERE email_id = ?"
        " AND (role_in_email NOT IN ('sender', 'recipient', 'cc')"
        " OR person_id IN (SELECT id FROM people WHERE COALESCE(email, '') = ''))",
        (email_id,),
    )
    _write_extraction(conn, email_id, metadata, right)


def stored_email(
    conn: sqlite3.Connection, email_id: int, leave_out: set[str] | frozenset[str] = frozenset()
) -> dict:
    """The email as the loader was given it, rebuilt from the store: what the
    extraction prompt reads, and the header people replace_extraction names from.

    A header person always has an address, so one without is the model's. `leave_out`
    drops the names, lowercased, that must not be offered as recipients
    (scripts/repair_case_twins.py passes those a twin's extraction called recipients).
    """
    row = conn.execute(
        "SELECT message_id, subject, date_received, sender_name, sender_address, content,"
        " mailbox_name FROM emails WHERE id = ?",
        (email_id,),
    ).fetchone()
    people = [
        (role, name, address)
        for role, name, address in conn.execute(
            "SELECT ep.role_in_email, p.name, p.email FROM email_people ep"
            " JOIN people p ON p.id = ep.person_id"
            " WHERE ep.email_id = ? AND ep.role_in_email IN ('recipient', 'cc')"
            " AND COALESCE(p.email, '') != '' ORDER BY ep.rowid",
            (email_id,),
        )
        if (name or "").lower() not in leave_out
    ]
    return {
        "message_id": row[0],
        "subject": row[1],
        "date_received": row[2],
        "sender": {"name": row[3] or "", "address": row[4] or ""},
        "content": row[5],
        "mailbox_name": row[6],
        "to_recipients": [{"name": n, "address": a} for r, n, a in people if r == "recipient"],
        "cc_recipients": [{"name": n, "address": a} for r, n, a in people if r == "cc"],
    }


def load_conversations(db_path: str) -> int:
    """Load extracted conversations from JSON files into the database.

    Reads conversation batch files from staging/conversations/ and
    corresponding extraction files from extracted/conversations/.

    Args:
        db_path: Path to SQLite database

    Returns:
        Number of conversations successfully loaded
    """
    from src.config import DATA_ROOT

    from .schema import get_connection, run_migrations

    staging_dir = DATA_ROOT / "staging" / "conversations"
    extracted_dir = DATA_ROOT / "extracted" / "conversations"

    if not staging_dir.exists() or not extracted_dir.exists():
        return 0

    conn = get_connection(db_path)
    run_migrations(conn)

    extractions = {}
    for extraction_file in sorted(extracted_dir.glob("*.json")):
        with open(extraction_file, encoding="utf-8") as f:
            extractions[extraction_file.stem] = json.load(f)

    # Each extraction loads with the copy of its session it read, where that is
    # still staged, not with the newest copy, which is ahead of it when the
    # session went on between the two; else with the newest. Only those two
    # copies of a session are kept: every batch ever written is still on disk.
    newest: dict[str, dict] = {}
    read: dict[str, dict] = {}
    for batch_file in sorted(staging_dir.glob("conversation-batch-*.json")):
        batch = load_json_or_quarantine(batch_file)
        if batch is None:
            continue
        for conv in batch.get("conversations", []):
            session_id = conv["session_id"]
            newest[session_id] = conv
            extraction = extractions.get(session_id)
            ended_at = str(conv.get("ended_at") or "")
            if extraction is not None and extraction.get("transcript_ended_at") == ended_at:
                read[session_id] = conv

    loaded_count = 0
    for session_id, extraction in extractions.items():
        metadata = read.get(session_id) or newest.get(session_id)
        if metadata is None:
            continue

        if load_single_conversation(conn, metadata, extraction):
            loaded_count += 1

    conn.commit()
    conn.close()
    return loaded_count


def conversation_went_on(metadata: dict, ended_at: str | None, turns: int | None) -> bool:
    """Whether a transcript has gone on past a copy that ended at `ended_at` with
    `turns` turns: it ends later AND holds more turns. A later end alone comes
    from trailing events that add no conversation, and more turns alone would
    follow any change in how the parser counts them. A copy with no end on
    record never counts as overtaken."""
    if not ended_at:
        return False
    count = metadata.get("turn_count") or len(metadata.get("turns", []))
    return (metadata.get("ended_at") or "") > ended_at and count > (turns or 0)


def delete_conversation(conn: sqlite3.Connection, conversation_id: int) -> None:
    """A conversation and everything loaded from it; the triggers update the
    full-text indexes. Its vector goes the next time build_index saves."""
    conn.execute("DELETE FROM conversation_topics WHERE conversation_id = ?", (conversation_id,))
    turns = "SELECT id FROM conversation_turns WHERE conversation_id = ?"
    for table in ("decisions", "action_items", "key_facts"):
        conn.execute(
            f"DELETE FROM {table} WHERE conversation_turn_id IN ({turns})", (conversation_id,)
        )
    conn.execute("DELETE FROM conversation_turns WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def load_single_conversation(
    conn: sqlite3.Connection,
    metadata: dict,
    extraction: dict,
    *,
    replace: bool = False,
) -> bool:
    """Load a single conversation with its extraction into the database.

    Args:
        conn: Database connection
        metadata: Conversation metadata from staging (session_id, turns, etc.)
        extraction: Extracted data from LLM (summary, topics, decisions, etc.)
        replace: Load it again even if it has not gone on since it was loaded
            (the hook that ingests a session while it runs)

    Returns:
        True if loaded, False if the store holds it and it has not gone on since
    """
    session_id = metadata["session_id"]

    existing = conn.execute(
        "SELECT id, ended_at, turn_count FROM conversations WHERE session_id = ?", (session_id,)
    ).fetchone()
    # An extraction names the end and turn count of the transcript it read
    # (run_conversation_extraction). One of a transcript this one went on past
    # waits to be done again, as extraction's own rule has it: loaded, the new
    # turns would carry the old summary, and the new extraction would find the
    # store already holding them. One that names no end, written before it did,
    # may still make a first load.
    described = extraction.get("transcript_ended_at")
    if described is None:
        stale = existing is not None
    elif extraction.get("transcript_turn_count") is None:
        stale = str(described) < str(metadata.get("ended_at") or "")
    else:
        stale = conversation_went_on(metadata, described, extraction["transcript_turn_count"])
    if stale and not replace:
        return False
    if existing and not (replace or conversation_went_on(metadata, existing[1], existing[2])):
        return False
    # About to write: the checks again under the write lock.
    if _take_the_write_lock(conn):
        return load_single_conversation(conn, metadata, extraction, replace=replace)
    new_id = None  # SQLite's next
    if existing:
        delete_conversation(conn, existing[0])
        # Loaded again, whole, under an id above every one in use, read once the
        # delete holds the write lock: SQLite would give the top id to the next
        # row, and build_index embeds only an id it holds no vector for.
        top = conn.execute("SELECT MAX(id) FROM conversations").fetchone()[0]
        new_id = max(top or 0, existing[0]) + 1

    # Insert conversation record
    cursor = conn.execute(
        """
        INSERT INTO conversations (
            id, session_id, started_at, ended_at, workspace, project_name,
            turn_count, summary, topics_summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            new_id,
            session_id,
            metadata.get("started_at", ""),
            metadata.get("ended_at"),
            metadata.get("workspace"),
            metadata.get("project_name"),
            metadata.get("turn_count", len(metadata.get("turns", []))),
            extraction.get("summary"),
            ", ".join(extraction.get("topics") or []),
        ),
    )
    conversation_id = cursor.lastrowid

    # Insert turns
    for i, turn in enumerate(metadata.get("turns", [])):
        cursor = conn.execute(
            """
            INSERT INTO conversation_turns (
                conversation_id, turn_index, timestamp, speaker,
                content, content_length, has_code, has_tool_use
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                i,
                turn.get("timestamp", ""),
                turn["speaker"],
                turn["content"],
                len(turn["content"]),
                1 if turn.get("has_code") else 0,
                1 if turn.get("has_tool_use") else 0,
            ),
        )

    # Link topics (reuse existing topics table)
    for topic_name in extraction.get("topics") or []:
        topic_id = find_or_create_topic(conn, topic_name)
        conn.execute(
            "INSERT OR IGNORE INTO conversation_topics (conversation_id, topic_id) VALUES (?, ?)",
            (conversation_id, topic_id),
        )

    # Load decisions (linked to conversation, not email)
    for decision in extraction.get("decisions") or []:
        if isinstance(decision, dict) and decision.get("decision"):
            decided_by = decision.get("decided_by")
            if isinstance(decided_by, list):
                decided_by = ", ".join(str(d) for d in decided_by)
            conn.execute(
                """
                INSERT INTO decisions (decision, decided_by, conversation_turn_id)
                VALUES (?, ?, (SELECT id FROM conversation_turns
                              WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1))
                """,
                (redact_secrets(decision["decision"]), decided_by, conversation_id),
            )

    # Load action items
    for action in extraction.get("action_items") or []:
        if isinstance(action, dict) and action.get("task"):
            owner = action.get("owner")
            if isinstance(owner, list):
                owner = ", ".join(str(o) for o in owner)
            conn.execute(
                """
                INSERT INTO action_items (task, owner, deadline, status, conversation_turn_id)
                VALUES (?, ?, ?, 'open',
                        (SELECT id FROM conversation_turns
                         WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1))
                """,
                (redact_secrets(action["task"]), owner, action.get("deadline"), conversation_id),
            )

    # Load key facts
    for fact in _as_string_list(extraction.get("key_facts")):
        if fact:
            conn.execute(
                """
                INSERT INTO key_facts (fact, conversation_turn_id)
                VALUES (?, (SELECT id FROM conversation_turns
                           WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1))
                """,
                (redact_secrets(fact), conversation_id),
            )

    # Store preferences and technical decisions as key_facts with prefix
    # Conversation extractions are read from disk without parse_extraction, and
    # files written before it normalised these lists hold dict items (stored as
    # a repr) or nulls (which stopped every conversation load).
    for pref in _as_string_list(extraction.get("preferences_expressed")):
        if pref:
            conn.execute(
                """
                INSERT INTO key_facts (fact, conversation_turn_id)
                VALUES (?, (SELECT id FROM conversation_turns
                           WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1))
                """,
                (redact_secrets(f"[PREFERENCE] {pref}"), conversation_id),
            )

    for tech in _as_string_list(extraction.get("technical_decisions")):
        if tech:
            conn.execute(
                """
                INSERT INTO key_facts (fact, conversation_turn_id)
                VALUES (?, (SELECT id FROM conversation_turns
                           WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1))
                """,
                (redact_secrets(f"[TECHNICAL] {tech}"), conversation_id),
            )

    return True


def _header_addresses(metadata: dict) -> list[str]:
    """The To and Cc addresses the staged email carries."""
    people = (metadata.get("to_recipients", metadata.get("to")) or []) + (
        metadata.get("cc_recipients", metadata.get("cc")) or []
    )
    return [p["address"] for p in people if isinstance(p, dict) and p.get("address")]


def _find_person_email(person_name: str, metadata: dict) -> str | None:
    """Find email address for a person from metadata recipients.

    Args:
        person_name: Name to search for
        metadata: Email metadata with to/cc/sender fields

    Returns:
        Email address if found, None otherwise
    """
    name_lower = person_name.lower()

    # Check sender. A name staged as null is a key that holds None, which
    # .get()'s default does not replace.
    sender = metadata.get("sender", {})
    if (sender.get("name") or "").lower() == name_lower:
        return sender.get("address")

    # Check recipients
    for recipient in metadata.get("to_recipients", metadata.get("to", [])) + metadata.get(
        "cc_recipients", metadata.get("cc", [])
    ):
        if (recipient.get("name") or "").lower() == name_lower:
            return recipient.get("address")

    return None
