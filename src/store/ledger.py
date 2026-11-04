"""The item ledger (gated migration item_ledger, issue 168): a lifecycle for decisions, actions and commitments
that outlives the rows extraction writes.

Every extraction path replaces its parent's items wholesale (the loader's replace_items, a
meeting, Teams thread, WhatsApp session, attachment or conversation extracted again), so a
row id lasts only until the next extraction, and what a row knew went with it: an action
marked done came back open, an expired one open again, a superseded decision current. The
ledger keeps that state by what does not change, the item's parent and the hash of its
normalised text:

- Each row gets `observed_at` (its parent's date: the email's received time, the meeting's
  start, the Teams or WhatsApp thread's first message, the conversation turn's time),
  `item_hash` (item_hash below), and on actions and commitments `due_date` (the deadline
  resolved to a day, resolve_due) and `owner_role` (from the schema v2 direction), plus
  `counterpart_person_id`, the people row on the other side of the item from the owner,
  and `counterpart_via`, how it was found: 'address' (a hard identifier) or 'name'.
- item_ledger holds one entry per (kind, parent, hash), the uniqueness rule, with its
  status: open, done, superseded, expired or dropped. A second row of one parent with the
  same hash is its exact duplicate: it gets canonical_id, logged in item_labels with method
  'ledger' as consolidation logs a label, so `brain maint consolidate --revert` undoes it.
- reconcile() runs after the writers: it stamps new rows, gives a new row the status its
  entry holds (done with what closed it, expired, superseded through invalid_at), records a
  row closed since (a closed entry is never reopened by a row, and done is never turned into
  expired), marks dropped the open entry whose item a new extraction no longer gives (it
  comes back open if the item returns), and deletes the entries of a parent that is gone,
  so `forget` leaves nothing behind. resync() follows a revert, the one way back to open.

Nothing here runs on a store without item_ledger: it is a gated migration (src/store/schema.py
GATED_MIGRATIONS), and
until the owner runs `brain maint migrate --apply-gated` every function is a no-op.
"""

import hashlib
import json
import re
import sqlite3
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta

from src.store.greek import search_fold

TABLES = ("decisions", "action_items", "commitments")
TEXT_COLUMN = {"decisions": "decision", "action_items": "task", "commitments": "commitment"}
STATUSES = ("open", "done", "superseded", "expired", "dropped")
CLOSED = ("done", "superseded", "expired")

# The columns the item_ledger migration adds: on every kind, and on the two that are owed by someone.
COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "decisions": (
        ("observed_at", "TEXT"),
        ("item_hash", "TEXT"),
        ("counterpart_person_id", "INTEGER"),
        ("counterpart_via", "TEXT"),
    ),
    "action_items": (
        ("observed_at", "TEXT"),
        ("item_hash", "TEXT"),
        ("due_date", "TEXT"),
        ("owner_role", "TEXT"),
        ("counterpart_person_id", "INTEGER"),
        ("counterpart_via", "TEXT"),
    ),
    "commitments": (
        ("observed_at", "TEXT"),
        ("item_hash", "TEXT"),
        ("due_date", "TEXT"),
        ("owner_role", "TEXT"),
        ("counterpart_person_id", "INTEGER"),
        ("counterpart_via", "TEXT"),
    ),
}

LEDGER_TABLE = """CREATE TABLE IF NOT EXISTS item_ledger (
    item_table TEXT NOT NULL CHECK (item_table IN ('decisions', 'action_items', 'commitments')),
    parent_key TEXT NOT NULL,
    item_hash TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('open', 'done', 'superseded', 'expired', 'dropped')),
    status_at TEXT NOT NULL,
    status_by TEXT,
    observed_at TEXT,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (item_table, parent_key, item_hash)
)"""

# The parent columns, most specific first: an attachment's items carry its email's id too.
_PARENTS = (
    ("attachment_id", "attachment", "attachments"),
    ("email_id", "email", "emails"),
    ("event_id", "event", "calendar_events"),
    ("teams_thread_id", "teams_thread", "teams_threads"),
    ("whatsapp_thread_id", "whatsapp_thread", "whatsapp_threads"),
    ("conversation_turn_id", "conversation_turn", "conversation_turns"),
)
_PARENT_DATE = {
    "email_id": "SELECT date_received FROM emails WHERE id = {a}.email_id",
    "event_id": "SELECT start_at FROM calendar_events WHERE id = {a}.event_id",
    "teams_thread_id": "SELECT started_at FROM teams_threads WHERE id = {a}.teams_thread_id",
    "whatsapp_thread_id": (
        "SELECT started_at FROM whatsapp_threads WHERE id = {a}.whatsapp_thread_id"
    ),
    "conversation_turn_id": (
        "SELECT timestamp FROM conversation_turns WHERE id = {a}.conversation_turn_id"
    ),
}
_ROLE_OF_DIRECTION = {
    "owner_owes": "assigned_to_owner",
    "owed_to_owner": "owner_assigned_other",
    "third_party": "third_party",
}
_BATCH = 5000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def enabled(conn: sqlite3.Connection) -> bool:
    """Whether the store took the gated item_ledger migration."""
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'item_ledger'"
        ).fetchone()
        is not None
    )


# --- the hash ------------------------------------------------------------------------------

_NOT_WORD = re.compile(r"[^\w]+")


def normalise(text: str | None) -> str:
    """The text an item is the same as: folded (case, Greek accents, final sigma), its
    punctuation gone and its spacing one space. Digits stay, so 'Pay 4,000' is not 'Pay
    5,000'."""
    return " ".join(_NOT_WORD.sub(" ", search_fold(text)).split())


def item_hash(text: str | None) -> str | None:
    """32 hex characters of the SHA-256 of normalise(text); None for an item with no words."""
    words = normalise(text)
    return hashlib.sha256(words.encode()).hexdigest()[:32] if words else None


# --- the due date --------------------------------------------------------------------------

_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
# An ISO date anywhere, unless it says where a span starts ('from 2026-01-27').
_ISO_ANYWHERE = re.compile(
    r"(?<!from )(?<!after )(?<!από )(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", re.IGNORECASE
)
# A deadline that recurs names no one day.
_RECURRING = frozenset(("every", "each", "weekly", "monthly", "daily", "καθε"))
# A day and month, with a year after any separator, without one only after / or . : '2-3
# days' is a span, not the 2nd of March. As for ISO dates, not where a span starts.
_DMY = re.compile(
    r"(?<!from )(?<!after )(?<!από )(?<![\d/.-])(\d{1,2})(?:[/.](\d{1,2})(?:[/.-](\d{4}|\d{2}))?|-(\d{1,2})-(\d{4}|\d{2}))"
    r"(?![\d/.-])",
    re.IGNORECASE,
)
_WEEKDAYS = {
    word: number
    for number, words in enumerate(
        (
            ("monday", "δευτερα"),
            ("tuesday", "τριτη"),
            ("wednesday", "τεταρτη"),
            ("thursday", "πεμπτη"),
            ("friday", "παρασκευη"),
            ("saturday", "σαββατο"),
            ("sunday", "κυριακη"),
        )
    )
    for word in words
}
_TODAY = ("today", "σημερα")
_TOMORROW = ("tomorrow", "αυριο")
_END_OF_MONTH = re.compile(r"\b(end of (the )?month|eom|τελοσ (του )?μηνα)\b")
_END_OF_WEEK = re.compile(r"\b(end of (the )?week|eow|τελοσ (τησ )?εβδομαδασ)\b")


def _day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def resolve_due(text: str | None, seen: date | None) -> str | None:
    """A deadline as written ('20/11', 'Friday', «αύριο», 'end of month', an ISO date
    anywhere in it), as an ISO day, read from `seen`, the day its parent was dated. None
    for what names no day ('ASAP', 'Q4', 'every Monday', an impossible date), and for any
    relative form without `seen`. A day and month that cannot be a date ('10.30', a time)
    is passed over.

    A day and month with no year is the next such day from `seen`, unless it fell within
    the month before (a deadline already passed when written); a weekday is the next one
    after `seen`; the end of the week is its Friday."""
    if not text:
        return None
    words = normalise(text)
    tokens = set(words.split())
    if tokens & _RECURRING:
        return None
    iso = _ISO_ANYWHERE.search(text)
    if iso:
        found = _day(int(iso[1]), int(iso[2]), int(iso[3]))
        return found.isoformat() if found else None
    for dmy in _DMY.finditer(text):
        day, month, year = int(dmy[1]), int(dmy[2] or dmy[4]), dmy[3] or dmy[5]
        if year:
            found = _day(int(year) + (2000 if len(year) == 2 else 0), month, day)
        elif seen is None:
            found = None
        else:
            found = _day(seen.year, month, day)
            if found and found < seen - timedelta(days=31):
                found = _day(seen.year + 1, month, day)
        if found:
            return found.isoformat()
    if seen is None:
        return None
    if tokens & set(_TODAY):
        return seen.isoformat()
    if tokens & set(_TOMORROW):
        return (seen + timedelta(days=1)).isoformat()
    if _END_OF_MONTH.search(words):
        return seen.replace(day=monthrange(seen.year, seen.month)[1]).isoformat()
    if _END_OF_WEEK.search(words):
        return (seen + timedelta(days=(4 - seen.weekday()) % 7)).isoformat()
    for token in words.split():
        if token in _WEEKDAYS:
            ahead = (_WEEKDAYS[token] - seen.weekday()) % 7 or 7
            return (seen + timedelta(days=ahead)).isoformat()
    return None


def _seen_day(value: str | None) -> date | None:
    match = _ISO.match(value or "")
    return _day(int(match[1]), int(match[2]), int(match[3])) if match else None


# --- parents -------------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _parents(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    have = _columns(conn, table)
    return [parent for parent in _PARENTS if parent[0] in have]


def parent_key_of(row) -> str:
    """'<kind>:<id>' of the row's parent (a mapping of its columns), 'item:<id>' for none."""
    for column, kind, _ in _PARENTS:
        value = row[column] if column in row.keys() else None
        if value is not None:
            return f"{kind}:{value}"
    return f"item:{row['id']}"


def _parent_sql(conn: sqlite3.Connection, table: str, alias: str) -> str:
    whens = " ".join(
        f"WHEN {alias}.{column} IS NOT NULL THEN '{kind}:' || {alias}.{column}"
        for column, kind, _ in _parents(conn, table)
    )
    return f"(CASE {whens} ELSE 'item:' || {alias}.id END)"


def _observed_sql(conn: sqlite3.Connection, table: str, alias: str) -> str:
    have = _columns(conn, table)
    dates = [f"({sql.format(a=alias)})" for column, sql in _PARENT_DATE.items() if column in have]
    if len(dates) > 1:
        return f"COALESCE({', '.join(dates)})"
    return dates[0] if dates else "NULL"


def _lifecycle_sql(table: str, alias: str) -> str:
    """A row's status in the ledger's words."""
    a = alias
    superseded = (
        f"WHEN {a}.superseded_by IS NOT NULL OR {a}.invalid_at IS NOT NULL THEN 'superseded'"
    )
    if table == "decisions":
        return f"(CASE {superseded} ELSE 'open' END)"
    return (
        f"(CASE WHEN {a}.status = 'done' THEN 'done' {superseded}"
        f" WHEN {a}.status = 'expired' THEN 'expired' ELSE 'open' END)"
    )


# --- who -----------------------------------------------------------------------------------


class _People:
    """The people rows an item's counterpart is read from, looked up once each."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        from src import config

        self.conn = conn
        self.user_name = config.USER_NAME
        self.pattern = config.USER_EMAIL_PATTERN
        self._names: dict[str, int | None] = {}
        self._addresses: dict[str, int | None] = {}

    def is_owner_name(self, name: str | None) -> bool:
        from src.store.owner import is_owner_name

        return is_owner_name(name, self.user_name)

    def by_name(self, name: str | None) -> int | None:
        """The one people row of that name, not the owner's; None for none or several."""
        if not name or "," in name or self.is_owner_name(name):
            return None
        key = name.strip().lower()
        if key not in self._names:
            rows = self.conn.execute(
                "SELECT id FROM people WHERE name = ? COLLATE NOCASE LIMIT 2", (name.strip(),)
            ).fetchall()
            self._names[key] = rows[0][0] if len(rows) == 1 else None
        return self._names[key]

    def address_of(self, person_id: int) -> str | None:
        """The address of a people row, lower case; None for a row with none."""
        row = self.conn.execute("SELECT email FROM people WHERE id = ?", (person_id,)).fetchone()
        return (row[0] or "").strip().lower() or None if row else None

    def by_address(self, address: str | None) -> int | None:
        """The people row of that address, not the owner's."""
        from src.store.owner import is_owner_address

        if not address or is_owner_address(address, self.pattern):
            return None
        key = address.strip().lower()
        if key not in self._addresses:
            row = self.conn.execute(
                "SELECT id FROM people WHERE email = ? COLLATE NOCASE", (address.strip(),)
            ).fetchone()
            self._addresses[key] = row[0] if row else None
        return self._addresses[key]


def _owner_role(table: str, row: dict, people: _People) -> str | None:
    role = _ROLE_OF_DIRECTION.get(row.get("direction") or "")
    if role or table != "action_items":
        return role
    return "assigned_to_owner" if people.is_owner_name(row.get("owner")) else None


def _counterpart(table: str, row: dict, people: _People) -> tuple[int | None, str | None]:
    """The people row on the other side of the item from the owner, and how it was found:
    'address' when a hard identifier names it (the parent email's From address, or a
    commitment party whose people row has that address), 'name' when only a name does.
    Only an 'address' counterpart may join items across threads (issue 170: merge on a
    hard identifier alone); a 'name' one is kept for display."""
    sender = row.get("sender_address")

    def via_row(person_id: int | None) -> tuple[int | None, str | None]:
        if person_id is None:
            return None, None
        hard = sender and people.address_of(person_id) == sender.strip().lower()
        return person_id, "address" if hard else "name"

    def named(person_id: int | None) -> tuple[int | None, str | None]:
        return person_id, "name" if person_id is not None else None

    def addressed(person_id: int | None) -> tuple[int | None, str | None]:
        return person_id, "address" if person_id is not None else None

    direction = row.get("direction")
    if table == "decisions":
        return named(people.by_name(row.get("decided_by")))
    if table == "commitments":
        if direction == "owner_owes":
            return via_row(row.get("to_person_id"))
        found = row.get("by_person_id") or row.get("to_person_id")
        if found or row.get("email_id"):
            return via_row(found)
        # A Teams or WhatsApp commitment names its people only: the one who is not the owner.
        by, to = row.get("by_person"), row.get("to_person")
        return named(people.by_name(to) if people.is_owner_name(by) else people.by_name(by))
    owner = row.get("owner")
    if direction == "owed_to_owner":
        return named(people.by_name(owner))
    if direction == "owner_owes" or people.is_owner_name(owner):
        return addressed(people.by_address(sender))
    by_name = people.by_name(owner)
    return named(by_name) if by_name else addressed(people.by_address(sender))


# --- reconcile -----------------------------------------------------------------------------


def _stamp(conn: sqlite3.Connection, table: str, people: _People) -> list[dict]:
    """Fill the ledger columns of the rows that have none; the rows stamped."""
    have = _columns(conn, table)
    wanted = [
        c
        for c in (
            "direction",
            "owner",
            "decided_by",
            "deadline",
            "due",
            "by_person_id",
            "to_person_id",
            "by_person",
            "to_person",
            "email_id",
            "status",
            "closed_by",
            "closed_at",
            "superseded_by",
            "invalid_at",
            "canonical_id",
        )
        if c in have
    ]
    sender = (
        "(SELECT sender_address FROM emails WHERE id = x.email_id)"
        if "email_id" in have
        else "NULL"
    )
    select = (
        f"SELECT x.id, x.{TEXT_COLUMN[table]} AS text, {_parent_sql(conn, table, 'x')} AS parent,"
        f" {_observed_sql(conn, table, 'x')} AS observed, {sender} AS sender_address,"
        f" {_lifecycle_sql(table, 'x')} AS lifecycle"
        + "".join(f", x.{c}" for c in wanted)
        + f" FROM {table} x WHERE x.item_hash IS NULL AND x.id > ? ORDER BY x.id LIMIT {_BATCH}"
    )
    stamped: list[dict] = []
    last = 0
    while True:
        cur = conn.execute(select, (last,))
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
        if not rows:
            return stamped
        last = rows[-1]["id"]
        updates = []
        for row in rows:
            row["item_hash"] = item_hash(row["text"]) or f"empty:{row['id']}"
            values = {
                "observed_at": row["observed"],
                "item_hash": row["item_hash"],
            }
            values["counterpart_person_id"], values["counterpart_via"] = _counterpart(
                table, row, people
            )
            if table != "decisions":
                deadline = row.get("deadline") if table == "action_items" else row.get("due")
                values["due_date"] = resolve_due(deadline, _seen_day(row["observed"]))
                values["owner_role"] = _owner_role(table, row, people)
            updates.append(values | {"id": row["id"]})
        sets = ", ".join(f"{c} = :{c}" for c in updates[0] if c != "id")
        conn.executemany(f"UPDATE {table} SET {sets} WHERE id = :id", updates)
        stamped.extend(rows)


def _pull(conn: sqlite3.Connection, table: str, row: dict, entry: tuple) -> bool:
    """Give a new row the closed status its entry holds; whether it changed."""
    status, status_at, status_by = entry
    if status == "superseded":
        conn.execute(f"UPDATE {table} SET invalid_at = ? WHERE id = ?", (status_at, row["id"]))
    elif status == "done" and table != "decisions":
        conn.execute(
            f"UPDATE {table} SET status = 'done', closed_by = ?, closed_at = ? WHERE id = ?",
            (status_by or "ledger", status_at, row["id"]),
        )
    elif status == "expired" and table == "action_items":
        conn.execute("UPDATE action_items SET status = 'expired' WHERE id = ?", (row["id"],))
    else:
        return False
    return True


def _label_duplicate(
    conn: sqlite3.Connection, table: str, row: dict, first: int, run_id: str
) -> None:
    """canonical_id of `row` set to `first`, logged in item_labels as consolidation logs a
    label (src/store/consolidate.py write_label), with the values it replaced as `prior`,
    so `brain maint consolidate --revert RUN_ID` puts it back."""
    prior = {"canonical_id": None, "superseded_by": row.get("superseded_by")}
    prior["invalid_at"] = row.get("invalid_at")
    if table != "decisions":
        prior |= {c: row.get(c) for c in ("status", "closed_by", "closed_at")}
    conn.execute(f"UPDATE {table} SET canonical_id = ? WHERE id = ?", (first, row["id"]))
    conn.execute(
        "INSERT INTO item_labels (item_table, item_id, label, target_id, evidence, prior,"
        " method, thread_key, run_id, created_at)"
        " VALUES (?, ?, 'duplicate_of', ?, 'same text in the same parent', ?, 'ledger', ?, ?, ?)",
        (table, row["id"], first, json.dumps(prior), row["parent"], run_id, _now()),
    )


def _new_rows(conn: sqlite3.Connection, table: str, stamped: list[dict], run_id: str) -> dict:
    out = {"inserted": 0, "restored": 0, "duplicates": 0, "reopened": 0}
    now = _now()
    for row in stamped:
        parent, digest = row["parent"], row["item_hash"]
        if row.get("canonical_id") is None:
            first = conn.execute(
                f"SELECT MIN(x.id) FROM {table} x WHERE x.item_hash = ? AND x.id < ?"
                f" AND {_parent_sql(conn, table, 'x')} = ? AND x.canonical_id IS NULL",
                (digest, row["id"], parent),
            ).fetchone()[0]
            if first is not None:
                _label_duplicate(conn, table, row, first, run_id)
                out["duplicates"] += 1
        entry = conn.execute(
            "SELECT status, status_at, status_by FROM item_ledger"
            " WHERE item_table = ? AND parent_key = ? AND item_hash = ?",
            (table, parent, digest),
        ).fetchone()
        if entry is None:
            conn.execute(
                "INSERT INTO item_ledger (item_table, parent_key, item_hash, status, status_at,"
                " status_by, observed_at, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    table,
                    parent,
                    digest,
                    row["lifecycle"],
                    row.get("closed_at") or row.get("invalid_at") or now,
                    row.get("closed_by"),
                    row["observed"],
                    now,
                ),
            )
            out["inserted"] += 1
        elif entry[0] == "dropped":
            conn.execute(
                "UPDATE item_ledger SET status = ?, status_at = ?, status_by = NULL"
                " WHERE item_table = ? AND parent_key = ? AND item_hash = ?",
                (row["lifecycle"], now, table, parent, digest),
            )
            out["reopened"] += 1
        elif entry[0] in CLOSED and row["lifecycle"] == "open" and _pull(conn, table, row, entry):
            out["restored"] += 1
    return out


def _push(conn: sqlite3.Connection, table: str) -> int:
    """Record the rows closed since their entry was written. A closed entry changes only to
    another closed status, and done never to expired."""
    rows = conn.execute(
        f"SELECT l.parent_key, l.item_hash, {_lifecycle_sql(table, 'x')},"
        f" COALESCE({'x.closed_at' if table != 'decisions' else 'NULL'}, x.invalid_at),"
        f" {'x.closed_by' if table != 'decisions' else 'NULL'}"
        f" FROM {table} x JOIN item_ledger l ON l.item_table = ?"
        f" AND l.parent_key = {_parent_sql(conn, table, 'x')} AND l.item_hash = x.item_hash"
        f" WHERE {_lifecycle_sql(table, 'x')} <> 'open' AND l.status <> {_lifecycle_sql(table, 'x')}"
        f" AND NOT (l.status = 'done' AND {_lifecycle_sql(table, 'x')} = 'expired')",
        (table,),
    ).fetchall()
    now = _now()
    for parent, digest, status, at, by in rows:
        conn.execute(
            "UPDATE item_ledger SET status = ?, status_at = ?, status_by = ?"
            " WHERE item_table = ? AND parent_key = ? AND item_hash = ?",
            (status, at or now, by, table, parent, digest),
        )
    return len(rows)


def _drop(conn: sqlite3.Connection, table: str) -> int:
    """Mark dropped the open entries no row of their parent gives any more."""
    cur = conn.execute(
        f"UPDATE item_ledger SET status = 'dropped', status_at = ?, status_by = NULL"
        f" WHERE item_table = ? AND status = 'open' AND NOT EXISTS (SELECT 1 FROM {table} x"
        f" WHERE x.item_hash = item_ledger.item_hash"
        f" AND {_parent_sql(conn, table, 'x')} = item_ledger.parent_key)",
        (_now(), table),
    )
    return cur.rowcount


def _forget(conn: sqlite3.Connection) -> int:
    """Delete the entries whose parent no longer exists."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    gone = 0
    for _, kind, parent_table in _PARENTS:
        prefix = f"{kind}:"
        if parent_table in tables:
            exists = f"SELECT 1 FROM {parent_table} p WHERE p.id = CAST(substr(parent_key, ?) AS INTEGER)"
        else:
            exists = "SELECT 1 WHERE 0 AND ?"
        gone += conn.execute(
            f"DELETE FROM item_ledger WHERE substr(parent_key, 1, ?) = ? AND NOT EXISTS ({exists})",
            (len(prefix), prefix, len(prefix) + 1),
        ).rowcount
    for table in TABLES:
        gone += conn.execute(
            f"DELETE FROM item_ledger WHERE item_table = ? AND substr(parent_key, 1, 5) = 'item:'"
            f" AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.id = CAST(substr(parent_key, 6) AS INTEGER))",
            (table,),
        ).rowcount
    return gone


def reconcile(conn: sqlite3.Connection) -> dict:
    """Bring rows and ledger into step (see the module docstring). Returns its counts;
    {'enabled': False} on a store without the ledger. The caller commits."""
    if not enabled(conn):
        return {"enabled": False}
    run_id = f"ledger-{uuid.uuid4().hex[:12]}"
    people = _People(conn)
    counts = dict.fromkeys(
        ("stamped", "inserted", "restored", "duplicates", "reopened", "recorded", "dropped"), 0
    )
    counts["forgotten"] = _forget(conn)
    for table in TABLES:
        stamped = _stamp(conn, table, people)
        counts["stamped"] += len(stamped)
        for key, value in _new_rows(conn, table, stamped, run_id).items():
            counts[key] += value
        counts["recorded"] += _push(conn, table)
        counts["dropped"] += _drop(conn, table)
    return {"enabled": True, "run_id": run_id, **counts}


def resync(conn: sqlite3.Connection, table: str, ids) -> int:
    """Set the entries of rows `ids` of `table` to what the rows say now, open included:
    after a revert put labels back. The caller commits."""
    if table not in TABLES or not enabled(conn):
        return 0
    changed = 0
    for item_id in {int(i) for i in ids if i is not None}:
        changed += conn.execute(
            f"UPDATE item_ledger SET status = (SELECT {_lifecycle_sql(table, 'x')} FROM {table} x"
            f" WHERE x.id = ?), status_at = ?, status_by = NULL"
            f" WHERE item_table = ? AND (parent_key, item_hash) IN (SELECT"
            f" {_parent_sql(conn, table, 'x')}, x.item_hash FROM {table} x WHERE x.id = ?)",
            (item_id, _now(), table, item_id),
        ).rowcount
    return changed


# Gated migration commitment_parents: the parents a commitment can have besides an email (issue 168).
COMMITMENT_PARENTS = (
    ("teams_thread_id", "teams_threads"),
    ("whatsapp_thread_id", "whatsapp_threads"),
)


def migrate_commitment_parents(conn: sqlite3.Connection) -> None:
    """Gated migration commitment_parents: commitments from Teams threads and WhatsApp sessions, under a parent column of
    their own as decisions and actions have. ADD COLUMN with no default and two indexes on
    columns that start empty: no row rewritten, milliseconds. The caller commits."""
    have = _columns(conn, "commitments")
    for column, parent in COMMITMENT_PARENTS:
        if column not in have:
            conn.execute(
                f"ALTER TABLE commitments ADD COLUMN {column} INTEGER REFERENCES {parent}(id)"
            )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_commitments_{column} ON commitments({column})"
        )


def takes_thread_commitments(conn: sqlite3.Connection, column: str) -> bool:
    """Whether the store holds commitments under `column` (gated commitment_parents), so a Teams or WhatsApp
    extraction asks for them."""
    return column in dict(COMMITMENT_PARENTS) and column in _columns(conn, "commitments")


def _field(item: dict, key: str) -> str | None:
    """A name or date of an extracted item as text: a list joined, blank as None."""
    value = item.get(key)
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return (str(value).strip() or None) if value else None


def write_thread_commitments(
    conn: sqlite3.Connection, column: str, thread_id: int, commitments
) -> int:
    """Replace the commitments of a Teams thread or WhatsApp session (`column` its parent
    column) with `commitments`, the extraction's list; the rows written. Nothing on a store
    without that gated migration, which has no such column: its thread extractions keep writing none. The
    texts are masked as the loader masks an email's. The caller holds the transaction."""
    from src.redact import redact_secrets

    if not takes_thread_commitments(conn, column):
        return 0
    conn.execute(f"DELETE FROM commitments WHERE {column} = ?", (thread_id,))
    written = 0
    for item in commitments if isinstance(commitments, list) else []:
        if not isinstance(item, dict) or not str(item.get("commitment") or "").strip():
            continue

        conn.execute(
            f"INSERT INTO commitments (commitment, by_person, to_person, due, status, {column})"
            " VALUES (?, ?, ?, ?, 'open', ?)",
            (
                redact_secrets(str(item["commitment"])),
                _field(item, "by"),
                _field(item, "to"),
                _field(item, "due"),
                thread_id,
            ),
        )
        written += 1
    return written


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """What the gated steps change, as counts a person can compare before and after: rows,
    labels by method, duplicates marked, action statuses, ledger entries by status."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    out: dict[str, int] = {}
    for table in TABLES:
        out[f"{table} rows"] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out[f"{table} canonical_id set"] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE canonical_id IS NOT NULL"
        ).fetchone()[0]
    for status, n in conn.execute(
        "SELECT COALESCE(status, 'null'), COUNT(*) FROM action_items GROUP BY 1 ORDER BY 1"
    ):
        out[f"action_items status {status}"] = n
    if "item_labels" in tables:
        out["item_labels rows"] = conn.execute("SELECT COUNT(*) FROM item_labels").fetchone()[0]
        for method, n in conn.execute(
            "SELECT method, COUNT(*) FROM item_labels GROUP BY 1 ORDER BY 1"
        ):
            out[f"item_labels method {method}"] = n
    if "item_ledger" in tables:
        for table, status, n in conn.execute(
            "SELECT item_table, status, COUNT(*) FROM item_ledger GROUP BY 1, 2 ORDER BY 1, 2"
        ):
            out[f"item_ledger {table} {status}"] = n
    return out


def migrate(conn: sqlite3.Connection) -> None:
    """Gated migration item_ledger: the columns, the ledger and its indexes, then every stored row stamped and
    entered (reconcile). ADD COLUMN with no default rewrites no row; the stamping is one
    UPDATE per row, which touches no full-text index (each table's update trigger fires on
    its text column alone). The caller commits (schema.apply_gated), or rolls back for a
    dry run."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table, columns in COLUMNS.items():
        if table not in tables:
            continue
        have = _columns(conn, table)
        for name, declaration in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_item_hash ON {table}(item_hash)")
    conn.execute(LEDGER_TABLE)
    reconcile(conn)
