"""sql_query is a third of all brain calls, and it was easy to get wrong.

In six weeks 7% of its calls failed. 50 of those were "no such column", 30 of them a
guessed `date` or `e.date` for emails.date_received, and in 34 of the 50 the agent
had not called sql_schema. Its description named the full-text columns but no date,
sender or parent column, and steered text search to `sb_fold(...) LIKE '%...%'`:
agents ran 274 such scans against 2 full-text MATCH queries. Results reached 100,000
characters, past the 50,000 at which Claude Code saves a result to a file.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from src import config, mcp_server
from src.mcp_budget import RESPONSE_BUDGET_CHARS, json_chars
from src.store import sql_readonly
from tests.mcp_listing import advertised_text


@pytest.fixture(autouse=True)
def _no_pull_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPLICA_STAMP", tmp_path / "absent-db-pull.stamp")


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.store.schema import create_database

    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.executescript(
        """
        INSERT INTO emails (id, message_id, date_received, subject, summary, sender_name,
                            sender_address, mailbox_name, content, source_class) VALUES
            (1, 1, '2026-03-01T10:00:00Z', 'Νέες κάρτες', 'first', 'Alice',
             'a@example.com', 'Inbox', 'Οι κάρτες ήρθαν', 'mail'),
            (2, 2, '2026-03-02T10:00:00Z', 'Budget plan', 'second', 'Bob',
             'b@example.com', 'Inbox', 'numbers', 'mail');
        INSERT INTO attachments (id, email_id, message_id, filename, file_path, exported_at,
                                 mime_type, file_size)
            VALUES (7, 2, 2, 'plan.pdf', '/x', '2026-03-02', 'application/pdf', 1);
        INSERT INTO attachment_content (id, attachment_id, extracted_text, extraction_status)
            VALUES (70, 7, 'the okapi figures for 2027', 'done');
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(sql_readonly, "DEFAULT_DB", path)
    monkeypatch.setattr(mcp_server, "DEFAULT_DB", path)
    return path


# ------------------------------------------------------------ "no such column"


def _error(sql: str) -> str:
    with pytest.raises(ToolError) as caught:
        mcp_server.sql_query(sql=sql)
    return str(caught.value)


def test_a_guessed_date_column_is_answered_with_the_real_one(db):
    text = _error("SELECT date, subject FROM emails ORDER BY date DESC")

    assert "no such column: date" in text
    assert "date_received" in text
    # The table's real columns, so the next call needs no sql_schema round trip.
    for column in ("sender_address", "mailbox_name", "source_class"):
        assert column in text


def test_a_qualified_guess_lists_the_columns_of_its_alias(db):
    text = _error("SELECT e.date, a.filename FROM emails e JOIN attachments a ON a.email_id = e.id")

    assert "date_received" in text
    assert "emails" in text


def test_a_wrong_column_of_a_joined_table_names_that_table(db):
    text = _error(
        "SELECT a.name FROM emails e JOIN attachments a ON a.email_id = e.id WHERE e.id = 2"
    )

    assert "attachments" in text
    assert "filename" in text


def test_the_folded_shadow_columns_are_not_offered(db):
    text = _error("SELECT nope FROM emails")

    assert "subject_f" not in text


def test_an_unknown_table_names_the_close_ones(db):
    text = _error("SELECT id FROM email")

    assert "no such table: email" in text
    assert "emails" in text
    assert "sql_schema" in text


def test_a_query_whose_tables_cannot_be_named_still_says_where_to_look(db):
    text = _error("WITH t(x) AS (VALUES (1)) SELECT y FROM t")

    assert "no such column: y" in text
    assert "sql_schema" in text


def test_the_lookup_after_an_error_writes_nothing(db):
    """The column listing runs on the same read-only connection, after the error."""
    _error("SELECT date FROM emails")

    assert mcp_server.sql_query(sql="SELECT COUNT(*) FROM emails")["rows"] == [[2]]


# ------------------------------------------------------------ the description


def test_the_description_names_the_columns_agents_guess(db):
    text = advertised_text("sql_query")

    for name in (
        "emails.date_received",
        "sender_name",
        "sender_address",
        "subject",
        "source_class",
        "attachment_content.extracted_text",
        "attachments.id = attachment_content.attachment_id",
        "teams_messages",
        "calendar_events.start_at",
        "teams_thread_id",
        "whatsapp_thread_id",
        "event_id",
        "conversation_turn_id",
    ):
        assert name in text, name
    assert "no `date` column" in text


def test_the_description_steers_words_to_the_full_text_tables(db):
    text = advertised_text("sql_query")

    assert "MATCH sb_fold(" in text
    assert "emails_fts" in text
    assert "LIKE '%" in text  # named as what not to do


def test_the_match_form_the_description_gives_finds_greek_in_any_accent(db):
    out = mcp_server.sql_query(
        sql="SELECT e.id FROM emails_fts JOIN emails e ON e.id = emails_fts.rowid "
        "WHERE emails_fts MATCH sb_fold('ΚΑΡΤΕΣ') ORDER BY e.date_received DESC"
    )

    assert out["rows"] == [[1]]


def test_attachment_text_is_found_through_its_own_index(db):
    out = mcp_server.sql_query(
        sql="SELECT a.id, a.filename FROM attachment_content_fts "
        "JOIN attachment_content ac ON ac.id = attachment_content_fts.rowid "
        "JOIN attachments a ON a.id = ac.attachment_id "
        "WHERE attachment_content_fts MATCH sb_fold('okapi')"
    )

    assert out["rows"] == [[7, "plan.pdf"]]


def test_a_slow_like_scan_is_told_about_match(db, monkeypatch):
    monkeypatch.setattr(sql_readonly, "BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(sql_readonly, "_PROGRESS_STEPS", 1)

    text = _error("SELECT id FROM emails WHERE sb_fold(content) LIKE '%' || sb_fold('x') || '%'")

    assert "budget" in text
    assert "MATCH" in text


# ------------------------------------------------------------ the response budget


def test_an_answer_stays_under_the_response_budget(db, monkeypatch):
    out = mcp_server.sql_query(
        sql="WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 200) "
        "SELECT i, printf('%.900c', 'x') FROM n"
    )

    assert json_chars(out) <= RESPONSE_BUDGET_CHARS
    assert out["truncated"] is True
    assert out["row_count"] == len(out["rows"]) > 0


def test_the_description_states_the_budget():
    assert f"{RESPONSE_BUDGET_CHARS:,} characters" in advertised_text("sql_query")
