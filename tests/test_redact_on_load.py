"""What the extractor writes is masked again as it is stored.

The model reads masked text since src/redact.py learned card numbers, IBANs and
passwords, but files and rows written before that are still read, and a model can
ignore its prompt. The audit found card numbers and IBANs in key facts, and
passwords lifted into them (security-privacy-01 and -07, code-extract-02), so the
decision, action item and key fact texts are masked on the way into every table.
The Teams and WhatsApp pipelines are covered beside their own tests.
"""

from src.store.loader import load_single_conversation, load_single_email, replace_extraction
from src.store.schema import create_database
from tests.payment_data import card, digits, grouped, iban, masked, printed

VISA = card("4", 16)
AMEX = card("37", 15)
IBAN = iban("GR", digits(23, 1))
MASKED_IBAN = f"GR[REDACTED:iban]{IBAN[-4:]}"

META = {
    "message_id": "m1",
    "date_received": "2026-10-01T09:00:00",
    "sender": {"name": "Sender", "address": "sender@example.com"},
    "subject": "payment details",
    "content": "see below",
    "mailbox": "Inbox",
    "to": [],
    "cc": [],
}


def _extraction(**lists) -> dict:
    base = {
        "summary": "s",
        "sentiment": "informational",
        "urgency": "low",
        "language": "english",
        "topics": [],
        "decisions": [],
        "action_items": [],
        "commitments": [],
        "people_roles": {},
        "key_facts": [],
    }
    return {**base, **lists}


def _texts(conn, sql: str) -> list[str]:
    return [row[0] for row in conn.execute(sql)]


SENSITIVE = _extraction(
    decisions=[{"decision": f"refund to IBAN {printed(IBAN)}", "decided_by": "ops"}, f"pay {VISA}"],
    action_items=[
        {"task": "Password: letmein to be reset", "owner": "it"},
        f"block {grouped(AMEX, (4, 6, 5))}",
    ],
    commitments=[{"commitment": f"send the card {VISA}", "by": "a", "to": "b"}],
    key_facts=[f"card {VISA} expires 12/27", f"IBAN {IBAN}", "Κωδικός πελάτη: 987654"],
)


class TestEmails:
    def test_decisions_actions_commitments_and_facts_are_masked(self):
        conn = create_database(":memory:")

        load_single_email(conn, META, SENSITIVE)

        assert _texts(conn, "SELECT decision FROM decisions ORDER BY id") == [
            f"refund to IBAN {MASKED_IBAN}",
            f"pay {masked(VISA)}",
        ]
        assert _texts(conn, "SELECT task FROM action_items ORDER BY id") == [
            "Password: [REDACTED:password] to be reset",
            f"block {masked(AMEX)}",
        ]
        assert _texts(conn, "SELECT commitment FROM commitments") == [
            f"send the card {masked(VISA)}"
        ]
        assert _texts(conn, "SELECT fact FROM key_facts ORDER BY id") == [
            f"card {masked(VISA)} expires 12/27",
            f"IBAN {MASKED_IBAN}",
            "Κωδικός πελάτη: 987654",
        ]

    def test_a_replaced_extraction_takes_its_masked_rows_with_it(self):
        """replace_extraction finds the rows `wrong` wrote by their text, and that
        text is now stored masked."""
        conn = create_database(":memory:")
        load_single_email(conn, META, SENSITIVE)
        (email_id,) = conn.execute("SELECT id FROM emails").fetchone()

        replace_extraction(
            conn, email_id, META, wrong=SENSITIVE, right=_extraction(key_facts=["the right fact"])
        )

        for table in ("decisions", "action_items", "commitments"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        assert _texts(conn, "SELECT fact FROM key_facts") == ["the right fact"]

    def test_rows_stored_unmasked_before_go_too(self):
        conn = create_database(":memory:")
        load_single_email(conn, META, _extraction())
        (email_id,) = conn.execute("SELECT id FROM emails").fetchone()
        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (email_id, f"IBAN {IBAN}")
        )

        replace_extraction(
            conn, email_id, META, wrong=_extraction(key_facts=[f"IBAN {IBAN}"]), right=_extraction()
        )

        assert conn.execute("SELECT COUNT(*) FROM key_facts").fetchone()[0] == 0


def test_a_conversation_is_masked_too():
    conn = create_database(":memory:")
    metadata = {
        "session_id": "s1",
        "started_at": "2026-10-01T09:00:00Z",
        "ended_at": "2026-10-01T10:00:00Z",
        "turn_count": 1,
        "turns": [{"speaker": "user", "content": "set it up", "timestamp": "2026-10-01T09:00:00Z"}],
    }
    extraction = {
        "summary": "s",
        "topics": [],
        "decisions": [{"decision": f"use the card {VISA}", "decided_by": "user"}],
        "action_items": [{"task": "rotate pwd=letmein", "owner": "user"}],
        "key_facts": [f"IBAN {IBAN}"],
        "preferences_expressed": [f"bill {VISA}"],
        "technical_decisions": ["password: hunter2 in the env file"],
    }

    assert load_single_conversation(conn, metadata, extraction)

    assert _texts(conn, "SELECT decision FROM decisions") == [f"use the card {masked(VISA)}"]
    assert _texts(conn, "SELECT task FROM action_items") == ["rotate pwd=[REDACTED:password]"]
    assert _texts(conn, "SELECT fact FROM key_facts ORDER BY id") == [
        f"IBAN {MASKED_IBAN}",
        f"[PREFERENCE] bill {masked(VISA)}",
        "[TECHNICAL] password: [REDACTED:password] in the env file",
    ]


def test_a_calendar_event_is_masked_too():
    from src.store.calendar_loader import load_event
    from tests.test_calendar_loader import SAMPLE_EVENT

    conn = create_database(":memory:")
    extraction = {
        "body_summary": "s",
        "decisions": [{"decision": f"refund to {IBAN}", "decided_by": "ops"}],
        "action_items": [{"task": f"cancel {grouped(VISA)}", "owner": "ops"}],
    }

    load_event(conn, SAMPLE_EVENT, extraction, llm_status="extracted")

    assert _texts(conn, "SELECT decision FROM decisions") == [f"refund to {MASKED_IBAN}"]
    assert _texts(conn, "SELECT task FROM action_items") == [f"cancel {masked(VISA)}"]


def test_an_attachment_summary_is_masked_too(tmp_path, monkeypatch):
    from tests.test_attachment_facts import EXTRACTION, _db, _summarise

    path, conn = _db(tmp_path)
    conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (1, ?)", (f"IBAN {MASKED_IBAN}",))
    conn.commit()

    _summarise(
        path,
        monkeypatch,
        {
            **EXTRACTION,
            "decisions": [{"decision": f"pay {VISA}", "decided_by": "board"}],
            "action_items": [{"task": "Passcode: abc123", "owner": "me"}],
            "key_facts": [f"IBAN {IBAN}", f"card {VISA}"],
        },
    )

    assert _texts(conn, "SELECT decision FROM decisions") == [f"pay {masked(VISA)}"]
    assert _texts(conn, "SELECT task FROM action_items") == ["Passcode: [REDACTED:password]"]
    # The email already held the masked IBAN fact, so it is not added twice.
    assert _texts(conn, "SELECT fact FROM key_facts ORDER BY id") == [
        f"IBAN {MASKED_IBAN}",
        f"card {masked(VISA)}",
    ]
