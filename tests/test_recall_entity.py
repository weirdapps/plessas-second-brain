"""recall fuses the entity list, the items linked to what a query names, as a third ranked
list into its email, Teams and WhatsApp buckets (src/store/recall.py,
src/store/entities.py entity_ranked_ids), behind BRAIN_RECALL_ENTITY_LIST.

Every value is invented: example.com addresses and made-up names.
"""

import pytest

from src import config
from src.store import entities as en
from src.store import recall as rc
from src.store.embeddings import SemanticCandidates
from src.store.recall import recall
from src.store.schema import create_database
from tests import factories as f
from tests.patching import unset_env

OWNER = ("Olga Owner", "olga@corp.example")
MARIA = ("Maria Papadopoulou", "maria.papadopoulou@corp.example")
BOB = ("Bob Jones", "bob@corp.example")
PETROS_JID = "30000000077@s.whatsapp.net"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    unset_env(monkeypatch, "BRAIN_QUERY_RANKING", "BRAIN_RECALL_WORKERS")
    monkeypatch.setenv("BRAIN_RECALL_ENTITY_LIST", "1")
    monkeypatch.setattr(config, "USER_NAME", OWNER[0])
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])


@pytest.fixture
def store(tmp_path):
    """Maria on mail no word of hers is in, in a Teams thread, and Petros on WhatsApp."""
    conn = create_database(str(tmp_path / "brain.db"))
    made = {
        "cc": f.email(conn, subject="Lunch plans", sender=BOB, to=(OWNER,), cc=(MARIA,)),
        "unrelated": f.email(conn, subject="Parking", sender=BOB, to=(OWNER,)),
    }
    thread = f.teams_thread(conn, title="Weekly sync", summary="Dates moved.")
    # Shown by her initials, so no keyword finds the thread by her name.
    maria = _pid(conn, MARIA[1])
    f.teams_message(conn, sender=("M. P.", MARIA[1]), thread_id=thread, sender_person_id=maria)
    made["teams"] = thread
    f.person(conn, "Petros Zebrakis", PETROS_JID)
    conn.executescript(
        f"""
        INSERT INTO whatsapp_chats (id, chat_jid, name, chat_kind, first_seen_at)
            VALUES (1, '120000000000000009@g.us', 'Chat A', 'group', '{f.ago(days=3)}');
        INSERT INTO whatsapp_threads (id, chat_id, anchor_message_id, started_at, ended_at,
                                      message_count, participant_names, title, summary)
            VALUES (1, 1, 'w1', '{f.ago(days=3)}', '{f.ago(days=3)}', 1, '[]',
                    'Friday', 'Plans for Friday');
        INSERT INTO whatsapp_messages (chat_jid, message_id, chat_id, thread_id, sender_jid,
                                       sender_name, is_from_me, sent_at, content)
            VALUES ('120000000000000009@g.us', 'w1', 1, 1, '{PETROS_JID}', 'P. Z.', 0,
                    '{f.ago(days=3)}', 'see you then');
        """
    )
    conn.commit()
    made["whatsapp"] = 1
    en.seed_entities(conn)
    yield conn, made
    conn.close()


def _pid(conn, address):
    return conn.execute("SELECT id FROM people WHERE email = ?", (address,)).fetchone()[0]


def _provider(emails=(), **by_kind):
    def provider(conn, query, limit):
        return SemanticCandidates(list(emails)[:limit], semantic="ok", by_kind=by_kind)

    return provider


def test_mail_linked_to_the_named_person_joins_the_email_bucket(store):
    conn, made = store

    out = recall(conn, "Maria Papadopoulou", semantic_candidates=_provider())

    rows = {r["email_id"]: r for r in out["emails"]}
    assert made["cc"] in rows and made["unrelated"] not in rows
    assert rows[made["cc"]]["matched_in"] == "entity"
    assert rows[made["cc"]]["ref"] is not None  # enveloped


def test_teams_and_whatsapp_rows_of_the_named_person_join_their_buckets(store):
    conn, made = store

    maria = recall(conn, "Maria Papadopoulou", semantic_candidates=_provider())
    petros = recall(conn, "Petros Zebrakis", semantic_candidates=_provider())

    assert [r["thread_id"] for r in maria["teams"]] == [made["teams"]]
    assert maria["teams"][0]["matched_in"] == "entity" and maria["teams"][0]["kind"] == "teams"
    assert [r["thread_id"] for r in petros["whatsapp"]] == [made["whatsapp"]]
    assert petros["whatsapp"][0]["matched_in"] == "entity"


def test_entity_rows_count_as_neither_whole_nor_partial_matches(store):
    conn, _made = store

    out = recall(conn, "Maria Papadopoulou", semantic_candidates=_provider())

    assert "emails" in out["summary"]["kinds_with_results"]
    assert "emails" not in out["summary"]["partial_kinds"]


@pytest.mark.parametrize(
    "setting, value", [("BRAIN_RECALL_ENTITY_LIST", "0"), ("BRAIN_QUERY_RANKING", "relevance")]
)
def test_off_by_the_switch_or_with_relevance_ranking(store, monkeypatch, setting, value):
    conn, made = store
    monkeypatch.setenv(setting, value)

    out = recall(conn, "Maria Papadopoulou", semantic_candidates=_provider())

    assert made["cc"] not in [r["email_id"] for r in out["emails"]]
    assert out["teams"] == []


def test_without_a_semantic_provider_recall_stays_keyword_only(store):
    conn, made = store

    out = recall(conn, "Maria Papadopoulou")

    assert made["cc"] not in [r["email_id"] for r in out["emails"]]


def test_the_list_fuses_when_the_semantic_half_is_unavailable(store):
    conn, made = store

    def boom(conn, query, limit):
        raise RuntimeError("no credentials")

    out = recall(conn, "Maria Papadopoulou", semantic_candidates=boom)

    assert out["summary"]["semantic"] == "unavailable: RuntimeError"
    assert made["cc"] in [r["email_id"] for r in out["emails"]]
    assert [r["thread_id"] for r in out["teams"]] == [made["teams"]]


def test_the_entities_are_looked_up_once_a_call(store, monkeypatch):
    conn, _made = store
    calls = []
    real = rc.entity_ranked_ids

    def counting(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(rc, "entity_ranked_ids", counting)

    recall(conn, "Maria Papadopoulou", semantic_candidates=_provider())

    assert calls == ["Maria Papadopoulou"]


def test_a_kind_not_asked_for_takes_no_entity_rows(store):
    conn, made = store

    out = recall(
        conn, "Maria Papadopoulou", semantic_candidates=_provider(), kinds=["document", "teams"]
    )

    assert out["emails"] == []
    assert [r["thread_id"] for r in out["teams"]] == [made["teams"]]


def test_a_keyword_hit_keeps_its_row_and_the_list_moves_it_up(store, monkeypatch):
    conn, made = store
    named = f.email(
        conn,
        subject="Zebra budget review for the quarter",
        sender=MARIA,
        to=(OWNER,),
        date_received=f.ago(days=30),
    )
    plain = f.email(conn, subject="Zebra budget", sender=BOB, to=(OWNER,))
    query = "zebra budget Maria Papadopoulou"

    monkeypatch.setenv("BRAIN_RECALL_ENTITY_LIST", "0")
    off = [r["email_id"] for r in recall(conn, query, semantic_candidates=_provider())["emails"]]
    monkeypatch.setenv("BRAIN_RECALL_ENTITY_LIST", "1")
    out = recall(conn, query, semantic_candidates=_provider())

    assert off[:2] == [plain, named]  # the closer and newer subject first
    ids = [r["email_id"] for r in out["emails"]]
    assert ids[:2] == [named, plain] and made["cc"] in ids
    assert out["emails"][0]["matched_in"] != "entity"  # its keyword row, with the evidence


def test_a_store_that_cannot_answer_the_list_leaves_the_buckets_as_they_were(store, monkeypatch):
    import sqlite3

    conn, made = store

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: entity_aliases")

    monkeypatch.setattr(rc, "entity_ranked_ids", broken)

    out = recall(conn, "Maria Papadopoulou", semantic_candidates=_provider())

    assert made["cc"] not in [r["email_id"] for r in out["emails"]]
