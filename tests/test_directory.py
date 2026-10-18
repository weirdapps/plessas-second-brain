"""The organisation's directory: title, department, office and manager for colleagues.

`brain directory-sync` looks up the people at the owner's own mail domains through
`teams-cli lookup-users` (Graph) and keeps one directory_entries row per address
(schema v40). person_context shows the entry, with the person's manager and direct
reports, and people.role and people.department carry the title and department.
Synthetic data only.
"""

from argparse import Namespace
from unittest.mock import patch

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.export.teams_cli import TeamsCliAuthRequired, TeamsCliError
from src.pipeline import EXIT_REAUTH, directory_sync
from src.store import directory
from src.store.context import get_person_context
from src.store.normalizer import find_or_create_person
from src.store.schema import (
    create_database,
    get_connection,
    get_schema_version,
    migrate_add_directory,
    run_migrations,
    set_schema_version,
)

NOW = "2026-10-18T12:00:00+00:00"
COLUMNS = [
    "email",
    "status",
    "directory_id",
    "display_name",
    "job_title",
    "department",
    "office",
    "company",
    "account_enabled",
    "manager_email",
    "manager_name",
    "manager_title",
    "checked_at",
]


def _ok(
    address: str,
    title: str | None = "Analyst",
    department: str | None = "Payments",
    manager: dict | None = None,
    enabled: bool = True,
) -> dict:
    return {
        "query": address,
        "status": "ok",
        "id": f"id-{address}",
        "displayName": address.split("@")[0].title(),
        "mail": address.upper(),
        "userPrincipalName": address,
        "jobTitle": title,
        "department": department,
        "officeLocation": "01234",
        "companyName": "Example Bank",
        "accountEnabled": enabled,
        "manager": manager,
    }


BOSS = {"displayName": "Bea Boss", "mail": "Bea.Boss@example.com", "jobTitle": "Director"}


@pytest.fixture
def conn():
    c = create_database(":memory:")
    yield c
    c.close()


# --- schema v40 ---


def test_schema_version_is_40():
    assert CURRENT_SCHEMA_VERSION == 40


def test_a_fresh_store_has_the_directory_table(conn):
    assert [r[1] for r in conn.execute("PRAGMA table_info(directory_entries)")] == COLUMNS
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(directory_entries)")}
    assert "idx_directory_manager" in indexes


def test_the_v40_migration_runs_once_and_again(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    c.execute("DROP TABLE directory_entries")
    set_schema_version(c, 39)
    run_migrations(c)
    assert get_schema_version(c) == 40
    migrate_add_directory(c)  # a second run changes nothing
    assert [r[1] for r in c.execute("PRAGMA table_info(directory_entries)")] == COLUMNS
    c.close()


# --- which addresses are looked up ---


def test_org_domains_are_the_domains_of_the_owners_whole_addresses():
    pattern = "first.last@example.com, alias@Example.com owner second@example.org"
    assert directory.org_domains(pattern) == ["example.com", "example.org"]
    assert directory.org_domains("") == []


def test_due_addresses_are_colleagues_never_looked_up_most_mailed_first(conn):
    quiet = find_or_create_person(conn, "Quiet Colleague", "quiet@example.com")
    busy = find_or_create_person(conn, "Busy Colleague", "Busy@Example.com")
    find_or_create_person(conn, "Outsider", "someone@elsewhere.org")
    find_or_create_person(conn, "Mentioned Only", None)
    for n in range(3):
        email_id = conn.execute(
            "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, 's')",
            (f"m{n}", "2026-10-01T10:00:00"),
        ).lastrowid
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'to')",
            (email_id, busy),
        )
    assert quiet != busy
    assert directory.due_addresses(conn, ["example.com"], now=NOW) == [
        "busy@example.com",
        "quiet@example.com",
    ]
    assert directory.due_addresses(conn, ["example.com"], now=NOW, limit=1) == ["busy@example.com"]
    assert directory.due_addresses(conn, [], now=NOW) == []


def test_an_address_that_could_read_as_a_flag_is_never_due(conn):
    # People rows come from mail headers, which anyone can write.
    for address in ("--help@example.com", "-x@example.com", "a b@example.com", "ok@example.com"):
        conn.execute("INSERT INTO people (name, email) VALUES ('x', ?)", (address,))
    assert directory.due_addresses(conn, ["example.com"], now=NOW) == ["ok@example.com"]


def test_a_recent_lookup_is_not_due_and_an_old_one_is(conn):
    for address in ("fresh@example.com", "old@example.com", "gone@example.com"):
        find_or_create_person(conn, address.split("@")[0], address)
    directory.store_lookups(
        conn,
        [
            _ok("fresh@example.com"),
            _ok("old@example.com"),
            {"query": "gone@example.com", "status": "not_found"},
        ],
        now="2026-10-01T00:00:00+00:00",
    )
    conn.execute(
        "UPDATE directory_entries SET checked_at = '2026-08-01T00:00:00+00:00' "
        "WHERE email = 'old@example.com'"
    )
    # 17 days after the lookup: an entry found is refreshed after 30 days, an
    # address the directory does not know after 90.
    assert directory.due_addresses(conn, ["example.com"], now=NOW) == ["old@example.com"]
    later = "2027-01-15T00:00:00+00:00"
    assert directory.due_addresses(conn, ["example.com"], now=later) == [
        "old@example.com",  # the oldest entry first
        "fresh@example.com",
        "gone@example.com",
    ]


# --- storing what the directory said ---


def test_store_lookups_keeps_the_entry_and_fills_the_people_row(conn):
    pid = find_or_create_person(conn, "Ann Analyst", "Ann@Example.com")
    counts = directory.store_lookups(conn, [_ok("ann@example.com", manager=BOSS)], now=NOW)
    assert counts == {"ok": 1, "not_found": 0, "error": 0}
    row = dict(
        conn.execute("SELECT * FROM directory_entries WHERE email = 'ann@example.com'").fetchone()
    )
    assert row["job_title"] == "Analyst"
    assert row["department"] == "Payments"
    assert row["office"] == "01234"
    assert row["account_enabled"] == 1
    assert row["manager_email"] == "bea.boss@example.com"
    assert row["manager_name"] == "Bea Boss"
    assert row["checked_at"] == NOW
    person = conn.execute("SELECT role, department FROM people WHERE id = ?", (pid,)).fetchone()
    assert tuple(person) == ("Analyst", "Payments")


def test_a_missing_title_does_not_wipe_a_known_role(conn):
    pid = find_or_create_person(conn, "Ann Analyst", "ann@example.com")
    conn.execute("UPDATE people SET role = 'Head of Cards' WHERE id = ?", (pid,))
    directory.store_lookups(conn, [_ok("ann@example.com", title=None)], now=NOW)
    assert conn.execute("SELECT role FROM people WHERE id = ?", (pid,)).fetchone()[0] == (
        "Head of Cards"
    )


def test_not_found_is_remembered_and_an_error_is_not(conn):
    pid = find_or_create_person(conn, "Group Mailbox", "group@example.com")
    counts = directory.store_lookups(
        conn,
        [
            {"query": "group@example.com", "status": "not_found"},
            {"query": "flaky@example.com", "status": "error", "error": "429"},
        ],
        now=NOW,
    )
    assert counts == {"ok": 0, "not_found": 1, "error": 1}
    rows = conn.execute("SELECT email, status, job_title FROM directory_entries").fetchall()
    assert [tuple(r) for r in rows] == [("group@example.com", "not_found", None)]
    assert conn.execute("SELECT role FROM people WHERE id = ?", (pid,)).fetchone()[0] is None


def test_a_later_lookup_replaces_the_entry(conn):
    directory.store_lookups(conn, [_ok("ann@example.com", title="Analyst")], now=NOW)
    directory.store_lookups(
        conn, [_ok("ann@example.com", title="Manager")], now="2026-11-20T00:00:00+00:00"
    )
    rows = conn.execute("SELECT job_title, checked_at FROM directory_entries").fetchall()
    assert [tuple(r) for r in rows] == [("Manager", "2026-11-20T00:00:00+00:00")]


# --- what person_context shows ---


def test_person_context_shows_the_directory_with_manager_and_reports(conn):
    find_or_create_person(conn, "Bea Boss", "bea.boss@example.com")
    directory.store_lookups(
        conn,
        [
            _ok("bea.boss@example.com", title="Director", department="Cards"),
            _ok("ann@example.com", manager=BOSS),
            _ok("carl@example.com", title="Officer", manager=BOSS),
            _ok("left@example.com", manager=BOSS, enabled=False),
        ],
        now=NOW,
    )
    ctx = get_person_context(conn, "bea.boss@example.com")
    person = ctx["person"]
    assert person["role"] == "Director"
    assert person["department"] == "Cards"
    card = person["directory"]
    assert card["title"] == "Director"
    assert card["office"] == "01234"
    assert card["manager"] is None
    assert [r["email"] for r in card["reports"]] == ["ann@example.com", "carl@example.com"]
    assert card["reports_total"] == 2
    assert card["checked_at"] == NOW


def test_person_context_names_the_manager(conn):
    find_or_create_person(conn, "Ann Analyst", "ann@example.com")
    directory.store_lookups(conn, [_ok("ann@example.com", manager=BOSS)], now=NOW)
    card = get_person_context(conn, "ann@example.com")["person"]["directory"]
    assert card["manager"] == {
        "name": "Bea Boss",
        "email": "bea.boss@example.com",
        "title": "Director",
    }


def test_no_directory_key_without_an_entry(conn):
    find_or_create_person(conn, "Outsider", "someone@elsewhere.org")
    assert "directory" not in get_person_context(conn, "someone@elsewhere.org")["person"]


def test_the_card_carries_department_and_manager(conn):
    from src.bridge import sender_brief

    find_or_create_person(conn, "Ann Analyst", "ann@example.com")
    directory.store_lookups(conn, [_ok("ann@example.com", manager=BOSS)], now=NOW)
    card = sender_brief(conn, "ann@example.com")
    assert card["role"] == "Analyst"
    assert card["department"] == "Payments"
    assert card["manager"] == "Bea Boss"


# --- the job ---


def _args(db, **kw):
    return Namespace(
        db=db, limit=kw.get("limit", 0), domain=kw.get("domain"), dry_run=kw.get("dry_run", False)
    )


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "brain.db")
    c = create_database(db)
    for n in range(5):
        find_or_create_person(c, f"Colleague {n}", f"c{n}@example.com")
    c.commit()
    c.close()
    return db


def _addresses(args):
    """The addresses of a lookup-users call, after the "--" that ends its options."""
    assert args[:3] == ["lookup-users", "--no-auto-reauth", "--"]
    return args[3:]


def _answer(args, timeout_sec=60):
    return {"users": [_ok(a) for a in _addresses(args)]}


def test_the_job_looks_up_due_colleagues_in_chunks(store, monkeypatch):
    monkeypatch.setattr(directory_sync, "CHUNK", 2)
    with patch.object(directory_sync, "run_teams_cli", side_effect=_answer) as cli:
        assert directory_sync.run(_args(store, domain=["example.com"])) == 0
    assert [len(_addresses(call.args[0])) for call in cli.call_args_list] == [2, 2, 1]
    c = get_connection(store)
    assert (
        c.execute("SELECT COUNT(*) FROM directory_entries WHERE status = 'ok'").fetchone()[0] == 5
    )
    c.close()


def test_the_job_takes_its_domains_from_the_owner_identity(store, monkeypatch):
    monkeypatch.setattr(directory_sync, "USER_EMAIL_PATTERN", "me@example.com")
    with patch.object(directory_sync, "run_teams_cli", side_effect=_answer) as cli:
        assert directory_sync.run(_args(store, limit=3)) == 0
    assert len(_addresses(cli.call_args_list[0].args[0])) == 3


def test_the_job_refuses_without_a_domain(store, monkeypatch, capsys):
    monkeypatch.setattr(directory_sync, "USER_EMAIL_PATTERN", "")
    with patch.object(directory_sync, "run_teams_cli") as cli:
        assert directory_sync.run(_args(store)) == 2
    cli.assert_not_called()
    assert "BRAIN_USER_EMAIL_PATTERN" in capsys.readouterr().err


def test_a_dry_run_looks_nothing_up(store):
    with patch.object(directory_sync, "run_teams_cli") as cli:
        assert directory_sync.run(_args(store, domain=["example.com"], dry_run=True)) == 0
    cli.assert_not_called()


def test_an_expired_sign_in_keeps_what_was_stored_and_asks_for_reauth(store, monkeypatch):
    monkeypatch.setattr(directory_sync, "CHUNK", 2)
    answers = iter(
        [_answer(["lookup-users", "--no-auto-reauth", "--", "c0@example.com", "c1@example.com"])]
    )

    def cli(args, timeout_sec=60):
        try:
            return next(answers)
        except StopIteration:
            raise TeamsCliAuthRequired("expired") from None

    with patch.object(directory_sync, "run_teams_cli", side_effect=cli):
        assert directory_sync.run(_args(store, domain=["example.com"])) == EXIT_REAUTH
    c = get_connection(store)
    assert c.execute("SELECT COUNT(*) FROM directory_entries").fetchone()[0] == 2
    c.close()


def test_an_upstream_failure_fails_the_run(store):
    with patch.object(
        directory_sync, "run_teams_cli", side_effect=TeamsCliError(5, "boom", retryable=True)
    ):
        assert directory_sync.run(_args(store, domain=["example.com"])) == 1
