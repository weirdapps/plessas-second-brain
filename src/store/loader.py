"""Load extracted email data into SQLite database.

Reads JSON files from data/extracted/ and data/staging/, normalizes entities,
and inserts into the knowledge store with proper deduplication.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from .normalizer import find_or_create_person, find_or_create_topic
from .schema import normalize_subject


def load_extractions(db_path: str, extracted_dir: str, staging_dir: str) -> int:
    """Load all extracted emails from JSON files into the database.

    Reads batch files from staging_dir to get email metadata, then loads
    corresponding extraction files from extracted_dir. Uses transactions
    for batch commits (every 100 emails).

    Args:
        db_path: Path to SQLite database
        extracted_dir: Directory containing extracted JSON files ({message_id}.json)
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

    # Open database connection
    from .schema import get_connection

    conn = get_connection(db_path)

    # Build index of staged emails by message_id.
    # Also remember which message_ids each batch contains, so we can prune
    # fully-loaded batch files at the end (no extra IO — same pass).
    staging_index: dict[str, dict] = {}
    batch_to_msgids: dict[Path, set] = {}
    staging_im: dict[str, str] = {}  # message_id -> RFC822 internet_message_id
    for batch_file in sorted(staging_path.glob("batch-*.json")):
        with open(batch_file, encoding="utf-8") as f:
            batch = json.load(f)
            emails = batch.get("emails", batch) if isinstance(batch, dict) else batch
            msgids = set()
            for email in emails:
                msgid = str(email["message_id"])
                staging_index[msgid] = email
                staging_im[msgid] = email.get("internet_message_id") or ""
                msgids.add(msgid)
            batch_to_msgids[batch_file] = msgids

    # Index available extraction files by lowercased stem. Outlook message-ids
    # are case-sensitive base64, but macOS/APFS is case-insensitive (and
    # case-preserving), so an extraction written as "{message_id}.json" can
    # land on disk with different letter-case than the staged message_id.
    # Driving the load from the staging entries (the source of truth for the
    # canonical message_id and metadata) and matching extraction files
    # case-insensitively avoids silently dropping those emails.
    extracted_by_lower: dict[str, Path] = {}
    for ef in extracted_path.glob("*.json"):
        extracted_by_lower[ef.stem.lower()] = ef

    loaded_count = 0
    batch_count = 0

    for message_id, metadata in staging_index.items():
        extraction_file = extracted_by_lower.get(message_id.lower())
        if extraction_file is None:
            continue  # staged but not yet extracted

        # Load extraction (metadata carries the canonical, correct-case id)
        with open(extraction_file, encoding="utf-8") as f:
            extraction = json.load(f)

        # Load into database
        if load_single_email(conn, metadata, extraction):
            loaded_count += 1
            batch_count += 1

            # Commit every 100 emails
            if batch_count >= 100:
                conn.commit()
                batch_count = 0

    # Final commit for remaining emails
    if batch_count > 0:
        conn.commit()

    # Prune fully-resolved batch files — keeps staging dir from growing
    # forever. A staged email is "resolved" when it's already represented in
    # the emails table, either by its source message_id OR by its RFC822
    # internet_message_id (a cross-source duplicate captured via another
    # export path under a different message_id — which load_single_email
    # correctly declines to re-insert, but whose batch must still drain).
    # The seen-ids dedup cache (apple_mail.load_seen_ids) is unaffected
    # because it's a separate cache file primed before deletion.
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
    new_mailbox = metadata.get("mailbox_name") or metadata.get("mailbox")

    # Check if already exists by source-specific message_id.
    # If found AND the folder changed (e.g. user moved Inbox→Archive via
    # /triage-inbox), UPDATE the mailbox_name so the DB reflects the
    # current location instead of going stale on the original folder.
    cursor = conn.execute("SELECT id, mailbox_name FROM emails WHERE message_id = ?", (message_id,))
    row = cursor.fetchone()
    if row:
        if new_mailbox and row[1] != new_mailbox:
            conn.execute(
                "UPDATE emails SET mailbox_name = ? WHERE id = ?",
                (new_mailbox, row[0]),
            )
        return False  # Duplicate; mailbox reconciled if needed

    # Cross-source dedup: same RFC822 Message-ID from a different source
    # (AppleScript vs outlook-cli) means we already have this email.
    if internet_message_id:
        cursor = conn.execute(
            "SELECT id, mailbox_name FROM emails WHERE internet_message_id = ?",
            (internet_message_id,),
        )
        row = cursor.fetchone()
        if row:
            if new_mailbox and row[1] != new_mailbox:
                conn.execute(
                    "UPDATE emails SET mailbox_name = ? WHERE id = ?",
                    (new_mailbox, row[0]),
                )
            return False  # Duplicate by RFC822 Message-ID; mailbox reconciled

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

    # Insert email record
    cursor = conn.execute(
        """
        INSERT INTO emails (
            message_id, internet_message_id, date_received, sender_name, sender_address,
            subject, summary, sentiment, urgency, language, mailbox_name, content,
            in_reply_to, "references", conversation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            metadata.get("mailbox_name", metadata.get("mailbox")),
            metadata.get("content"),
            in_reply_to or None,
            references or None,
            conversation_id,
        ),
    )
    email_id = cursor.lastrowid

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
                (email_id, decision_text, decided_by, decision_date),
            )
        elif decision:
            conn.execute(
                "INSERT INTO decisions (email_id, decision) VALUES (?, ?)",
                (email_id, decision),
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
                (email_id, task_text, owner, deadline, status),
            )
        elif action:
            conn.execute(
                "INSERT INTO action_items (email_id, task) VALUES (?, ?)",
                (email_id, action),
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
                (email_id, commitment_text, by_person, to_person),
            )
        elif commitment:
            conn.execute(
                "INSERT INTO commitments (email_id, commitment) VALUES (?, ?)",
                (email_id, commitment),
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

    # Also add sender as a person
    if sender_name and sender_address:
        sender_id = find_or_create_person(conn, sender_name, sender_address)
        conn.execute(
            "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
            (email_id, sender_id, "sender"),
        )

    # Add all recipients
    for recipient in metadata.get("to_recipients", metadata.get("to", [])):
        if recipient.get("address"):
            recipient_id = find_or_create_person(
                conn, recipient.get("name", recipient["address"]), recipient["address"]
            )
            conn.execute(
                "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
                (email_id, recipient_id, "recipient"),
            )

    for cc_recipient in metadata.get("cc_recipients", metadata.get("cc", [])):
        if cc_recipient.get("address"):
            cc_id = find_or_create_person(
                conn,
                cc_recipient.get("name", cc_recipient["address"]),
                cc_recipient["address"],
            )
            conn.execute(
                "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
                (email_id, cc_id, "cc"),
            )

    # Load key facts
    for fact in extraction.get("key_facts", []):
        if fact:
            conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (email_id, fact))

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

    # Build index of staged conversations by session_id
    staging_index = {}
    for batch_file in sorted(staging_dir.glob("conversation-batch-*.json")):
        with open(batch_file, encoding="utf-8") as f:
            batch = json.load(f)
        for conv in batch.get("conversations", []):
            staging_index[conv["session_id"]] = conv

    loaded_count = 0
    for extraction_file in sorted(extracted_dir.glob("*.json")):
        session_id = extraction_file.stem

        if session_id not in staging_index:
            continue

        with open(extraction_file, encoding="utf-8") as f:
            extraction = json.load(f)

        metadata = staging_index[session_id]

        if load_single_conversation(conn, metadata, extraction):
            loaded_count += 1

    conn.commit()
    conn.close()
    return loaded_count


def load_single_conversation(
    conn: sqlite3.Connection,
    metadata: dict,
    extraction: dict,
) -> bool:
    """Load a single conversation with its extraction into the database.

    Args:
        conn: Database connection
        metadata: Conversation metadata from staging (session_id, turns, etc.)
        extraction: Extracted data from LLM (summary, topics, decisions, etc.)

    Returns:
        True if loaded successfully, False if duplicate
    """
    session_id = metadata["session_id"]

    # Check if already exists
    if conn.execute("SELECT id FROM conversations WHERE session_id = ?", (session_id,)).fetchone():
        return False

    # Insert conversation record
    cursor = conn.execute(
        """
        INSERT INTO conversations (
            session_id, started_at, ended_at, workspace, project_name,
            turn_count, summary, topics_summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (
            session_id,
            metadata.get("started_at", ""),
            metadata.get("ended_at"),
            metadata.get("workspace"),
            metadata.get("project_name"),
            metadata.get("turn_count", len(metadata.get("turns", []))),
            extraction.get("summary"),
            ", ".join(extraction.get("topics", [])),
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
    for topic_name in extraction.get("topics", []):
        topic_id = find_or_create_topic(conn, topic_name)
        conn.execute(
            "INSERT OR IGNORE INTO conversation_topics (conversation_id, topic_id) VALUES (?, ?)",
            (conversation_id, topic_id),
        )

    # Load decisions (linked to conversation, not email)
    for decision in extraction.get("decisions", []):
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
                (decision["decision"], decided_by, conversation_id),
            )

    # Load action items
    for action in extraction.get("action_items", []):
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
                (action["task"], owner, action.get("deadline"), conversation_id),
            )

    # Load key facts
    for fact in extraction.get("key_facts", []):
        if fact:
            conn.execute(
                """
                INSERT INTO key_facts (fact, conversation_turn_id)
                VALUES (?, (SELECT id FROM conversation_turns
                           WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1))
                """,
                (fact, conversation_id),
            )

    # Store preferences and technical decisions as key_facts with prefix
    for pref in extraction.get("preferences_expressed", []):
        if pref:
            conn.execute(
                """
                INSERT INTO key_facts (fact, conversation_turn_id)
                VALUES (?, (SELECT id FROM conversation_turns
                           WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1))
                """,
                (f"[PREFERENCE] {pref}", conversation_id),
            )

    for tech in extraction.get("technical_decisions", []):
        if tech:
            conn.execute(
                """
                INSERT INTO key_facts (fact, conversation_turn_id)
                VALUES (?, (SELECT id FROM conversation_turns
                           WHERE conversation_id = ? ORDER BY turn_index DESC LIMIT 1))
                """,
                (f"[TECHNICAL] {tech}", conversation_id),
            )

    return True


def _find_person_email(person_name: str, metadata: dict) -> str | None:
    """Find email address for a person from metadata recipients.

    Args:
        person_name: Name to search for
        metadata: Email metadata with to/cc/sender fields

    Returns:
        Email address if found, None otherwise
    """
    name_lower = person_name.lower()

    # Check sender
    sender = metadata.get("sender", {})
    if sender.get("name", "").lower() == name_lower:
        return sender.get("address")

    # Check recipients
    for recipient in metadata.get("to_recipients", metadata.get("to", [])) + metadata.get(
        "cc_recipients", metadata.get("cc", [])
    ):
        if recipient.get("name", "").lower() == name_lower:
            return recipient.get("address")

    return None
