"""Key facts marked boilerplate (schema v35) are left out of the topic dossier's fact list by
default, its count included, and come back when asked for. A store older than v35, as a
replica's file is until its next pull, has no mark and lists every fact as before."""

import pytest

from src.store.context import get_topic_context
from src.store.item_cleanup import not_boilerplate_sql
from src.store.schema import create_database
from tests import factories as f


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    email_id = f.email(conn, subject="Okapi plan", topics=["okapi"])
    conn.execute(
        "INSERT INTO key_facts (email_id, fact) VALUES (?, 'Okapi budget: 40,000')", (email_id,)
    )
    conn.execute(
        "INSERT INTO key_facts (email_id, fact, boilerplate) VALUES (?, 'This e-mail is restricted', 1)",
        (email_id,),
    )
    conn.commit()
    yield conn
    conn.close()


def test_the_topic_dossier_leaves_boilerplate_out_by_default(store):
    context = get_topic_context(store, "okapi")

    assert [row["fact"] for row in context["key_facts"]] == ["Okapi budget: 40,000"]
    assert context["key_facts_total"] == 1


def test_the_topic_dossier_lists_boilerplate_when_asked(store):
    context = get_topic_context(store, "okapi", include_boilerplate=True)

    assert context["key_facts_total"] == 2


def test_the_meeting_prep_topic_facts_leave_boilerplate_out(store):
    from src.store.query import meeting_prep

    prep = meeting_prep(store, [f.ALICE[1]], topic="okapi")

    assert [row["fact"] for row in prep["topic_context"]["key_facts"]] == ["Okapi budget: 40,000"]


def test_a_store_without_the_mark_filters_nothing(tmp_path):
    conn = create_database(str(tmp_path / "old.db"))
    conn.execute("ALTER TABLE key_facts DROP COLUMN boilerplate")

    assert not_boilerplate_sql(conn) == "1"
    assert not_boilerplate_sql(conn, include=True) == "1"
    conn.close()


def test_the_condition_names_the_alias(store):
    assert not_boilerplate_sql(store, "k") == "k.boilerplate = 0"
    assert not_boilerplate_sql(store, include=True) == "1"
