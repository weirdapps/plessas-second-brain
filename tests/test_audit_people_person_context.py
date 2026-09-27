"""person_context counts an email once, however many roles the person holds on it.

email_people is keyed (email_id, person_id, role_in_email), and the model writes
free-text roles, so about 30% of (email, person) pairs have several rows. Every
list and total but email_count joined through it, so the top correspondent's
decisions_total read 132K against 59K real, the sentiment counts summed to twice
email_count, and the capped lists were about half repeats (embed-context-1,
search-6). Synthetic data only.
"""

from src.store.context import get_person_context
from src.store.normalizer import find_or_create_person, find_or_create_topic
from src.store.schema import create_database


def _email(conn, message_id, date="2026-09-20T10:00:00"):
    return conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary, sentiment) "
        "VALUES (?, ?, ?, 's', 'informational')",
        (message_id, date, f"subject {message_id}"),
    ).lastrowid


def _link(conn, email_id, person_id, *roles):
    for role in roles:
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
            (email_id, person_id, role),
        )


def _one_email_two_roles():
    conn = create_database(":memory:")
    pid = find_or_create_person(conn, "Jane Roe", "jane.roe@example.com")
    email_id = _email(conn, 1)
    _link(conn, email_id, pid, "recipient", "FYI")
    conn.execute(
        "INSERT INTO decisions (email_id, decision, decision_date) "
        "VALUES (?, 'go ahead', '2026-09-20')",
        (email_id,),
    )
    conn.execute(
        "INSERT INTO action_items (email_id, task) VALUES (?, 'send the deck')", (email_id,)
    )
    topic_id = find_or_create_topic(conn, "Cards Migration")
    conn.execute(
        "INSERT INTO email_topics (email_id, topic_id) VALUES (?, ?)", (email_id, topic_id)
    )
    conn.commit()
    return conn


def test_an_email_with_two_roles_counts_once():
    conn = _one_email_two_roles()

    ctx = get_person_context(conn, "jane.roe@example.com", days=100000)

    assert ctx["email_count"] == 1
    assert len(ctx["recent_emails"]) == 1
    assert ctx["recent_emails"][0]["role_in_email"] in ("recipient", "FYI")
    assert ctx["topics_total"] == 1
    assert ctx["topics"] == [{"topic": "Cards Migration", "count": 1}]
    assert sum(ctx["sentiment_distribution"].values()) == 1
    assert ctx["decisions_total"] == 1
    assert len(ctx["decisions"]) == 1
    assert ctx["open_actions_total"] == 1
    assert len(ctx["open_actions"]) == 1


def test_the_role_shown_is_sender_first():
    conn = create_database(":memory:")
    pid = find_or_create_person(conn, "Jane Roe", "jane.roe@example.com")
    _link(conn, _email(conn, 1), pid, "cc", "sender", "approver")

    ctx = get_person_context(conn, "jane.roe@example.com", days=100000)

    assert [e["role_in_email"] for e in ctx["recent_emails"]] == ["sender"]


def test_recent_emails_honour_the_limit_up_to_ten():
    conn = create_database(":memory:")
    pid = find_or_create_person(conn, "Jane Roe", "jane.roe@example.com")
    for n in range(1, 13):
        _link(conn, _email(conn, n, f"2026-09-{n:02d}T10:00:00"), pid, "recipient")

    assert len(get_person_context(conn, "jane.roe@example.com", 100000, 5)["recent_emails"]) == 5
    assert len(get_person_context(conn, "jane.roe@example.com", 100000, 20)["recent_emails"]) == 10
