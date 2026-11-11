"""Identities, mentions, identifiers and logged merges (issue 170, gated person_identity).

A people row with a hard identifier (an address, a Teams MRI, a WhatsApp JID, a phone) is
an identity; a row the model named with none of them is a mention, listed in
person_mentions. The rows stay in people, which ten tables point at, so the split is by
membership: the view person_identities holds the rows with an identifier. Every
identifier, and every name a person went by, is a row of person_identifiers; a hard one
belongs to one person only, so an address merged away still finds the row it went to.

email_people keeps the header role as an enum in role_in_email (sender, recipient, cc,
bcc, or mentioned for a name the model gave) and the model's own words in role_text,
outside the primary key.

Two rows merge by themselves only on an exact hard identifier. A match on a name alone is
queued in person_review, which the owner clears with `brain maint people-review`. Every
merge writes person_merges with the rule, the evidence, the row removed and every
reference it moved, so `brain maint people-unmerge ID` puts it back. The owner's row is
fixed in person_owner and is always the one a merge keeps.

Without the step (a store where `active` is False) nothing here runs and the people dedup
merges as it did.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

NAME = "person_identity"

ROLES = ("sender", "recipient", "cc", "bcc", "mentioned")
HEADER_ROLES = ROLES[:-1]
HARD_KINDS = ("address", "mri", "jid", "phone")
KINDS = (*HARD_KINDS, "name")
ROLE_TEXT_SEPARATOR = "; "

_ROLE_CHECK = ", ".join(f"'{r}'" for r in ROLES)
_KIND_CHECK = ", ".join(f"'{k}'" for k in KINDS)
_HARD_SQL = ", ".join(f"'{k}'" for k in HARD_KINDS)

TABLES = (
    f"""CREATE TABLE IF NOT EXISTS person_identifiers (
        id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ({_KIND_CHECK})),
        value TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    # A hard identifier is one person's; a name may be many people's, once each.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_person_identifiers_hard"
    f" ON person_identifiers(kind, value) WHERE kind IN ({_HARD_SQL})",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_person_identifiers_person"
    " ON person_identifiers(person_id, kind, value)",
    "CREATE INDEX IF NOT EXISTS idx_person_identifiers_value ON person_identifiers(kind, value)",
    """CREATE TABLE IF NOT EXISTS person_mentions (
        person_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS person_merges (
        id INTEGER PRIMARY KEY,
        kept_id INTEGER NOT NULL,
        merged_id INTEGER NOT NULL,
        method TEXT NOT NULL,
        evidence TEXT,
        merged_row TEXT NOT NULL,
        moved TEXT NOT NULL,
        created_at TEXT NOT NULL,
        undone_at TEXT,
        restored_id INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_person_merges_kept ON person_merges(kept_id)",
    "CREATE INDEX IF NOT EXISTS idx_person_merges_merged ON person_merges(merged_id)",
    """CREATE TABLE IF NOT EXISTS person_review (
        id INTEGER PRIMARY KEY,
        person_a INTEGER NOT NULL,
        person_b INTEGER NOT NULL,
        rule TEXT NOT NULL,
        evidence TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'merged', 'rejected', 'gone')),
        created_at TEXT NOT NULL,
        decided_at TEXT,
        merge_id INTEGER,
        UNIQUE (person_a, person_b)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_person_review_status ON person_review(status)",
    """CREATE TABLE IF NOT EXISTS person_owner (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        person_id INTEGER NOT NULL,
        set_at TEXT NOT NULL
    )""",
    f"""CREATE VIEW IF NOT EXISTS person_identities AS
        SELECT p.* FROM people p WHERE EXISTS (SELECT 1 FROM person_identifiers i
        WHERE i.person_id = p.id AND i.kind IN ({_HARD_SQL}))""",
)

EMAIL_PEOPLE = f"""CREATE TABLE email_people_v170 (
    email_id INTEGER REFERENCES emails(id),
    person_id INTEGER REFERENCES people(id),
    role_in_email TEXT NOT NULL CHECK (role_in_email IN ({_ROLE_CHECK})),
    role_text TEXT,
    PRIMARY KEY (email_id, person_id, role_in_email)
)"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def active(conn: sqlite3.Connection) -> bool:
    """Whether this store has taken the step."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'person_merges'"
        ).fetchone()
        is not None
    )


# --- identifiers --------------------------------------------------------------------------


def _norm(kind: str, value: str) -> str:
    value = value.strip()
    return value.lower() if kind in ("address", "jid") else value


def record(
    conn: sqlite3.Connection, person_id: int, kind: str, value: str | None, source: str
) -> None:
    """Note an identifier of a person; a hard one already someone's is left to them."""
    if not value or not value.strip():
        return
    value = _norm(kind, value)
    conn.execute(
        "INSERT OR IGNORE INTO person_identifiers (person_id, kind, value, source, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (person_id, kind, value, source, _now()),
    )
    if kind in HARD_KINDS:
        conn.execute("DELETE FROM person_mentions WHERE person_id = ?", (person_id,))


def person_by(conn: sqlite3.Connection, kind: str, value: str) -> int | None:
    """The person a hard identifier belongs to, if the row is still there."""
    row = conn.execute(
        "SELECT i.person_id FROM person_identifiers i JOIN people p ON p.id = i.person_id"
        " WHERE i.kind = ? AND i.value = ?",
        (kind, _norm(kind, value)),
    ).fetchone()
    return row[0] if row else None


def note_mention(conn: sqlite3.Connection, person_id: int, name: str) -> None:
    """A row created with no identifier: a mention, under the name it was given."""
    conn.execute(
        "INSERT OR IGNORE INTO person_mentions (person_id, name, created_at) VALUES (?, ?, ?)",
        (person_id, name, _now()),
    )
    record(conn, person_id, "name", name, "loader")


# --- the owner ----------------------------------------------------------------------------


def owner_id(conn: sqlite3.Connection) -> int | None:
    """The owner's fixed people id, or None on a store without one."""
    if "person_owner" not in _tables(conn):
        return None
    row = conn.execute("SELECT person_id FROM person_owner WHERE id = 1").fetchone()
    return row[0] if row else None


def set_owner(conn: sqlite3.Connection, person_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO person_owner (id, person_id, set_at) VALUES (1, ?, ?)",
        (person_id, _now()),
    )


# --- email_people roles -------------------------------------------------------------------


def link_role(conn: sqlite3.Connection, email_id: int, person_id: int, role: str) -> None:
    """Link a person the model named in an email, under its role. With the step the role
    is 'mentioned' and the model's words go to role_text, each once; without it, the words
    are the role, as before."""
    if "role_text" not in _columns(conn, "email_people"):
        conn.execute(
            "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email)"
            " VALUES (?, ?, ?)",
            (email_id, person_id, role),
        )
        return
    role = role.strip()
    row = conn.execute(
        "SELECT role_text FROM email_people"
        " WHERE email_id = ? AND person_id = ? AND role_in_email = 'mentioned'",
        (email_id, person_id),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email, role_text)"
            " VALUES (?, ?, 'mentioned', ?)",
            (email_id, person_id, role or None),
        )
        return
    held = [t for t in (row[0] or "").split(ROLE_TEXT_SEPARATOR) if t]
    if role and role not in held:
        conn.execute(
            "UPDATE email_people SET role_text = ?"
            " WHERE email_id = ? AND person_id = ? AND role_in_email = 'mentioned'",
            (ROLE_TEXT_SEPARATOR.join([*held, role]), email_id, person_id),
        )


def _rebuild_email_people(conn: sqlite3.Connection) -> None:
    """email_people with the enum role: a header role stays, any other text becomes
    'mentioned' with the text in role_text (the texts of one person in one email joined),
    and every row of a person with no address is a mention, as the headers always give
    one."""
    indexes = [
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'email_people'"
            " AND sql IS NOT NULL"
        )
    ]
    conn.execute("DROP TABLE IF EXISTS email_people_v170")
    conn.execute(EMAIL_PEOPLE)
    header = ", ".join(f"'{r}'" for r in HEADER_ROLES)
    conn.execute(
        f"""
        INSERT INTO email_people_v170 (email_id, person_id, role_in_email, role_text)
        SELECT email_id, person_id, role, group_concat(text, '{ROLE_TEXT_SEPARATOR}')
        FROM (
            SELECT DISTINCT ep.email_id, ep.person_id,
                CASE WHEN ep.role_in_email IN ({header})
                      AND COALESCE(p.email, '') <> '' THEN ep.role_in_email
                     ELSE 'mentioned' END AS role,
                CASE WHEN ep.role_in_email IN ({header})
                      AND COALESCE(p.email, '') <> '' THEN NULL
                     ELSE NULLIF(trim(ep.role_in_email), '') END AS text
            FROM email_people ep LEFT JOIN people p ON p.id = ep.person_id
            ORDER BY ep.email_id, ep.person_id, text
        )
        GROUP BY email_id, person_id, role
        """
    )
    conn.execute("DROP TABLE email_people")
    conn.execute("ALTER TABLE email_people_v170 RENAME TO email_people")
    for sql in indexes:
        conn.execute(sql)


# --- the migration ------------------------------------------------------------------------


def migrate(conn: sqlite3.Connection) -> None:
    """Gated migration person_identity. The caller commits (schema.apply_gated), or rolls
    back for a dry run. Adds rows and tables; the one merge it makes is of the rows at the
    owner's own addresses into his fixed id, logged like any other."""
    for sql in TABLES:
        conn.execute(sql)
    if "email_people" in _tables(conn) and "role_text" not in _columns(conn, "email_people"):
        _rebuild_email_people(conn)
    at = _now()
    conn.execute(
        "INSERT OR IGNORE INTO person_identifiers (person_id, kind, value, source, created_at)"
        " SELECT id, 'address', lower(trim(email)), 'people.email', ? FROM people"
        " WHERE COALESCE(trim(email), '') <> '' ORDER BY id",
        (at,),
    )
    if "teams_mri_resolution" in _tables(conn):
        conn.execute(
            "INSERT OR IGNORE INTO person_identifiers"
            " (person_id, kind, value, source, created_at)"
            " SELECT r.person_id, 'mri', r.mri, 'teams_mri_resolution', ? FROM"
            " teams_mri_resolution r JOIN people p ON p.id = r.person_id ORDER BY r.mri",
            (at,),
        )
    if {"entity_aliases", "entities"} <= _tables(conn):
        # An MRI or JID the entity seed joined to a person (src/store/entities.py).
        conn.execute(
            "INSERT OR IGNORE INTO person_identifiers"
            " (person_id, kind, value, source, created_at)"
            " SELECT e.person_id, a.kind, a.value, 'entity_aliases', ? FROM entity_aliases a"
            " JOIN entities e ON e.id = a.entity_id JOIN people p ON p.id = e.person_id"
            " WHERE a.kind IN ('mri', 'jid')",
            (at,),
        )
    conn.execute(
        "INSERT OR IGNORE INTO person_identifiers (person_id, kind, value, source, created_at)"
        " SELECT id, 'name', trim(name), 'people.name', ? FROM people"
        " WHERE COALESCE(trim(name), '') <> ''",
        (at,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO person_mentions (person_id, name, created_at)"
        " SELECT p.id, p.name, ? FROM people p WHERE NOT EXISTS (SELECT 1 FROM"
        f" person_identifiers i WHERE i.person_id = p.id AND i.kind IN ({_HARD_SQL}))",
        (at,),
    )
    _fix_owner(conn)


def _fix_owner(conn: sqlite3.Connection) -> None:
    """The owner's id: the row the readers chose (src/store/owner.py owner_person_id), and
    every other row at one of his addresses merged into it."""
    from src import config
    from src.store.owner import is_owner_address, owner_person_id

    rows = [
        (r[0], r[1])
        for r in conn.execute("SELECT id, email FROM people WHERE email IS NOT NULL")
        if is_owner_address(r[1], config.USER_EMAIL_PATTERN)
    ]
    fixed = owner_person_id(conn, [r[0] for r in rows])
    if fixed is None:
        return
    set_owner(conn, fixed)
    for person_id, address in rows:
        if person_id != fixed:
            merge(conn, fixed, person_id, method="owner", evidence=f"address:{address.lower()}")


# --- merges -------------------------------------------------------------------------------

# (table, column) pairs that name a person, besides email_people, the identifiers, the
# mentions and the entity tables, which have their own handling.
_REFERENCES = (
    ("teams_messages", "sender_person_id", "id"),
    ("teams_mri_resolution", "person_id", "mri"),
    ("event_attendees", "person_id", "id"),
    ("commitments", "by_person_id", "id"),
    ("commitments", "to_person_id", "id"),
    ("commitments", "counterpart_person_id", "id"),
    ("decisions", "counterpart_person_id", "id"),
    ("action_items", "counterpart_person_id", "id"),
    ("entities", "person_id", "id"),
)


def merge(
    conn: sqlite3.Connection,
    keep_id: int,
    remove_id: int,
    *,
    method: str,
    evidence: str | None = None,
) -> int | None:
    """Merge remove_id into keep_id, logged with everything it moved; the log id. The
    owner's row is always the one kept. Nothing is committed here."""
    if keep_id == remove_id:
        return None
    fixed = owner_id(conn)
    if remove_id == fixed:
        keep_id, remove_id = remove_id, keep_id
    gone = conn.execute(
        "SELECT id, name, email, role, department FROM people WHERE id = ?", (remove_id,)
    ).fetchone()
    if gone is None or not conn.execute("SELECT 1 FROM people WHERE id = ?", (keep_id,)).fetchone():
        return None
    merged_row = {
        "id": gone[0],
        "name": gone[1],
        "email": gone[2],
        "role": gone[3],
        "department": gone[4],
    }
    # The address of the row merged away stays an identifier, now of the kept row.
    if gone[2]:
        record(conn, remove_id, "address", gone[2], "people.email")
    tables = _tables(conn)
    moved: dict = {}

    kept_keys = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT email_id, role_in_email FROM email_people WHERE person_id = ?", (keep_id,)
        )
    }
    text = "role_text" in _columns(conn, "email_people")
    links = conn.execute(
        "SELECT email_id, role_in_email"
        + (", role_text" if text else ", NULL")
        + " FROM email_people WHERE person_id = ?",
        (remove_id,),
    ).fetchall()
    moved["email_people"] = [[r[0], r[1], r[2], (r[0], r[1]) in kept_keys] for r in links]
    conn.execute(
        "UPDATE OR IGNORE email_people SET person_id = ? WHERE person_id = ?", (keep_id, remove_id)
    )
    conn.execute("DELETE FROM email_people WHERE person_id = ?", (remove_id,))

    for table, column, key in _REFERENCES:
        if table not in tables or column not in _columns(conn, table):
            continue
        keys = [
            r[0]
            for r in conn.execute(f"SELECT {key} FROM {table} WHERE {column} = ?", (remove_id,))
        ]
        if keys:
            moved[f"{table}.{column}"] = keys
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (keep_id, remove_id)
            )

    if "entity_aliases" in tables:
        alias = conn.execute(
            "SELECT entity_id, method, score, created_at FROM entity_aliases"
            " WHERE kind = 'person' AND value = ?",
            (str(remove_id),),
        ).fetchone()
        if alias is not None:
            took = conn.execute(
                "UPDATE OR IGNORE entity_aliases SET value = ? WHERE kind = 'person' AND value = ?",
                (str(keep_id), str(remove_id)),
            ).rowcount
            conn.execute(
                "DELETE FROM entity_aliases WHERE kind = 'person' AND value = ?", (str(remove_id),)
            )
            moved["entity_alias"] = [*alias, bool(took)]

    identifiers = conn.execute(
        "SELECT id, kind, value, source FROM person_identifiers WHERE person_id = ?",
        (remove_id,),
    ).fetchall()
    held = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT kind, value FROM person_identifiers WHERE person_id = ?", (keep_id,)
        )
    }
    moved["person_identifiers"] = [
        [r[1], r[2], r[3], (r[1], r[2]) not in held] for r in identifiers
    ]
    conn.execute(
        "UPDATE OR IGNORE person_identifiers SET person_id = ? WHERE person_id = ?",
        (keep_id, remove_id),
    )
    conn.execute("DELETE FROM person_identifiers WHERE person_id = ?", (remove_id,))
    mention = conn.execute(
        "SELECT name, created_at FROM person_mentions WHERE person_id = ?", (remove_id,)
    ).fetchone()
    if mention is not None:
        moved["person_mention"] = list(mention)
        conn.execute("DELETE FROM person_mentions WHERE person_id = ?", (remove_id,))
    if any(k in HARD_KINDS for k, _ in held | {(r[1], r[2]) for r in identifiers}):
        conn.execute("DELETE FROM person_mentions WHERE person_id = ?", (keep_id,))

    conn.execute("DELETE FROM people WHERE id = ?", (remove_id,))
    cursor = conn.execute(
        "INSERT INTO person_merges (kept_id, merged_id, method, evidence, merged_row, moved,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            keep_id,
            remove_id,
            method,
            evidence,
            json.dumps(merged_row, ensure_ascii=False),
            json.dumps(moved, ensure_ascii=False),
            _now(),
        ),
    )
    return cursor.lastrowid


def unmerge(conn: sqlite3.Connection, merge_id: int) -> int:
    """Undo a logged merge: the row back (under its old id when that is free) and every
    reference the merge moved pointed at it again. The id the row has now. Raises
    ValueError for an unknown or already undone merge. Nothing is committed here."""
    row = conn.execute(
        "SELECT kept_id, merged_row, moved, undone_at FROM person_merges WHERE id = ?",
        (merge_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no merge {merge_id}")
    if row[3] is not None:
        raise ValueError(f"merge {merge_id} was undone at {row[3]}")
    keep_id = row[0]
    person = json.loads(row[1])
    moved = json.loads(row[2])
    old_id = person["id"]
    email = person["email"]
    if (
        email
        and conn.execute("SELECT 1 FROM people WHERE lower(email) = lower(?)", (email,)).fetchone()
    ):
        email = None  # the kept row took the address; it stays there
    if conn.execute("SELECT 1 FROM people WHERE id = ?", (old_id,)).fetchone():
        back = conn.execute(
            "INSERT INTO people (name, email, role, department) VALUES (?, ?, ?, ?)",
            (person["name"], email, person["role"], person["department"]),
        ).lastrowid
    else:
        conn.execute(
            "INSERT INTO people (id, name, email, role, department) VALUES (?, ?, ?, ?, ?)",
            (old_id, person["name"], email, person["role"], person["department"]),
        )
        back = old_id
    assert back is not None

    text = "role_text" in _columns(conn, "email_people")
    for email_id, role, role_text, collided in moved.get("email_people", []):
        if not collided:
            conn.execute(
                "DELETE FROM email_people WHERE email_id = ? AND person_id = ? AND role_in_email = ?",
                (email_id, keep_id, role),
            )
        if text:
            conn.execute(
                "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email, role_text)"
                " VALUES (?, ?, ?, ?)",
                (email_id, back, role, role_text),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email)"
                " VALUES (?, ?, ?)",
                (email_id, back, role),
            )

    tables = _tables(conn)
    for table, column, key in _REFERENCES:
        keys = moved.get(f"{table}.{column}")
        if not keys or table not in tables:
            continue
        conn.executemany(
            f"UPDATE {table} SET {column} = ? WHERE {key} = ? AND {column} = ?",
            [(back, k, keep_id) for k in keys],
        )

    alias = moved.get("entity_alias")
    if alias and "entity_aliases" in tables:
        entity_id, method, score, created_at, took = alias
        if took:
            conn.execute(
                "UPDATE entity_aliases SET value = ? WHERE kind = 'person' AND value = ?"
                " AND entity_id = ?",
                (str(back), str(keep_id), entity_id),
            )
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (kind, value, entity_id, method, score,"
            " created_at) VALUES ('person', ?, ?, ?, ?, ?)",
            (str(back), entity_id, method, score, created_at),
        )

    for kind, value, source, took in moved.get("person_identifiers", []):
        if took:
            conn.execute(
                "UPDATE person_identifiers SET person_id = ?"
                " WHERE person_id = ? AND kind = ? AND value = ?",
                (back, keep_id, kind, value),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO person_identifiers"
                " (person_id, kind, value, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (back, kind, value, source, _now()),
            )
    mention = moved.get("person_mention")
    if mention:
        conn.execute(
            "INSERT OR IGNORE INTO person_mentions (person_id, name, created_at) VALUES (?, ?, ?)",
            (back, mention[0], mention[1]),
        )
    conn.execute(
        "UPDATE person_merges SET undone_at = ?, restored_id = ? WHERE id = ?",
        (_now(), back, merge_id),
    )
    conn.execute(
        "UPDATE person_review SET status = 'rejected', decided_at = ? WHERE merge_id = ?",
        (_now(), merge_id),
    )
    return back


# --- the review queue ---------------------------------------------------------------------


def queue(
    conn: sqlite3.Connection, keep_id: int, other_id: int, rule: str, evidence: str | None = None
) -> bool:
    """Queue a name-only match for the owner; False when the pair was queued before, in
    whatever state (a rejected pair is not asked again)."""
    a, b = keep_id, other_id
    if conn.execute(
        "SELECT 1 FROM person_review WHERE (person_a = ? AND person_b = ?)"
        " OR (person_a = ? AND person_b = ?)",
        (a, b, b, a),
    ).fetchone():
        return False
    conn.execute(
        "INSERT INTO person_review (person_a, person_b, rule, evidence, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (a, b, rule, evidence, _now()),
    )
    return True


def auto_merge(
    conn: sqlite3.Connection, keep_id: int, other_id: int, rule: str, evidence: str | None = None
) -> bool | None:
    """Merge a name-only match by itself, the owner's rule of 2026-11-11 for a name that
    belongs to exactly one person with an address (the caller decides that). True when it
    merged, closing the pair's pending review if one was queued. None when the owner has
    spoken on the pair already, by rejecting it or by undoing its merge: it is left alone,
    neither merged nor queued again. False when nothing was merged (a row gone)."""
    a, b = keep_id, other_id
    if conn.execute(
        "SELECT 1 FROM person_merges WHERE undone_at IS NOT NULL"
        " AND ((kept_id = ? AND COALESCE(restored_id, merged_id) = ?)"
        " OR (kept_id = ? AND COALESCE(restored_id, merged_id) = ?))",
        (a, b, b, a),
    ).fetchone():
        return None
    review = conn.execute(
        "SELECT id, status FROM person_review WHERE (person_a = ? AND person_b = ?)"
        " OR (person_a = ? AND person_b = ?)",
        (a, b, b, a),
    ).fetchone()
    if review is not None and review[1] != "pending":
        return None
    merge_id = merge(conn, a, b, method=rule, evidence=evidence)
    if review is not None:
        conn.execute(
            "UPDATE person_review SET status = ?, decided_at = ?, merge_id = ? WHERE id = ?",
            ("merged" if merge_id is not None else "gone", _now(), merge_id, review[0]),
        )
    return merge_id is not None


def pending(conn: sqlite3.Connection) -> list[dict]:
    """The pairs waiting, with the names and addresses of both rows."""
    rows = conn.execute(
        """SELECT r.id, r.rule, r.evidence, r.person_a, a.name, a.email, r.person_b,
                  b.name, b.email
           FROM person_review r
           LEFT JOIN people a ON a.id = r.person_a
           LEFT JOIN people b ON b.id = r.person_b
           WHERE r.status = 'pending' ORDER BY r.id"""
    ).fetchall()
    keys = ("id", "rule", "evidence", "a", "a_name", "a_email", "b", "b_name", "b_email")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def decide(conn: sqlite3.Connection, review_id: int, verdict: str) -> int | None:
    """Merge or reject one queued pair; the merge's log id for a merge that happened. A
    pair one of whose rows is gone (merged elsewhere meanwhile) is closed as 'gone'."""
    row = conn.execute(
        "SELECT person_a, person_b, rule, evidence, status FROM person_review WHERE id = ?",
        (review_id,),
    ).fetchone()
    if row is None or row[4] != "pending":
        return None
    at = _now()
    if verdict == "reject":
        conn.execute(
            "UPDATE person_review SET status = 'rejected', decided_at = ? WHERE id = ?",
            (at, review_id),
        )
        return None
    merge_id = merge(conn, row[0], row[1], method="review", evidence=row[3] or row[2])
    status = "merged" if merge_id is not None else "gone"
    conn.execute(
        "UPDATE person_review SET status = ?, decided_at = ?, merge_id = ? WHERE id = ?",
        (status, at, merge_id, review_id),
    )
    return merge_id


# --- counts for the dry run ---------------------------------------------------------------


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """The rows the step changes, for `brain maint migrate --apply-gated` to print."""
    tables = _tables(conn)
    out: dict[str, int] = {}
    if "people" in tables:
        out["people"] = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    for table in ("person_identifiers", "person_mentions", "person_merges", "person_review"):
        out[table] = (
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in tables else 0
        )
    if "person_identifiers" in tables:
        for kind, n in conn.execute(
            "SELECT kind, COUNT(*) FROM person_identifiers GROUP BY kind"
        ).fetchall():
            out[f"person_identifiers.{kind}"] = n
    if "email_people" in tables:
        out["email_people"] = conn.execute("SELECT COUNT(*) FROM email_people").fetchone()[0]
        out["email_people.distinct_roles"] = conn.execute(
            "SELECT COUNT(DISTINCT role_in_email) FROM email_people"
        ).fetchone()[0]
        for role in ROLES:
            out[f"email_people.{role}"] = conn.execute(
                "SELECT COUNT(*) FROM email_people WHERE role_in_email = ?", (role,)
            ).fetchone()[0]
    return out
