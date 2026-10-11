"""Tool results stay under a character budget, and recall puts its flags first.

Claude Code saves a tool result longer than 50,000 characters to a file and
hands the model a path. recall crossed that line in a quarter of real calls
(37-45K characters at the default, 52-64K at the usual limit_per_kind=8), and
its freshness and semantic flags, appended last, were exactly what the 2 KB
preview of a saved result cut off. Other list tools reached 100-950K at their
row caps: rows were bounded, characters were not.
"""

import pytest

from src import mcp_server
from src.mcp_budget import RESPONSE_BUDGET_CHARS, budget_response, json_chars
from src.store.schema import create_database, get_connection

# ------------------------------------------------------------ budget_response


def _rows(n: int, width: int, tag: str = "r") -> list[dict]:
    return [{"id": i, "text": f"{tag}{i} " + "x" * width} for i in range(n)]


def test_a_result_that_fits_comes_back_unchanged():
    result = {"emails": _rows(3, 10), "summary": {"kinds": ["emails"]}}

    assert budget_response(result, 10_000) == result


def test_rows_are_cut_from_the_tail_until_the_json_fits():
    result = {"emails": _rows(40, 400)}

    out = budget_response(result, 5_000)

    assert json_chars(out) <= 5_000
    kept = out["truncated"]["emails"]["kept"]
    assert out["emails"] == result["emails"][:kept]
    assert out["truncated"]["emails"] == {"kept": kept, "total": 40}
    assert 0 < kept < 40


def test_the_heaviest_list_loses_rows_before_a_light_one_loses_its_few():
    result = {"heavy": _rows(30, 600, "h"), "light": _rows(2, 50, "l")}

    out = budget_response(result, 6_000)

    assert json_chars(out) <= 6_000
    assert out["light"] == result["light"]
    assert set(out["truncated"]) == {"heavy"}


def test_lists_inside_a_nested_dict_are_budgeted_and_named_by_path():
    result = {"person_context": {"name": "x", "recent_emails": _rows(30, 500)}}

    out = budget_response(result, 4_000)

    assert json_chars(out) <= 4_000
    assert out["person_context"]["name"] == "x"
    assert out["truncated"]["person_context.recent_emails"]["total"] == 30


def test_lists_of_plain_values_are_never_cut():
    result = {"summary": {"kinds_with_results": ["a" * 200] * 30}, "emails": _rows(20, 400)}

    out = budget_response(result, 9_000)

    assert out["summary"] == result["summary"]
    assert set(out["truncated"]) == {"emails"}


def test_the_input_is_not_modified():
    rows = _rows(40, 400)
    result = {"emails": rows}

    budget_response(result, 5_000)

    assert result == {"emails": rows} and len(rows) == 40


# ------------------------------------------------------------------- recall

LONG = " ".join(["the okapi programme"] + ["narrative"] * 300)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Ten of every kind about 'okapi', each row long: well past 40K at ten per kind."""
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO people (id, name, email) VALUES (1, 'Okapi Person', 'op@example.com')"
    )
    conn.execute("INSERT INTO topics (id, name, display_name) VALUES (1, 'okapi', 'Okapi')")
    for n in range(1, 11):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary, content, "
            "conversation_id, sender_name, sender_address, mailbox_name) VALUES "
            "(?, ?, ?, ?, ?, 'body', ?, 'Okapi Person', 'op@example.com', 'Inbox')",
            (n, n, f"2026-09-{n:02d}T10:00:00Z", f"Okapi plan {n}", LONG, f"conv{n}"),
        )
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, 1, 'sender')",
            (n,),
        )
        conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (?, 1)", (n,))
        conn.execute(
            "INSERT INTO decisions (email_id, decision, decided_by, decision_date) "
            "VALUES (?, ?, 'Okapi Person', ?)",
            (n, f"Decision {n}: {LONG}", f"2026-09-{n:02d}"),
        )
        conn.execute(
            "INSERT INTO action_items (email_id, task, owner, deadline, status) "
            "VALUES (?, ?, 'Okapi Person', '2026-12-01', 'open')",
            (n, f"Task {n}: {LONG}"),
        )
        conn.execute(
            "INSERT INTO commitments (email_id, commitment, by_person, to_person) "
            "VALUES (?, ?, 'a', 'b')",
            (n, f"Commitment {n}: {LONG}"),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))
    return path


def _stale(path):
    conn = get_connection(str(path))
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES ('last_sync_date', "
        "'2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()


def test_recall_fits_the_budget_and_says_what_it_cut(store):
    out = mcp_server.recall("okapi", limit_per_kind=10)

    assert json_chars(out) <= RESPONSE_BUDGET_CHARS
    assert out["truncated"]
    for bucket, record in out["truncated"].items():
        assert record["kept"] < record["total"]
        assert len(out[bucket]) == record["kept"]


def test_recall_leads_with_its_summary_and_freshness(store):
    _stale(store)

    out = mcp_server.recall("okapi", limit_per_kind=10)

    keys = list(out)
    assert keys[:4] == ["summary", "data_as_of", "stale", "_stale_warning"]
    assert keys.index("truncated") < keys.index("emails")
    assert next(iter(out["summary"])) == "semantic"
    assert out["stale"] is True


def test_a_fresh_recall_still_states_its_freshness(store):
    out = mcp_server.recall("okapi")

    assert list(out)[:3] == ["summary", "data_as_of", "stale"]
    assert out["stale"] is False
    assert "_stale_warning" not in out


def test_recall_rows_carry_a_short_summary_and_no_repeated_snippet(store):
    out = mcp_server.recall("okapi", limit_per_kind=3)

    for row in out["emails"]:
        assert len(row["summary"]) <= 301
        # A subject match's snippet is the subject; a summary match's, the summary.
        assert "snippet" not in row


def test_recall_attaches_the_dossiers_only_when_asked(store):
    plain = mcp_server.recall("okapi")
    full = mcp_server.recall("okapi", include_context=True)

    assert "person_context" not in plain and "topic_context" not in plain
    assert full["person_context"]["person"]["name"] == "Okapi Person"
    assert full["topic_context"]["topic"] is not None
    assert full["summary"]["has_person_context"] is True


def test_recall_holds_limit_per_kind_to_ten(store):
    out = mcp_server.recall("okapi", limit_per_kind=50)

    total = {bucket: r["total"] for bucket, r in out.get("truncated", {}).items()}
    for bucket in ("emails", "decisions", "actions", "commitments"):
        assert total.get(bucket, len(out[bucket])) <= 10


# --------------------------------------------------------------- other tools


def test_search_attachments_is_budgeted(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    for n in range(1, 101):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject) VALUES (?, ?, ?, 'x')",
            (n, n, "2026-09-01T10:00:00Z"),
        )
        conn.execute(
            "INSERT INTO attachments (id, email_id, message_id, filename, file_path, "
            "exported_at, mime_type, file_size) VALUES (?, ?, ?, ?, '/x', '2026-09-01', "
            "'application/pdf', 1)",
            (n, n, n, f"okapi{n}.pdf"),
        )
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status, "
            "summary) VALUES (?, ?, 'done', ?)",
            (n, LONG, LONG),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))

    out = mcp_server.search_attachments("okapi", limit=100)

    assert json_chars(out) <= RESPONSE_BUDGET_CHARS
    assert out["truncated"]["result"]["total"] == 100
    assert len(out["result"]) == out["truncated"]["result"]["kept"]


def test_a_calendar_event_lists_ten_attendees_and_counts_them_all(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO calendar_events (id, outlook_event_id, subject, start_at, end_at, "
        "is_recurring, is_self_organized, is_cancelled, ingested_at, llm_status, "
        "response_status) VALUES (1, 'ev1', 'Okapi review', '2026-09-01T10:00:00Z', "
        "'2026-09-01T11:00:00Z', 0, 0, 0, '2026-09-01', 'extracted', 'Accepted')"
    )
    for n in range(1, 31):
        name = "Zeta Late" if n == 25 else f"Attendee {n}"
        conn.execute(
            "INSERT INTO event_attendees (event_id, email, name, response_status, is_self) "
            "VALUES (1, ?, ?, 'None', 0)",
            (f"a{n}@example.com", name),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(mcp_server, "_get_conn", lambda: get_connection(str(path)))

    event = mcp_server.query_calendar_events(person="Zeta")["events"][0]

    assert event["attendees_total"] == 30
    assert len(event["attendees"]) == 10
    # The attendee the filter matched is listed, though 24 others came first.
    assert event["attendees"][0]["name"] == "Zeta Late"
    assert event["response_status"] == "Accepted"
