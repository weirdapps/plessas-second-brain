"""Normalization functions for topics and people deduplication.

Ensures consistent naming and prevents duplicate entries for topics and people.
"""

import re
import sqlite3
import unicodedata


def normalize_topic(name: str) -> str:
    """Normalize a topic name for deduplication.

    Rules:
    - Lowercase
    - Strip accents (Greek tonos, Latin diacritics)
    - Unify separators (hyphen, underscore, slash, dot) to spaces
    - Collapse whitespace to single spaces

    This canonicalizes separator/case/accent variants so they no longer fragment
    into distinct topics — the historical cause of ~53% single-use topic rows.

    Args:
        name: Original topic name

    Returns:
        Normalized topic name

    Examples:
        >>> normalize_topic("Cards Migration")
        'cards migration'
        >>> normalize_topic("Cards-Migration")
        'cards migration'
        >>> normalize_topic("cards_migration")
        'cards migration'
    """
    normalized = name.strip().lower()
    # Strip accents (é -> e, ή -> η)
    normalized = unicodedata.normalize("NFD", normalized)
    normalized = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    normalized = unicodedata.normalize("NFC", normalized)
    # Unify separators, then collapse whitespace
    normalized = re.sub(r"[-_/.]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized


def find_or_create_topic(conn: sqlite3.Connection, name: str, parent_id: int | None = None) -> int:
    """Find existing topic by normalized name or create new one.

    Args:
        conn: Database connection
        name: Topic name (will be normalized)
        parent_id: Optional parent topic ID for hierarchy

    Returns:
        Topic ID (existing or newly created)
    """
    normalized = normalize_topic(name)

    # Try to find existing topic
    cursor = conn.execute("SELECT id FROM topics WHERE name = ?", (normalized,))
    row = cursor.fetchone()

    if row:
        return row[0]

    # Create new topic
    cursor = conn.execute(
        "INSERT INTO topics (name, display_name, parent_id) VALUES (?, ?, ?)",
        (normalized, name, parent_id),
    )
    assert cursor.lastrowid is not None  # mypy: INSERT always sets it
    return cursor.lastrowid


def find_or_create_person(conn: sqlite3.Connection, name: str, email: str | None = None) -> int:
    """Find existing person by email or create new one.

    Email is the primary key for deduplication. If email is provided and matches
    an existing person, that person is returned (even if name differs slightly).
    If no email is provided, searches by exact name match.

    Args:
        conn: Database connection
        name: Person's name
        email: Person's email address (optional, but recommended for dedup)

    Returns:
        Person ID (existing or newly created)
    """
    # If email provided, use it as primary deduplication key
    if email:
        email_normalized = email.strip().lower()

        cursor = conn.execute("SELECT id FROM people WHERE LOWER(email) = ?", (email_normalized,))
        row = cursor.fetchone()

        if row:
            # Person exists, optionally update name if it's more complete
            person_id = row[0]
            # Update name if new name is longer (likely more complete)
            cursor = conn.execute("SELECT name FROM people WHERE id = ?", (person_id,))
            existing_name = cursor.fetchone()[0]
            if len(name) > len(existing_name):
                conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
            return person_id

    # No email provided, or email not found - search by name
    cursor = conn.execute("SELECT id FROM people WHERE name = ?", (name,))
    row = cursor.fetchone()

    if row:
        return row[0]

    # Create new person
    cursor = conn.execute(
        "INSERT INTO people (name, email) VALUES (?, ?)",
        (name, email.strip().lower() if email else None),
    )
    assert cursor.lastrowid is not None  # mypy: INSERT always sets it
    return cursor.lastrowid


def merge_people(conn: sqlite3.Connection, keep_id: int, merge_id: int) -> None:
    """Merge two person records, keeping one and updating all references.

    All email_people entries pointing to merge_id will be updated to point to keep_id.
    Then merge_id person record will be deleted.

    Args:
        conn: Database connection
        keep_id: Person ID to keep
        merge_id: Person ID to merge and delete
    """
    # Update all references in email_people (ignore if duplicate primary key)
    conn.execute(
        """
        UPDATE OR IGNORE email_people
        SET person_id = ?
        WHERE person_id = ?
        """,
        (keep_id, merge_id),
    )

    # Remove any remaining references that couldn't be updated (duplicates)
    conn.execute("DELETE FROM email_people WHERE person_id = ?", (merge_id,))

    # Delete the merged person
    conn.execute("DELETE FROM people WHERE id = ?", (merge_id,))

    conn.commit()
