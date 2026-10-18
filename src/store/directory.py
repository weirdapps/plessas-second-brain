"""The organisation's directory: what it says of the owner's colleagues (schema v40).

`brain directory-sync` (src/pipeline/directory_sync.py) asks `teams-cli lookup-users`
about the people at the owner's own mail domains and keeps one directory_entries row per
address. A found entry also fills people.role (the job title) and people.department, so
every reader of the people row sees them; person_context adds the rest through
directory_card: office, company, whether the account is enabled, the manager and the
direct reports.

An entry is asked again once it is older than its refresh age: a month for one found, so
a move or a new manager shows within weeks, and three for an address the directory does
not know (a group mailbox, a former colleague), which rarely changes.
"""

import sqlite3
from datetime import datetime, timedelta

from src.store.owner import owner_addresses
from src.store.stamps import utc_now

REFRESH_OK_DAYS = 30
REFRESH_NOT_FOUND_DAYS = 90

# What directory_card lists of a manager's direct reports.
REPORTS_LIMIT = 20


def org_domains(pattern: str | None) -> list[str]:
    """The domains of the owner's whole addresses in BRAIN_USER_EMAIL_PATTERN: the
    organisation whose directory is asked. A bare local part names no domain."""
    addresses, _local_parts = owner_addresses(pattern)
    return sorted({address.partition("@")[2] for address in addresses})


def due_addresses(
    conn: sqlite3.Connection, domains, *, now: str | None = None, limit: int = 0
) -> list[str]:
    """The addresses at `domains` the directory should be asked about now, lower-cased.

    Never asked first, the most mailed of them first, then the oldest entries past their
    refresh age. `limit` 0 means all.
    """
    domains = sorted({d.lower() for d in domains if d})
    if not domains:
        return []
    stamp = datetime.fromisoformat(now or utc_now())
    ok_before = (stamp - timedelta(days=REFRESH_OK_DAYS)).isoformat()
    not_found_before = (stamp - timedelta(days=REFRESH_NOT_FOUND_DAYS)).isoformat()
    like = " OR ".join("LOWER(p.email) LIKE ?" for _ in domains)
    rows = conn.execute(
        f"""SELECT LOWER(TRIM(p.email)) AS address,
                   (SELECT COUNT(*) FROM email_people ep WHERE ep.person_id = p.id) AS mails
            FROM people p WHERE {like}""",
        [f"%@{d}" for d in domains],
    ).fetchall()
    mails: dict[str, int] = {}
    for address, count in rows:
        if address.partition("@")[2] in domains:
            mails[address] = mails.get(address, 0) + count
    checked = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT email, status, checked_at FROM directory_entries")
    }
    due: list[tuple[int, str, int, str]] = []
    for address, count in mails.items():
        if address not in checked:
            due.append((0, "", -count, address))
            continue
        status, checked_at = checked[address]
        cutoff = ok_before if status == "ok" else not_found_before
        if checked_at < cutoff:
            due.append((1, checked_at, -count, address))
    due.sort()
    picked = [address for *_rest, address in due]
    return picked[:limit] if limit > 0 else picked


def _text(value) -> str | None:
    """A directory field as stored: trimmed, and None for empty."""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def store_lookups(conn: sqlite3.Connection, users, *, now: str | None = None) -> dict:
    """Keep what `teams-cli lookup-users` answered, one entry per address, and fill the
    people rows at a found address. Returns the counts {"ok", "not_found", "error"}.

    A later answer replaces the entry. An error (throttled, or the item failed) writes
    nothing, so the address stays due. A field the directory leaves empty does not wipe a
    role or department a person row already holds.
    """
    stamp = now or utc_now()
    counts = {"ok": 0, "not_found": 0, "error": 0}
    for user in users:
        address = (user.get("query") or "").strip().lower()
        status = user.get("status")
        if not address or status not in ("ok", "not_found"):
            counts["error"] += 1
            continue
        counts[status] += 1
        if status == "not_found":
            conn.execute(
                "INSERT OR REPLACE INTO directory_entries (email, status, checked_at) "
                "VALUES (?, 'not_found', ?)",
                (address, stamp),
            )
            continue
        manager = user.get("manager") or {}
        manager_email = _text(manager.get("mail"))
        enabled = user.get("accountEnabled")
        title, department = _text(user.get("jobTitle")), _text(user.get("department"))
        conn.execute(
            """INSERT OR REPLACE INTO directory_entries
               (email, status, directory_id, display_name, job_title, department, office,
                company, account_enabled, manager_email, manager_name, manager_title,
                checked_at)
               VALUES (?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                address,
                _text(user.get("id")),
                _text(user.get("displayName")),
                title,
                department,
                _text(user.get("officeLocation")),
                _text(user.get("companyName")),
                None if enabled is None else int(bool(enabled)),
                manager_email.lower() if manager_email else None,
                _text(manager.get("displayName")),
                _text(manager.get("jobTitle")),
                stamp,
            ),
        )
        conn.execute(
            "UPDATE people SET role = COALESCE(?, role), department = COALESCE(?, department) "
            "WHERE LOWER(email) = ?",
            (title, department, address),
        )
    conn.commit()
    return counts


def directory_card(conn: sqlite3.Connection, email: str | None) -> dict | None:
    """What the directory says of the person at `email`: title, department, office,
    company, whether the account is enabled, the manager and the direct reports (enabled
    accounts only, by name, at most REPORTS_LIMIT, with their total). None when the
    directory has no entry for the address, or the store predates v40."""
    address = (email or "").strip().lower()
    if not address:
        return None
    try:
        row = conn.execute(
            "SELECT job_title, department, office, company, account_enabled, manager_email, "
            "manager_name, manager_title, checked_at FROM directory_entries "
            "WHERE email = ? AND status = 'ok'",
            (address,),
        ).fetchone()
    except sqlite3.OperationalError:  # a store without the v40 table
        return None
    if row is None:
        return None
    (title, department, office, company, enabled, m_email, m_name, m_title, checked_at) = row
    reports = conn.execute(
        "SELECT display_name, email, job_title FROM directory_entries "
        "WHERE manager_email = ? AND status = 'ok' AND COALESCE(account_enabled, 1) <> 0 "
        "ORDER BY display_name, email",
        (address,),
    ).fetchall()
    return {
        "title": title,
        "department": department,
        "office": office,
        "company": company,
        "active": None if enabled is None else bool(enabled),
        "manager": (
            {"name": m_name, "email": m_email, "title": m_title} if m_email or m_name else None
        ),
        "reports": [{"name": name, "email": mail, "title": job} for name, mail, job in reports][
            :REPORTS_LIMIT
        ],
        "reports_total": len(reports),
        "checked_at": checked_at,
    }
