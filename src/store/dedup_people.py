"""Deduplicate and normalize the people table.

Conservative approach — only merges when there's a strong signal:
1. Clean parenthesized metadata from names (employee IDs, titles, dept numbers)
2. Merge no-email records into email records with same full name
3. Merge no-email internal duplicates (same full name, both no email)
4. Merge same full name + same personal email domain
5. Merge email-as-name records into proper records with that email

Name matching normalizes: case (UPPER/lower/Title), Greek accents (ά→α),
and whitespace. Two names match only if ALL words match after normalization.

A pass takes the whole table, or, after a sync, the groups of the rows that sync wrote
(see run_dedup).
"""

import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta

from src.config import DEFAULT_DB
from src.store.normalizer import looks_garbled, recover_garbled_greek
from src.store.transliterate import canonical_name

DB_PATH = DEFAULT_DB

# sync_metadata rows a pass leaves. HIGH_WATER_KEY: the highest people.id the pass had in
# view, so a row above it was added since, by whatever wrote it. FULL_AT_KEY: when a pass
# last took the whole table (UTC, ISO 8601).
HIGH_WATER_KEY = "people_dedup_high_water"
FULL_AT_KEY = "people_dedup_full_at"

# A pass narrowed to some rows takes the whole table instead once the last whole pass is
# this old. It covers what no narrowed pass sees: a rename by another writer (import-people,
# the repair scripts), and code that changes what a phase merges.
FULL_PASS_EVERY_H = 24.0

# Ids per IN (...) query, under SQLite's 999-variable limit on older builds.
_ID_CHUNK = 500

# Greek accent stripping map
_ACCENT_MAP = str.maketrans(
    "άέήίόύώΆΈΉΊΌΎΏϊϋΐΰ",
    "αεηιουωΑΕΗΙΟΥΩιυιυ",
)


def normalize_name(name: str) -> str:
    """Normalize a name for comparison purposes.

    Strips: case, Greek accents, extra whitespace, leading/trailing space.
    Returns a lowercase, accent-free, single-spaced string.
    """
    s = name.strip().lower()
    s = s.translate(_ACCENT_MAP)
    # Also handle combined unicode characters (e.g., ά as α + combining accent)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = unicodedata.normalize("NFC", s)
    # Collapse whitespace
    s = " ".join(s.split())
    return s


def pick_best_name(names: list[str]) -> str:
    """Pick the best display name from a list of variants.

    Prefers: Title Case > mixed case > ALL CAPS > all lower.
    Prefers: accented Greek > unaccented Greek.
    Prefers: longer name > shorter (more complete).

    Never a garbled name: its lower-case Latin letters scored as mixed case and
    its doubled length as more complete, so it beat the real ALL-CAPS Greek. A
    recoverable one stands in as its Greek, and an unrecoverable one wins only
    when every variant is garbled.
    """
    names = [recover_garbled_greek(n) or n for n in names]
    names = [n for n in names if not looks_garbled(n)] or names

    def score(name: str) -> tuple:
        n = name.strip()
        has_accents = any(c in n for c in "άέήίόύώ")
        is_all_caps = n == n.upper() and n != n.lower()
        is_all_lower = n == n.lower() and n != n.upper()
        is_title = not is_all_caps and not is_all_lower
        # Score: (title_case, has_accents, not_all_caps, length)
        return (is_title, has_accents, not is_all_caps, len(n))

    return max(names, key=score)


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or str(DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    # Match schema.get_connection: tolerate concurrent writers from launchd jobs
    # instead of failing instantly with "database is locked". 60s is long enough
    # to outlast any single transaction in the dedup/sync workloads.
    conn.execute("PRAGMA busy_timeout = 60000")
    # WAL + NORMAL sync (see schema.get_connection). foreign_keys stays OFF here by design.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # As schema.get_connection: zero what the dedup's deletes free.
    conn.execute("PRAGMA secure_delete = ON")
    return conn


def clean_name(name: str) -> str:
    """Remove parenthesized suffixes that are metadata, not name parts."""
    m = re.match(r"^(.+?)\s*\((.+?)\)\s*$", name)
    if not m:
        return name.strip()

    primary = m.group(1).strip()
    parens = m.group(2).strip()

    strip_patterns = [
        r"^[Ee]\d{4,}$",  # E40220, e38455
        r"^\d{1,4}$",  # 557, 001, 920
        r"^.*@.*$",  # email addresses
        r"^via\s+",  # via Looker Studio
        r"^ΔΙΕΥΘΥΝΤ",  # ΔΙΕΥΘΥΝΤΗΣ
        r"^Διευθύνων",  # Διευθύνων Σύμβουλος
        r"^Γραμματέας",  # Γραμματέας
        r"^Επικεφαλής",  # Επικεφαλής
        r"^Head of",  # Head of ...
        r"^Director",  # Director
        r"^Senior",  # Senior ...
        r"^Manager",  # Manager
    ]
    for pat in strip_patterns:
        if re.match(pat, parens, re.IGNORECASE):
            return primary

    return name.strip()


def merge_person(conn: sqlite3.Connection, keep_id: int, remove_id: int):
    """Merge remove_id into keep_id: reassign all references, then delete."""
    conn.execute(
        """
        UPDATE OR IGNORE email_people
        SET person_id = ?
        WHERE person_id = ?
    """,
        (keep_id, remove_id),
    )
    conn.execute("DELETE FROM email_people WHERE person_id = ?", (remove_id,))
    # Every other table that names a person. Left behind, a merged-away person's
    # Teams messages, meetings and resolved MRI pointed at a deleted row, so
    # person_context never showed them, and with foreign keys on the delete
    # below failed outright.
    conn.execute(
        "UPDATE teams_messages SET sender_person_id = ? WHERE sender_person_id = ?",
        (keep_id, remove_id),
    )
    conn.execute(
        "UPDATE teams_mri_resolution SET person_id = ? WHERE person_id = ?", (keep_id, remove_id)
    )
    conn.execute(
        "UPDATE event_attendees SET person_id = ? WHERE person_id = ?", (keep_id, remove_id)
    )
    # Who gave and who received a commitment (schema v35), on a store that has them.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(commitments)")}
    for column in ("by_person_id", "to_person_id"):
        if column in columns:
            conn.execute(
                f"UPDATE commitments SET {column} = ? WHERE {column} = ?", (keep_id, remove_id)
            )
    conn.execute("DELETE FROM people WHERE id = ?", (remove_id,))


# A phase given a scope (a set of person ids) acts only on the groups that hold one of
# them, and adds to it each survivor it renames or merges into, so the phases after it look
# at that row too. None is a pass over the whole table.


def _in_scope(scope: set[int] | None, members) -> bool:
    return scope is None or any(m["id"] in scope for m in members)


def _scoped_keys(scope: set[int] | None, rows, key) -> set[str] | None:
    """The keys the scope's rows hold, or None for a whole pass."""
    if scope is None:
        return None
    return {key(r["name"]) for r in rows if r["id"] in scope}


def _keep(scope: set[int] | None, person_id: int) -> None:
    if scope is not None:
        scope.add(person_id)


def _rows_with_ids(conn: sqlite3.Connection, columns: str, ids) -> list:
    ids = sorted(ids)
    rows: list = []
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        rows += conn.execute(
            f"SELECT {columns} FROM people WHERE id IN ({','.join('?' * len(chunk))}) ORDER BY id",
            chunk,
        ).fetchall()
    return rows


def phase1_clean_names(conn: sqlite3.Connection, scope: set[int] | None = None) -> int:
    """Clean parenthesized metadata from names."""
    if scope is None:
        people = conn.execute("SELECT id, name FROM people").fetchall()
    else:
        people = _rows_with_ids(conn, "id, name", scope)
    count = 0
    for p in people:
        cleaned = clean_name(p["name"])
        if cleaned != p["name"]:
            conn.execute("UPDATE people SET name = ? WHERE id = ?", (cleaned, p["id"]))
            count += 1
    return count


def phase2_merge_no_email_exact(conn: sqlite3.Connection, scope: set[int] | None = None) -> int:
    """Merge no-email people into email-people with same full name.

    Matches after normalizing case and Greek accents.
    """
    no_email = conn.execute("SELECT id, name FROM people WHERE email IS NULL").fetchall()
    with_email = conn.execute(
        "SELECT id, name, email FROM people WHERE email IS NOT NULL"
    ).fetchall()

    # Build normalized lookup for email-people
    email_by_norm = {}
    for p in with_email:
        norm = normalize_name(p["name"])
        if norm and len(norm) > 1:
            # Keep the one with more references if there are dupes
            if norm not in email_by_norm:
                email_by_norm[norm] = p

    names = _scoped_keys(scope, no_email + with_email, normalize_name)
    merged = set()
    count = 0
    for p in no_email:
        if p["id"] in merged:
            continue
        norm = normalize_name(p["name"])
        if not norm or len(norm) <= 1:
            continue
        # Skip single-word names (too ambiguous — "Nikos", "Stratos")
        if len(norm.split()) < 2:
            continue
        if names is not None and norm not in names:
            continue
        match = email_by_norm.get(norm)
        if match and match["id"] != p["id"]:
            merge_person(conn, match["id"], p["id"])
            merged.add(p["id"])
            count += 1
            _keep(scope, match["id"])

    return count


def phase3_merge_no_email_dupes(conn: sqlite3.Connection, scope: set[int] | None = None) -> int:
    """Merge duplicate no-email people with same full name."""
    no_email = conn.execute("SELECT id, name FROM people WHERE email IS NULL").fetchall()

    from collections import defaultdict

    groups = defaultdict(list)
    for p in no_email:
        norm = normalize_name(p["name"])
        if norm and len(norm.split()) >= 2:
            groups[norm].append(p)

    count = 0
    for _norm, members in groups.items():
        if len(members) < 2 or not _in_scope(scope, members):
            continue
        best_name = pick_best_name([m["name"] for m in members])
        keep = members[0]
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (best_name, keep["id"]))
        _keep(scope, keep["id"])
        for m in members[1:]:
            merge_person(conn, keep["id"], m["id"])
            count += 1

    return count


def phase4_merge_same_name_diff_email(
    conn: sqlite3.Connection, scope: set[int] | None = None
) -> int:
    """Merge people with same full name and personal emails."""
    personal_domains = {
        "gmail.com",
        "hotmail.com",
        "msn.com",
        "icloud.com",
        "yahoo.com",
        "yahoo.gr",
        "outlook.com",
        "live.com",
        "protonmail.com",
        "mail.com",
    }

    with_email = conn.execute(
        "SELECT id, name, email FROM people WHERE email IS NOT NULL"
    ).fetchall()

    from collections import defaultdict

    groups = defaultdict(list)
    for p in with_email:
        norm = normalize_name(p["name"])
        if norm and len(norm.split()) >= 2:
            groups[norm].append(p)

    count = 0
    for _norm, members in groups.items():
        if len(members) < 2 or not _in_scope(scope, members):
            continue
        # Only merge if ALL emails are personal
        all_personal = all(
            any(m["email"].lower().endswith(dom) for dom in personal_domains) for m in members
        )
        if not all_personal:
            continue

        best_name = pick_best_name([m["name"] for m in members])
        keep = members[0]
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (best_name, keep["id"]))
        _keep(scope, keep["id"])
        for m in members[1:]:
            merge_person(conn, keep["id"], m["id"])
            count += 1

    return count


def phase5_merge_email_as_name(conn: sqlite3.Connection, scope: set[int] | None = None) -> int:
    """Merge/clean records where name IS the email address.

    Each such row costs a scan of the table for its match, and one whose address has no
    dot in its local part is never rewritten, so a whole pass pays for every one of them
    every time. A scoped pass takes the scope's own rows, and the rows whose name is an
    address a scope row now holds, which that row may have become the match of.
    """
    email_names = conn.execute(
        "SELECT id, name, email FROM people WHERE name LIKE '%@%'"
    ).fetchall()
    if scope is not None:
        held = {
            r["email"].lower().strip()
            for r in _rows_with_ids(conn, "id, email", scope)
            if r["email"]
        }
        email_names = [
            p for p in email_names if p["id"] in scope or p["name"].lower().strip() in held
        ]

    count = 0
    for p in email_names:
        name_email = p["name"].lower().strip()
        # Find another person with same email but a real name
        match = conn.execute(
            """
            SELECT id, name FROM people
            WHERE LOWER(email) = ? AND id != ? AND name NOT LIKE '%@%'
            LIMIT 1
        """,
            (name_email, p["id"]),
        ).fetchone()

        if match:
            merge_person(conn, match["id"], p["id"])
            count += 1
            _keep(scope, match["id"])
        elif p["email"] and p["email"].lower() == name_email:
            # No match — convert email-as-name to readable form
            local = name_email.split("@")[0]
            parts = local.split(".")
            if len(parts) >= 2:
                readable = " ".join(part.capitalize() for part in parts)
                conn.execute("UPDATE people SET name = ? WHERE id = ?", (readable, p["id"]))
                count += 1
                _keep(scope, p["id"])

    return count


def phase6_normalize_best_names(conn: sqlite3.Connection, scope: set[int] | None = None) -> int:
    """For remaining people with email, pick best name among case/accent variants.

    If two records share the same email (after normalization), merge them and
    keep the best-formatted name.
    """
    # Group by normalized email
    all_people = conn.execute(
        "SELECT id, name, email FROM people WHERE email IS NOT NULL"
    ).fetchall()

    from collections import defaultdict

    by_email = defaultdict(list)
    for p in all_people:
        by_email[p["email"].lower().strip()].append(p)

    count = 0
    for _email, members in by_email.items():
        if len(members) < 2 or not _in_scope(scope, members):
            continue
        best_name = pick_best_name([m["name"] for m in members])
        keep = members[0]
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (best_name, keep["id"]))
        _keep(scope, keep["id"])
        for m in members[1:]:
            merge_person(conn, keep["id"], m["id"])
            count += 1

    return count


def phase7_merge_transliterated_variants(
    conn: sqlite3.Connection, scope: set[int] | None = None
) -> int:
    """Merge order-reversed and Greek<->Latin script variants of the same name.

    Groups people by a transliterated, order-independent canonical key (see
    src.store.transliterate.canonical_name) and merges within a group ONLY when
    there is at most one distinct email — so two different people who merely share
    a name but have different emails are never merged. This complements the
    exact-match phases, which miss name reversal ("Παπαδόπουλος Νίκος" vs
    "Νίκος Παπαδόπουλος") and cross-script forms ("Νίκος Παπαδόπουλος" vs
    "Nikos Papadopoulos").
    """
    from collections import defaultdict

    people = conn.execute("SELECT id, name, email FROM people").fetchall()
    groups: dict[str, list] = defaultdict(list)
    for p in people:
        key = canonical_name(p["name"])
        if key and len(key.split()) >= 2:  # require >=2 tokens (skip lone names)
            groups[key].append(p)

    count = 0
    for _key, members in groups.items():
        if len(members) < 2 or not _in_scope(scope, members):
            continue
        distinct_emails = {
            m["email"].lower().strip() for m in members if m["email"] and m["email"].strip()
        }
        if len(distinct_emails) > 1:
            continue  # same name, different emails -> different people; never merge

        # Survivor: prefer an email-bearing record (False sorts before True), then lowest id.
        members_sorted = sorted(members, key=lambda m: (m["email"] is None, m["id"]))
        keep = members_sorted[0]
        best_name = pick_best_name([m["name"] for m in members])
        keep_email = keep["email"] or next((m["email"] for m in members if m["email"]), None)

        # Merge (and delete) the others FIRST, so reassigning keep's email can't
        # collide with a soon-to-be-deleted duplicate on the UNIQUE(email) index.
        for m in members_sorted[1:]:
            merge_person(conn, keep["id"], m["id"])
            count += 1
        conn.execute(
            "UPDATE people SET name = ?, email = ? WHERE id = ?",
            (best_name, keep_email, keep["id"]),
        )
        _keep(scope, keep["id"])

    return count


def _top_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM people").fetchone()[0]


def _marks(conn: sqlite3.Connection) -> tuple[bool, int | None, datetime | None]:
    """(whether the store keeps sync_metadata, the high water mark, the last whole pass)."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sync_metadata'"
    ).fetchone():
        return False, None, None  # a store from before the table: every pass is whole
    stored = dict(
        conn.execute(
            "SELECT key, value FROM sync_metadata WHERE key IN (?, ?)",
            (HIGH_WATER_KEY, FULL_AT_KEY),
        ).fetchall()
    )
    try:
        high_water = int(stored[HIGH_WATER_KEY])
    except (KeyError, TypeError, ValueError):
        high_water = None
    try:
        full_at = datetime.fromisoformat(stored[FULL_AT_KEY])
    except (KeyError, TypeError, ValueError):
        full_at = None
    if full_at is not None and full_at.tzinfo is None:
        full_at = full_at.replace(tzinfo=UTC)
    return True, high_water, full_at


def run_dedup(
    db_path: str | None = None, dry_run: bool = False, touched: set[int] | None = None
) -> dict:
    """Run the seven phases, over the whole table or over the groups of some rows.

    `touched` None, the default and what the standalone command passes, takes the whole
    table. A set of person ids, the rows a sync's load added or changed, takes in each phase
    only the groups those rows fall in, together with every row above the high water mark
    (added since the last pass, by whatever wrote it). The rest of the table was settled by
    the passes before, so the merges are the ones a whole pass would make there
    (tests/test_dedup_touched.py). The whole table is taken anyway on a store no pass has
    run on, and once the last whole pass is FULL_PASS_EVERY_H old.

    The high water mark is the lower of the top id before and after the pass: SQLite gives
    the next row the id after the highest one left, so the id of a top row this pass merged
    away comes back, and the mark must sit below it. A dry run moves neither mark.
    """
    conn = get_connection(db_path)
    started = datetime.now(UTC)
    top_before = _top_id(conn)
    keeps_marks, high_water, full_at = _marks(conn)
    scope = None
    if (
        touched is not None
        and high_water is not None
        and full_at is not None
        and started - full_at < timedelta(hours=FULL_PASS_EVERY_H)
    ):
        added = conn.execute("SELECT id FROM people WHERE id > ?", (high_water,)).fetchall()
        scope = set(touched) | {row[0] for row in added}

    before = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    results: dict = {"scope": None if scope is None else len(scope)}

    print(f"Before: {before} people")
    if scope is not None:
        print(f"Scope: the groups of {len(scope)} people added or changed since the last pass")
    print()

    phases = [
        ("phase1_cleaned", "Cleaning parenthesized metadata from names", phase1_clean_names),
        (
            "phase2_merged",
            "Merging no-email → email (normalized full name match)",
            phase2_merge_no_email_exact,
        ),
        ("phase3_merged", "Merging no-email internal duplicates", phase3_merge_no_email_dupes),
        (
            "phase4_merged",
            "Merging same-name personal email variants",
            phase4_merge_same_name_diff_email,
        ),
        ("phase5_cleaned", "Cleaning email-as-name records", phase5_merge_email_as_name),
        (
            "phase6_normalized",
            "Normalizing display names (best case/accent variant)",
            phase6_normalize_best_names,
        ),
        (
            "phase7_merged",
            "Merging transliterated / reversed name variants",
            phase7_merge_transliterated_variants,
        ),
    ]
    if scope is not None and not scope:
        print("Nobody added or changed since the last pass: no phase to run")
        results.update(dict.fromkeys((key for key, _, _ in phases), 0))
    else:
        for n, (key, title, phase) in enumerate(phases, 1):
            print(f"Phase {n}: {title}...")
            results[key] = phase(conn, scope)
            print(f"  {results[key]} records")

    after = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    results["before"] = before
    results["after"] = after
    results["total_reduced"] = before - after

    print()
    print(f"After: {after} people ({before - after} removed)")

    if keeps_marks:
        marks = [(HIGH_WATER_KEY, str(min(top_before, _top_id(conn))))]
        if scope is None:
            marks.append((FULL_AT_KEY, started.isoformat()))
        conn.executemany("INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)", marks)

    if dry_run:
        conn.rollback()
        print("DRY RUN — changes rolled back")
    else:
        conn.commit()
        print("Changes committed.")

    conn.close()
    return results


def main() -> int:
    import argparse
    import sys

    from src.config import is_replica, replica_refusal

    parser = argparse.ArgumentParser(description="Deduplicate people table")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if is_replica():
        # Even a dry run writes and rolls back, and a replica's copy is replaced
        # by the next pull (see src/config.py).
        print(replica_refusal("the people dedup"), file=sys.stderr)
        return 2
    run_dedup(args.db, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
