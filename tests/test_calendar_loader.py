"""Tests for calendar event loading and UPSERT logic."""

import json
import sqlite3

import pytest

from src.store.calendar_loader import (
    _is_self_organized,
    _resolve_person_id,
    load_event,
    load_proxy_emails,
)
from src.store.schema import run_migrations

SAMPLE_EVENT = {
    "outlook_event_id": "evt-001",
    "subject": "Weekly sync",
    "organizer_email": "nikos.papadopoulos@example.com",
    "organizer_name": "Papadopoulos",
    "start_at": "2026-05-01T10:00:00",
    "end_at": "2026-05-01T11:00:00",
    "location": "Room A",
    "is_recurring": False,
    "recurrence_master_id": None,
    "response_status": "Accepted",
    "is_cancelled": False,
    "created_at": "2026-04-01T09:00:00Z",
    "modified_at": "2026-04-28T14:00:00Z",
    "attendees": [
        {
            "email": "nikos.papadopoulos@example.com",
            "name": "Papadopoulos",
            "response_status": "Accepted",
        },
        {
            "email": "chen@example.com",
            "name": "Chen",
            "response_status": "Accepted",
        },
    ],
}

SAMPLE_EXTRACTION = {
    "body_summary": "Discussed Q2 targets",
    "decisions": [
        {
            "decision": "Ship by June 1",
            "decided_by": "Papadopoulos",
            "decision_date": "2026-05-01",
        }
    ],
    "action_items": [{"task": "Draft timeline", "owner": "Chen"}],
}


@pytest.fixture
def db_conn(tmp_path):
    """Create a fresh database with all tables including migrations."""
    db_path = tmp_path / "test_calendar.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Create minimal base schema
    conn.execute("""
        CREATE TABLE people (
            id INTEGER PRIMARY KEY,
            name TEXT,
            email TEXT,
            role TEXT,
            department TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY,
            email_id INTEGER,
            decision TEXT NOT NULL,
            decided_by TEXT,
            decision_date TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE action_items (
            id INTEGER PRIMARY KEY,
            email_id INTEGER,
            task TEXT NOT NULL,
            owner TEXT,
            deadline TEXT,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version (version) VALUES (12)")
    conn.commit()

    # Run migrations to get to v13 (calendar tables)
    run_migrations(conn)

    yield conn
    conn.close()


@pytest.fixture
def proxy_config(tmp_path):
    """Create a canonical_people.json with proxy flag."""
    config_path = tmp_path / "canonical_people.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "canonical_name": "Dana Duarte",
                    "email": "duarte.dana@example.com",
                    "role": "PA",
                    "department": "AGM Office",
                    "is_proxy_for_self": True,
                }
            ]
        )
    )
    return str(config_path)


def test_load_proxy_emails(proxy_config):
    """Load proxy emails from canonical_people.json."""
    emails = load_proxy_emails(proxy_config)
    assert "duarte.dana@example.com" in emails
    assert len(emails) == 1


def test_load_proxy_emails_missing_file():
    """Return empty set if file doesn't exist."""
    emails = load_proxy_emails("/nonexistent/path.json")
    assert emails == set()


def test_is_self_organized_direct_match():
    """User email pattern matches organizer."""
    assert _is_self_organized("nikos.papadopoulos@example.com", "papadopoulos") is True
    assert _is_self_organized("nikos.papadopoulos@example.com", "PAPADOPOULOS") is True
    assert _is_self_organized("other@example.com", "papadopoulos") is False


def test_is_self_organized_via_proxy():
    """Proxy email is treated as self-organized."""
    proxy_emails = {"duarte.dana@example.com"}
    assert _is_self_organized("duarte.dana@example.com", "papadopoulos", proxy_emails) is True
    assert _is_self_organized("duarte.dana@example.com", "papadopoulos", proxy_emails) is True
    assert _is_self_organized("other@example.com", "papadopoulos", proxy_emails) is False


def test_resolve_person_id(db_conn):
    """Resolve person_id from email."""
    # Insert a test person
    db_conn.execute(
        "INSERT INTO people (name, email, role) VALUES (?, ?, ?)",
        ("Bella Chen", "chen@example.com", "Director"),
    )
    db_conn.commit()

    person_id = _resolve_person_id(db_conn, "chen@example.com")
    assert person_id is not None

    # Case-insensitive
    person_id_upper = _resolve_person_id(db_conn, "CHEN@EXAMPLE.COM")
    assert person_id_upper == person_id

    # Unknown email
    unknown_id = _resolve_person_id(db_conn, "unknown@example.com")
    assert unknown_id is None


def test_load_event_creates_rows(db_conn):
    """Load event creates calendar_events, attendees, and decisions."""
    # Insert attendees into people table first
    db_conn.execute(
        "INSERT INTO people (name, email, role) VALUES (?, ?, ?)",
        ("Nikos Papadopoulos", "nikos.papadopoulos@example.com", "AGM"),
    )
    db_conn.execute(
        "INSERT INTO people (name, email, role) VALUES (?, ?, ?)",
        ("Bella Chen", "chen@example.com", "Director"),
    )
    db_conn.commit()

    event_id = load_event(
        db_conn,
        SAMPLE_EVENT,
        SAMPLE_EXTRACTION,
        user_email_pattern="papadopoulos",
        llm_status="extracted",
    )

    # Verify calendar_events row
    event_row = db_conn.execute(
        "SELECT subject, is_self_organized, body_summary FROM calendar_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    assert event_row is not None
    assert event_row[0] == "Weekly sync"
    assert event_row[1] == 1  # is_self_organized
    assert event_row[2] == "Discussed Q2 targets"

    # Verify attendees
    attendees = db_conn.execute(
        "SELECT person_id, is_self, is_organizer FROM event_attendees WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    assert len(attendees) == 2

    # Verify decision
    decision_row = db_conn.execute(
        "SELECT decision, decided_by, event_id FROM decisions WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    assert decision_row is not None
    assert decision_row[0] == "Ship by June 1"
    assert decision_row[1] == "Papadopoulos"
    assert decision_row[2] == event_id


def test_upsert_updates_modified(db_conn):
    """UPSERT updates existing event on conflict."""
    db_conn.execute(
        "INSERT INTO people (name, email, role) VALUES (?, ?, ?)",
        ("Nikos Papadopoulos", "nikos.papadopoulos@example.com", "AGM"),
    )
    db_conn.execute(
        "INSERT INTO people (name, email, role) VALUES (?, ?, ?)",
        ("Bella Chen", "chen@example.com", "Director"),
    )
    db_conn.commit()

    # First load
    event_id_1 = load_event(
        db_conn,
        SAMPLE_EVENT,
        SAMPLE_EXTRACTION,
        user_email_pattern="papadopoulos",
        llm_status="extracted",
    )

    # Second load with updated subject and modified_at
    updated_event = SAMPLE_EVENT.copy()
    updated_event["subject"] = "Weekly sync (UPDATED)"
    updated_event["modified_at"] = "2026-05-01T10:00:00Z"

    event_id_2 = load_event(
        db_conn,
        updated_event,
        SAMPLE_EXTRACTION,
        user_email_pattern="papadopoulos",
        llm_status="extracted",
    )

    # Should be same event_id
    assert event_id_1 == event_id_2

    # Verify only 1 row exists
    count = db_conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]
    assert count == 1

    # Verify updated subject
    subject = db_conn.execute(
        "SELECT subject FROM calendar_events WHERE id = ?", (event_id_1,)
    ).fetchone()[0]
    assert subject == "Weekly sync (UPDATED)"


def test_proxy_organizer(db_conn, proxy_config):
    """Event organized by proxy is marked as self-organized."""
    # Insert people
    db_conn.execute(
        "INSERT INTO people (name, email, role) VALUES (?, ?, ?)",
        ("Dana Duarte", "duarte.dana@example.com", "PA"),
    )
    db_conn.execute(
        "INSERT INTO people (name, email, role) VALUES (?, ?, ?)",
        ("Bella Chen", "chen@example.com", "Director"),
    )
    db_conn.commit()

    # Event organized by Politis
    proxy_event = SAMPLE_EVENT.copy()
    proxy_event["organizer_email"] = "duarte.dana@example.com"
    proxy_event["organizer_name"] = "Duarte"

    proxy_emails = load_proxy_emails(proxy_config)
    event_id = load_event(
        db_conn,
        proxy_event,
        SAMPLE_EXTRACTION,
        user_email_pattern="papadopoulos",
        proxy_emails=proxy_emails,
        llm_status="extracted",
    )

    # Verify is_self_organized = 1
    is_self_organized = db_conn.execute(
        "SELECT is_self_organized FROM calendar_events WHERE id = ?", (event_id,)
    ).fetchone()[0]
    assert is_self_organized == 1
