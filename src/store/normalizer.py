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


def _is_greek(ch: str) -> bool:
    return "\u0370" <= ch <= "\u03ff"


def recover_garbled_greek(name: str) -> str | None:
    """The Greek a garbled name stands for, or None when it is not one.

    Seven people rows, the owner's among them, carried names such as 'Ķ°ĶŦĶĨ...':
    Greek written out as GBK, whose row A6 holds the Greek alphabet, and read
    back as ISO-8859-10, so every Greek letter became 'Ķ' (byte A6) and a second
    letter. Encoding it back and decoding it as GBK returns the Greek exactly, and
    only Greek and ASCII come back: anything else means these bytes never were
    Greek, as in a Latvian name that merely holds a 'Ķ'.
    """
    if not name or any(_is_greek(ch) for ch in name):
        return None
    try:
        recovered = name.encode("iso8859_10").decode("gbk")
    except UnicodeError:
        return None
    if not any(_is_greek(ch) for ch in recovered):
        return None
    if not all(ch.isascii() or _is_greek(ch) for ch in recovered):
        return None
    return recovered


def looks_garbled(name: str) -> bool:
    """Whether a name is mojibake rather than a name, recoverable or not.

    Recoverable, by recover_garbled_greek; or written mostly in Latin Extended-A
    (U+0100 to U+017F), which is what the garbled rows are made of even when cut
    inside a letter's pair. Czech, Polish and Baltic names mix those letters with
    plain ones: on the replica none reached 30%, and the garbled names are 100%.
    """
    if recover_garbled_greek(name) is not None:
        return True
    letters = [ch for ch in name if ch.isalpha()]
    extended = sum(1 for ch in letters if "\u0100" <= ch <= "\u017f")
    return bool(letters) and extended * 2 > len(letters)


def _stands_in_for_a_name(name: str) -> bool:
    """An empty name, or an address a recipient without a display name was given."""
    name = name.strip()
    return not name or ("@" in name and not any(ch.isspace() for ch in name))


def find_or_create_person(
    conn: sqlite3.Connection,
    name: str,
    email: str | None = None,
    *,
    display_name: bool = False,
) -> int:
    """Find existing person by email or create new one.

    Email is the primary key for deduplication. If email is provided and matches
    an existing person, that person is returned (even if name differs slightly).
    If no email is provided, searches by exact name match.

    Args:
        conn: Database connection
        name: Person's name
        email: Person's email address (optional, but recommended for dedup)
        display_name: True when `name` is the sender's or a recipient's display
            name from the message headers. Only such a name may rename the person
            found by `email`, and only when the name on record is an address,
            empty or garbled: 'the longer name wins' let a garbled name, twice
            the length of the real one, replace it for good, and let any longer
            header overwrite a canonical name from import-people. A name the
            model extracted (people_roles) never renames anyone.

    Returns:
        Person ID (existing or newly created)
    """
    name = recover_garbled_greek(name) or name

    # If email provided, use it as primary deduplication key
    if email:
        email_normalized = email.strip().lower()

        cursor = conn.execute(
            "SELECT id, name FROM people WHERE LOWER(email) = ?", (email_normalized,)
        )
        row = cursor.fetchone()

        if row:
            person_id, existing_name = row[0], row[1]
            if (
                display_name
                and not _stands_in_for_a_name(name)
                and not looks_garbled(name)
                and (_stands_in_for_a_name(existing_name) or looks_garbled(existing_name))
            ):
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
