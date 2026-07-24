"""Deduplicate and normalize the people table.

Conservative approach — only merges when there's a strong signal:
1. Clean parenthesized metadata from names (employee IDs, titles, dept numbers)
2. Merge no-email records into email records with same full name
3. Merge no-email internal duplicates (same full name, both no email)
4. Merge same full name + same personal email domain
5. Merge email-as-name records into proper records with that email

Name matching normalizes: case (UPPER/lower/Title), Greek accents (ά→α),
and whitespace. Two names match only if ALL words match after normalization.
"""

import re
import sqlite3
import unicodedata
from pathlib import Path

from src.store.transliterate import canonical_name

REPO_ROOT = Path(__file__).parent.parent.parent
DB_PATH = REPO_ROOT / "data" / "brain.db"

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
    """

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
    conn.execute("DELETE FROM people WHERE id = ?", (remove_id,))


def phase1_clean_names(conn: sqlite3.Connection) -> int:
    """Clean parenthesized metadata from names."""
    people = conn.execute("SELECT id, name FROM people").fetchall()
    count = 0
    for p in people:
        cleaned = clean_name(p["name"])
        if cleaned != p["name"]:
            conn.execute("UPDATE people SET name = ? WHERE id = ?", (cleaned, p["id"]))
            count += 1
    return count


def phase2_merge_no_email_exact(conn: sqlite3.Connection) -> int:
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
        match = email_by_norm.get(norm)
        if match and match["id"] != p["id"]:
            merge_person(conn, match["id"], p["id"])
            merged.add(p["id"])
            count += 1

    return count


def phase3_merge_no_email_dupes(conn: sqlite3.Connection) -> int:
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
        if len(members) < 2:
            continue
        best_name = pick_best_name([m["name"] for m in members])
        keep = members[0]
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (best_name, keep["id"]))
        for m in members[1:]:
            merge_person(conn, keep["id"], m["id"])
            count += 1

    return count


def phase4_merge_same_name_diff_email(conn: sqlite3.Connection) -> int:
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
        if len(members) < 2:
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
        for m in members[1:]:
            merge_person(conn, keep["id"], m["id"])
            count += 1

    return count


def phase5_merge_email_as_name(conn: sqlite3.Connection) -> int:
    """Merge/clean records where name IS the email address."""
    email_names = conn.execute(
        "SELECT id, name, email FROM people WHERE name LIKE '%@%'"
    ).fetchall()

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
        elif p["email"] and p["email"].lower() == name_email:
            # No match — convert email-as-name to readable form
            local = name_email.split("@")[0]
            parts = local.split(".")
            if len(parts) >= 2:
                readable = " ".join(part.capitalize() for part in parts)
                conn.execute("UPDATE people SET name = ? WHERE id = ?", (readable, p["id"]))
                count += 1

    return count


def phase6_normalize_best_names(conn: sqlite3.Connection) -> int:
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
        if len(members) < 2:
            continue
        best_name = pick_best_name([m["name"] for m in members])
        keep = members[0]
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (best_name, keep["id"]))
        for m in members[1:]:
            merge_person(conn, keep["id"], m["id"])
            count += 1

    return count


def phase7_merge_transliterated_variants(conn: sqlite3.Connection) -> int:
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
        if len(members) < 2:
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

    return count


def run_dedup(db_path: str | None = None, dry_run: bool = False) -> dict:
    """Run all deduplication phases."""
    conn = get_connection(db_path)

    before = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    results = {}

    print(f"Before: {before} people")
    print()

    print("Phase 1: Cleaning parenthesized metadata from names...")
    results["phase1_cleaned"] = phase1_clean_names(conn)
    print(f"  Cleaned {results['phase1_cleaned']} names")

    print("Phase 2: Merging no-email → email (normalized full name match)...")
    results["phase2_merged"] = phase2_merge_no_email_exact(conn)
    print(f"  Merged {results['phase2_merged']} records")

    print("Phase 3: Merging no-email internal duplicates...")
    results["phase3_merged"] = phase3_merge_no_email_dupes(conn)
    print(f"  Merged {results['phase3_merged']} records")

    print("Phase 4: Merging same-name personal email variants...")
    results["phase4_merged"] = phase4_merge_same_name_diff_email(conn)
    print(f"  Merged {results['phase4_merged']} records")

    print("Phase 5: Cleaning email-as-name records...")
    results["phase5_cleaned"] = phase5_merge_email_as_name(conn)
    print(f"  Cleaned/merged {results['phase5_cleaned']} records")

    print("Phase 6: Normalizing display names (best case/accent variant)...")
    results["phase6_normalized"] = phase6_normalize_best_names(conn)
    print(f"  Normalized {results['phase6_normalized']} records")

    print("Phase 7: Merging transliterated / reversed name variants...")
    results["phase7_merged"] = phase7_merge_transliterated_variants(conn)
    print(f"  Merged {results['phase7_merged']} records")

    after = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    results["before"] = before
    results["after"] = after
    results["total_reduced"] = before - after

    print()
    print(f"After: {after} people ({before - after} removed)")

    if dry_run:
        conn.rollback()
        print("DRY RUN — changes rolled back")
    else:
        conn.commit()
        print("Changes committed.")

    conn.close()
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deduplicate people table")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_dedup(args.db, args.dry_run)
