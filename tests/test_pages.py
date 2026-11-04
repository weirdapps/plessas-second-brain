"""The page layer (issue 169): compiled, cited pages for people, topics, threads and series.

A page is compiled ahead of the question: a deterministic skeleton read by SQL on every sync,
and later a short model synthesis, every line citing [kind:id, date]. Pages are derived:
never a source, never hand-edited (corrections live in page_overrides), rebuilt from dirty
flags, and swapped blue/green so a reader never sees half a rebuild. All of it sits behind
the gated step `page_layer` (src/store/pages.py).
"""

import json

import pytest

from src.store import pages
from src.store.schema import GATED_MIGRATIONS, apply_gated, create_database, pending_gated
from tests import factories as f

OWNER = ("Owner Example", "owner@example.com")
ALICE = f.ALICE
BOB = f.BOB


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    from src import config

    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])


@pytest.fixture
def store(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


@pytest.fixture
def old(tmp_path):
    conn = create_database(str(tmp_path / "old.db"), gated=False)
    yield conn
    conn.close()


def _thread(conn, subject, n=3, *, days=2, topics=("okapi budget",), cid=None):
    cid = cid or f"cid-{subject}"
    ids = []
    for i in range(n):
        ids.append(
            f.email(
                conn,
                subject=subject if i == 0 else f"Re: {subject}",
                sender=ALICE if i % 2 == 0 else OWNER,
                to=(OWNER,) if i % 2 == 0 else (ALICE,),
                topics=list(topics),
                conversation_id=cid,
                date_received=f.ago(days=days, hours=-i),
                source_class="mail",
            )
        )
    return ids


def _alice(conn):
    return conn.execute("SELECT id FROM people WHERE email = ?", (ALICE[1],)).fetchone()[0]


# --- the gated step ------------------------------------------------------------------------


def test_the_step_is_gated_and_last(old):
    assert GATED_MIGRATIONS[-1] == "page_layer"
    assert "page_layer" in pending_gated(old)
    assert not pages.enabled(old)


def test_without_the_step_everything_is_a_no_op(old):
    _thread(old, "Budget 2027")
    assert pages.refresh(old) == {"enabled": False}
    assert pages.page_for_person(old, _alice(old)) is None
    assert pages.cards(old, "Budget 2027") == []
    assert pages.invalidate_refs(old, ["email:1"]) == 0


def test_the_step_adds_tables_and_changes_no_row(old):
    _thread(old, "Budget 2027")
    before = old.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    apply_gated(old)
    old.commit()
    assert pages.enabled(old)
    assert old.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == before
    assert old.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0


def test_pages_are_in_no_full_text_index(store):
    names = {r[0] for r in store.execute("SELECT name FROM sqlite_master")}
    assert not any(n.startswith("pages_fts") for n in names)


# --- skeletons on sync ---------------------------------------------------------------------


def test_the_first_refresh_compiles_every_eligible_entity(store):
    ids = _thread(store, "Budget 2027", n=5)
    stats = pages.refresh(store)
    store.commit()

    assert stats["enabled"] is True
    person = pages.page_for_person(store, _alice(store))
    assert person is not None and person["name"] == ALICE[0]
    assert person["kind"] == "person"
    # Every skeleton line cites [kind:id, date].
    assert person["lines"], person
    for line in person["lines"]:
        assert line["refs"] and all(":" in r for r in line["refs"])
    assert f"[email:{ids[-1]}, " in person["markdown"]

    thread = pages.page_for_thread(store, "cid-Budget 2027")
    assert thread is not None and thread["facts"]["emails"] == 5

    topic_id = store.execute("SELECT id FROM topics WHERE name = 'okapi budget'").fetchone()[0]
    topic = pages.page_for_topic(store, topic_id)
    assert topic is not None and "okapi budget" in topic["name"].lower()


def test_the_owner_gets_no_person_page(store):
    _thread(store, "Budget 2027", n=4)
    pages.refresh(store)
    owner_id = store.execute("SELECT id FROM people WHERE email = ?", (OWNER[1],)).fetchone()[0]
    assert pages.page_for_person(store, owner_id) is None


def test_an_entity_below_the_floor_gets_no_page(store):
    f.email(store, subject="One-off", sender=BOB, to=(OWNER,), conversation_id="one")
    pages.refresh(store)
    bob = store.execute("SELECT id FROM people WHERE email = ?", (BOB[1],)).fetchone()[0]
    assert pages.page_for_person(store, bob) is None
    assert pages.page_for_thread(store, "one") is None


def test_a_later_refresh_touches_only_what_new_items_touched_and_marks_it_dirty(store):
    _thread(store, "Budget 2027", n=3)
    _thread(store, "Data platform", n=3, topics=("data platform",))
    pages.refresh(store)
    store.execute("DELETE FROM page_dirty")
    store.commit()

    f.email(
        store,
        subject="Re: Budget 2027",
        sender=ALICE,
        to=(OWNER,),
        topics=["okapi budget"],
        conversation_id="cid-Budget 2027",
        source_class="mail",
    )
    stats = pages.refresh(store)
    dirty = {(k, e) for k, e in store.execute("SELECT kind, entity_key FROM page_dirty")}

    assert ("thread", "cid-Budget 2027") in dirty
    assert ("thread", "cid-Data platform") not in dirty
    assert pages.page_for_thread(store, "cid-Budget 2027")["facts"]["emails"] == 4
    assert stats["touched"] >= 1


def test_a_series_page_is_keyed_on_the_recurrence_master(store):
    for d in (21, 14, 7):
        f.calendar_event(
            store,
            subject="Weekly cards steering",
            start_at=f.ago(days=d),
            recurrence_master_id="master-1",
            is_recurring=1,
            body_summary=f"Steering of {d} days ago: pricing agreed",
            attendees=(ALICE, OWNER),
        )
    pages.refresh(store)
    series = pages.page_for_series(store, "master-1")
    assert series is not None
    assert series["facts"]["occurrences"] == 3
    assert any(r.startswith("calendar:") for line in series["lines"] for r in line["refs"])


def test_topic_pages_key_on_the_canonical_topic_when_the_registry_holds_one(store):
    _thread(store, "Budget 2027", n=3, topics=("okapi budget",))
    _thread(store, "Budget again", n=3, topics=("budget okapi",))
    a = store.execute("SELECT id FROM topics WHERE name = 'okapi budget'").fetchone()[0]
    b = store.execute("SELECT id FROM topics WHERE name = 'budget okapi'").fetchone()[0]
    store.execute(
        "INSERT INTO canonical_topics (id, name, display_name, named_by, head_topic_id,"
        " created_at) VALUES (1, 'okapi budget', 'Okapi budget', 'rule', ?, 'x')",
        (a,),
    )
    for t in (a, b):
        store.execute(
            "INSERT INTO topic_aliases (topic_id, canonical_id, method, score, fold_key,"
            " mapped_at) VALUES (?, 1, 'fold', 1, 'budget okapi', 'x')",
            (t,),
        )
    store.commit()
    pages.refresh(store)

    page_a, page_b = pages.page_for_topic(store, a), pages.page_for_topic(store, b)
    assert page_a is not None and page_a["key"] == "c1" == page_b["key"]
    assert page_a["facts"]["emails"] == 6


# --- overrides, invalidation, generations --------------------------------------------------


def test_overrides_hide_drop_a_ref_and_add_a_correction_never_editing_the_page(store):
    ids = _thread(store, "Budget 2027", n=4)
    pages.refresh(store)
    key = str(_alice(store))

    pages.add_override(store, "person", key, "drop_ref", ref=f"email:{ids[-1]}")
    pages.add_override(store, "person", key, "correct", text="Alice moved to Treasury")
    page = pages.page_for_person(store, _alice(store))
    assert f"email:{ids[-1]}" not in page["markdown"]
    assert page["corrections"][0]["text"] == "Alice moved to Treasury"
    stored = store.execute(
        "SELECT skeleton FROM pages WHERE kind = 'person' AND entity_key = ?", (key,)
    ).fetchone()[0]
    assert f"email:{ids[-1]}" in stored, "the stored page is untouched"

    pages.add_override(store, "person", key, "hide")
    assert pages.page_for_person(store, _alice(store)) is None


def test_forgetting_an_item_blanks_the_synthesis_and_recompiles_the_skeleton(store):
    ids = _thread(store, "Budget 2027", n=4)
    pages.refresh(store)
    key = str(_alice(store))
    store.execute(
        "UPDATE pages SET synthesis = ?, evidence_keys = ? WHERE kind = 'person'"
        " AND entity_key = ?",
        (
            json.dumps([{"text": "x", "refs": [f"email:{ids[0]}"]}]),
            json.dumps([f"email:{ids[0]}"]),
            key,
        ),
    )
    store.execute("DELETE FROM page_dirty")
    store.execute("DELETE FROM email_people WHERE email_id = ?", (ids[0],))
    store.execute("DELETE FROM email_topics WHERE email_id = ?", (ids[0],))
    store.execute("DELETE FROM emails WHERE id = ?", (ids[0],))

    n = pages.invalidate_refs(store, [f"email:{ids[0]}"])

    assert n >= 1
    page = pages.page_for_person(store, _alice(store))
    assert page["synthesis"] == []
    assert f"email:{ids[0]}," not in page["markdown"]
    reasons = dict(store.execute("SELECT entity_key, reason FROM page_dirty WHERE kind='person'"))
    assert reasons[key] == "forgotten"


def test_forgetting_drops_a_live_topic_page_that_cited_the_item(store):
    store.execute(
        "INSERT INTO topic_pages (topic, display_name, members, selected_by, body, citations,"
        " evidence_keys, built_at, prompt_hash) VALUES ('t', 'T', '[]', 'named', 'b', '[]',"
        " '[\"email:9\"]', 'x', 'h')"
    )
    pages.invalidate_refs(store, ["email:9"])
    assert store.execute("SELECT COUNT(*) FROM topic_pages").fetchone()[0] == 0


def test_a_full_build_is_swapped_in_whole(store):
    _thread(store, "Budget 2027", n=4)
    pages.refresh(store)
    key = str(_alice(store))
    gen = pages.begin_build(store, "person")
    assert gen == 1
    pages.write_synthesis(
        store, "person", key, [{"text": "new", "refs": ["email:1"]}], generation=gen, meta={}
    )
    # Not live until the swap.
    assert pages.page_for_person(store, _alice(store))["synthesis"] == []
    pages.swap(store, "person")
    page = pages.page_for_person(store, _alice(store))
    assert page["synthesis"][0]["text"] == "new"
    assert page["generation"] == 1
    assert (
        store.execute(
            "SELECT COUNT(*) FROM pages WHERE kind = 'person' AND generation = 0"
        ).fetchone()[0]
        == 0
    )


# --- cards on search -----------------------------------------------------------------------


def test_cards_find_the_page_a_query_names(store):
    _thread(store, "Budget 2027", n=4)
    pages.refresh(store)
    found = pages.cards(store, "alice smith")
    assert found and found[0]["kind"] == "person"
    assert found[0]["uri"] == f"brain://pages/person/{_alice(store)}"
    assert pages.cards(store, "nothing like it") == []


def test_a_skeleton_lists_current_items_once(store):
    ids = _thread(store, "Budget 2027", n=5)
    keep = f.decision(store, "Adopt budget v3", email_id=ids[0])
    f.decision(store, "Adopt budget v3", email_id=ids[1])  # the same, said again
    old_id = f.decision(store, "Adopt budget v1", email_id=ids[2])
    store.execute("UPDATE decisions SET superseded_by = ? WHERE id = ?", (keep, old_id))
    store.commit()
    pages.refresh(store)
    topic_id = store.execute("SELECT id FROM topics WHERE name = 'okapi budget'").fetchone()[0]
    texts = [line["text"] for line in pages.page_for_topic(store, topic_id)["lines"]]
    assert sum("budget v3" in t for t in texts) == 1
    assert not any("budget v1" in t for t in texts)


# --- follow-up: chats mark person pages, and every owner row is excluded ------------------


def _teams_message(conn, person_id, mri, title="Budget chat"):
    chat = f.teams_chat(conn)
    thread = f.teams_thread(conn, chat_id=chat, title=title)
    conn.execute(
        "UPDATE teams_messages SET sender_person_id = ?, sender_mri = ? WHERE id = ?",
        (person_id, mri, f.teams_message(conn, thread_id=thread, chat_id=chat)),
    )
    conn.commit()
    return thread


def test_teams_activity_marks_a_person_page_dirty_with_the_thread_cited(store):
    _thread(store, "Budget 2027", n=4)
    pages.refresh(store)
    store.execute("DELETE FROM page_dirty")
    store.commit()
    alice = _alice(store)

    thread = _teams_message(store, alice, "8:orgid:alice")
    pages.refresh(store)

    dirty = {k for (k,) in store.execute("SELECT entity_key FROM page_dirty WHERE kind='person'")}
    assert str(alice) in dirty
    refs = {r for line in pages.page_for_person(store, alice)["lines"] for r in line["refs"]}
    assert f"teams:{thread}" in refs


def test_teams_activity_found_by_mri_identifier(store):
    _thread(store, "Budget 2027", n=4)
    alice = _alice(store)
    store.execute(
        "INSERT INTO person_identifiers (person_id, kind, value, source, created_at)"
        " VALUES (?, 'mri', '8:orgid:alice', 'test', 'x')",
        (alice,),
    )
    pages.refresh(store)
    thread = _teams_message(store, None, "8:orgid:alice", title="By MRI")
    pages.refresh(store)
    refs = {r for line in pages.page_for_person(store, alice)["lines"] for r in line["refs"]}
    assert f"teams:{thread}" in refs


def test_new_sources_on_an_older_page_store_start_at_their_head_without_a_full_compile(store):
    _thread(store, "Budget 2027", n=4)
    pages.refresh(store)
    store.execute(
        "DELETE FROM page_watermarks WHERE source IN ('teams_messages', 'whatsapp_messages')"
    )
    store.execute("DELETE FROM page_dirty")
    store.commit()
    stats = pages.refresh(store)
    assert stats["first"] is False and stats["touched"] == 0


def test_every_row_of_the_owner_is_left_without_a_page(store):
    """The owner's fixed id (person_owner) and every address it holds: a personal address
    the owner pattern does not list gets no page either."""
    from src.store import person_identity

    personal = ("Owner Example", "owner.personal@example.org")
    for i in range(4):
        f.email(
            store,
            subject=f"Note to self {i}",
            sender=personal,
            to=(ALICE,),
            conversation_id=f"self-{i}",
            source_class="mail",
        )
    owner = f.person(store, *OWNER)
    person_identity.set_owner(store, owner)
    store.execute("DELETE FROM person_identifiers WHERE value = ?", (personal[1],))
    store.execute(
        "INSERT INTO person_identifiers (person_id, kind, value, source, created_at)"
        " VALUES (?, 'address', ?, 'test', 'x')",
        (owner, personal[1]),
    )
    store.commit()
    pages.refresh(store)
    row = store.execute("SELECT id FROM people WHERE email = ?", (personal[1],)).fetchone()[0]
    assert pages.page_for_person(store, row) is None
    assert pages.page_for_person(store, _alice(store)) is not None
