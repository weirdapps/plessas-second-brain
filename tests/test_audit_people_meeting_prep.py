"""meeting_prep lists an email, a decision or an action once.

Its attendee queries joined email_people, keyed by role too, so an email where
the person held two roles came back twice (the DISTINCT included the role), and
so did its decisions and actions. Its topic block joined every topic whose name
held the word, so an email tagged with two of them repeated each of its items
(embed-context-2). Synthetic data only.
"""

from src.store.normalizer import find_or_create_person, find_or_create_topic
from src.store.query import meeting_prep
from src.store.schema import create_database


def _store():
    conn = create_database(":memory:")
    pid = find_or_create_person(conn, "Jane Roe", "jane.roe@example.com")
    email_id = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary, sentiment) "
        "VALUES (1, '2026-09-20T10:00:00', 'cards plan', 's', 'informational')"
    ).lastrowid
    for role in ("recipient", "FYI"):
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
            (email_id, pid, role),
        )
    conn.execute(
        "INSERT INTO decisions (email_id, decision, decision_date) "
        "VALUES (?, 'go ahead', '2026-09-20')",
        (email_id,),
    )
    conn.execute(
        "INSERT INTO action_items (email_id, task) VALUES (?, 'send the deck')", (email_id,)
    )
    conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'budget is 2M')", (email_id,))
    for topic in ("Cards Migration", "Cards Issuing"):
        conn.execute(
            "INSERT INTO email_topics (email_id, topic_id) VALUES (?, ?)",
            (email_id, find_or_create_topic(conn, topic)),
        )
    conn.commit()
    return conn


def test_an_attendee_dossier_lists_each_item_once():
    result = meeting_prep(_store(), ["jane.roe@example.com"], days=100000)

    dossier = result["attendees"][0]
    assert len(dossier["emails"]) == 1
    assert dossier["emails"][0]["role"] in ("recipient", "FYI")
    assert dossier["sentiment_summary"] == {"informational": 1}
    assert len(dossier["decisions"]) == 1
    assert len(dossier["open_actions"]) == 1
    assert sorted(t["count"] for t in dossier["topics"]) == [1, 1]


def test_the_topic_block_lists_each_item_once():
    result = meeting_prep(_store(), ["jane.roe@example.com"], topic="cards", days=100000)

    topic_ctx = result["topic_context"]
    assert len(topic_ctx["decisions"]) == 1
    assert len(topic_ctx["key_facts"]) == 1
    assert len(topic_ctx["action_items"]) == 1
