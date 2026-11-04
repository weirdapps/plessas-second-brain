"""Identity split (issue 170): gated migration person_identity.

People rows with a hard identifier (address, Teams MRI, WhatsApp JID, phone) are
identities; the rest are mentions, listed in person_mentions. person_identifiers holds
every identifier and name variant. email_people keeps the header role as an enum
(sender, recipient, cc, bcc, mentioned) and the model's free text in role_text. Only an
exact hard identifier merges two rows by itself; a name-only match waits in
person_review for the owner, who clears it with one command. Every merge is logged in
person_merges with what it moved, and can be undone. The owner keeps one fixed id.
Every value is invented.
"""

import json
import sqlite3

import pytest

from src import config
from src.store import dedup_people, person_identity
from src.store.loader import load_single_email
from src.store.normalizer import find_or_create_person
from src.store.schema import (
    GATED_MIGRATIONS,
    GATED_TABLE,
    _gated_steps,
    apply_gated,
    create_database,
    pending_gated,
)
from tests import factories as f
from tests.brain_cli import brain

OWNER = ("Olga Owner", "olga@corp.example")
OWNER_ALIAS = "o.owner@corp.example"
ALICE = ("Alice Smith", "alice@corp.example")


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", OWNER[0])
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", f"{OWNER[1]}, {OWNER_ALIAS}")


def _legacy(path=":memory:"):
    """A store with every gated step but this one, as the producer holds it today."""
    db = create_database(str(path), gated=False)
    db.execute(GATED_TABLE)
    for name, step in _gated_steps():
        if name != person_identity.NAME:
            step(db)
            db.execute(
                "INSERT INTO gated_migrations (name, applied_at, backup) VALUES (?, 'x', NULL)",
                (name,),
            )
    db.commit()
    return db


def _people(db):
    return {r[0] for r in db.execute("SELECT id FROM people")}


def _roles(db, email_id):
    return sorted(
        tuple(r)
        for r in db.execute(
            "SELECT p.name, ep.role_in_email, ep.role_text FROM email_people ep"
            " JOIN people p ON p.id = ep.person_id WHERE ep.email_id = ?",
            (email_id,),
        )
    )


# --- the step is gated --------------------------------------------------------------------


def test_the_step_is_named_and_last_and_waits_on_a_legacy_store():
    # After the three before it; topic_registry (issue 180) came after.
    assert GATED_MIGRATIONS.index("person_identity") == 3
    db = _legacy()
    assert pending_gated(db)[0] == "person_identity"
    assert "person_identifiers" not in {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert not person_identity.active(db)


def test_a_new_store_takes_it():
    db = create_database(":memory:")
    assert person_identity.active(db)
    assert pending_gated(db) == []


# --- the backfill -------------------------------------------------------------------------


def _seeded():
    db = _legacy()
    mail = f.email(db, subject="Okapi budget", sender=ALICE, to=(OWNER,))
    owner_alias = f.person(db, "Olga O.", OWNER_ALIAS)
    f.email(db, subject="Okapi notes", sender=ALICE, to=(("Olga O.", OWNER_ALIAS),))
    mention = db.execute("INSERT INTO people (name) VALUES ('Nikos Papadopoulos')").lastrowid
    for role in ("FYI", "reviewer"):
        db.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, ?)",
            (mail, mention, role),
        )
    alice = f.person(db, *ALICE)
    db.execute(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'FYI')",
        (mail, alice),
    )
    db.execute(
        "INSERT INTO teams_mri_resolution (mri, email, person_id, status)"
        " VALUES ('8:orgid:alice', ?, ?, 'resolved')",
        (ALICE[1], alice),
    )
    db.commit()
    return db, mail, mention, alice, owner_alias


def test_the_backfill_splits_identities_from_mentions_and_records_identifiers():
    db, mail, mention, alice, _ = _seeded()
    apply_gated(db)
    ids = {
        (r["kind"], r["value"]): r["person_id"]
        for r in db.execute("SELECT kind, value, person_id FROM person_identifiers")
    }
    assert ids[("address", ALICE[1])] == alice
    assert ids[("mri", "8:orgid:alice")] == alice
    assert ids[("name", ALICE[0])] == alice
    assert ids[("name", "Nikos Papadopoulos")] == mention
    mentions = {r[0] for r in db.execute("SELECT person_id FROM person_mentions")}
    assert mentions == {mention}
    identities = {r[0] for r in db.execute("SELECT id FROM person_identities")}
    assert alice in identities and mention not in identities


def test_the_header_role_becomes_an_enum_and_the_model_text_moves_out_of_the_key():
    db, mail, mention, alice, _ = _seeded()
    apply_gated(db)
    roles = _roles(db, mail)
    assert ("Alice Smith", "sender", None) in roles
    assert ("Alice Smith", "mentioned", "FYI") in roles
    nikos = [r for r in roles if r[0] == "Nikos Papadopoulos"]
    assert len(nikos) == 1 and nikos[0][1] == "mentioned"
    assert set(nikos[0][2].split("; ")) == {"FYI", "reviewer"}
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'FYI')",
            (mail, alice),
        )


def test_the_owner_gets_one_fixed_id_and_the_alias_row_is_merged_into_it_with_a_log():
    db, *_, owner_alias = _seeded()
    owner_row = f.person(db, *OWNER)
    apply_gated(db)
    fixed = person_identity.owner_id(db)
    assert fixed == owner_row
    assert owner_alias not in _people(db)
    log = db.execute("SELECT kept_id, merged_id, method, evidence FROM person_merges").fetchall()
    assert [tuple(r) for r in log] == [(owner_row, owner_alias, "owner", f"address:{OWNER_ALIAS}")]
    # The alias address still finds the owner, so the loader creates no new row for it.
    assert find_or_create_person(db, "Olga O.", OWNER_ALIAS.upper(), display_name=True) == fixed


def test_counts_cover_what_the_step_changes():
    db, *_ = _seeded()
    before = person_identity.counts(db)
    apply_gated(db)
    after = person_identity.counts(db)
    assert before["person_identifiers"] == 0
    assert after["person_identifiers"] > 0
    assert after["person_mentions"] == 1
    assert after["email_people.mentioned"] == 2
    assert after["person_merges"] == 1
    assert before["email_people"] == after["email_people"] + 1  # two Nikos roles became one


# --- the loader ---------------------------------------------------------------------------


def _meta(n, **extra):
    return {
        "message_id": f"m{n}",
        "date_received": "2026-10-01T09:00:00+00:00",
        "subject": f"Okapi {n}",
        "sender": {"name": ALICE[0], "address": ALICE[1]},
        "to_recipients": [{"name": OWNER[0], "address": OWNER[1]}],
        "cc_recipients": [],
        "body": "Body",
        **extra,
    }


def _extraction():
    return {
        "summary": "s",
        "people_roles": {"Nikos Papadopoulos": ["FYI", "reviewer"]},
    }


def test_the_loader_writes_enum_roles_and_records_mentions_on_a_gated_store():
    db = create_database(":memory:")
    assert load_single_email(db, _meta(1), _extraction())
    mail = db.execute("SELECT id FROM emails").fetchone()[0]
    roles = _roles(db, mail)
    assert ("Alice Smith", "sender", None) in roles
    assert ("Olga Owner", "recipient", None) in roles
    nikos = [r for r in roles if r[0] == "Nikos Papadopoulos"]
    assert len(nikos) == 1 and nikos[0][1] == "mentioned"
    assert set(nikos[0][2].split("; ")) == {"FYI", "reviewer"}
    nikos_id = db.execute("SELECT id FROM people WHERE name = 'Nikos Papadopoulos'").fetchone()[0]
    assert db.execute("SELECT 1 FROM person_mentions WHERE person_id = ?", (nikos_id,)).fetchone()
    alice = db.execute("SELECT id FROM people WHERE email = ?", (ALICE[1],)).fetchone()[0]
    assert db.execute(
        "SELECT 1 FROM person_identifiers WHERE kind = 'address' AND value = ? AND person_id = ?",
        (ALICE[1], alice),
    ).fetchone()


def test_the_loader_keeps_free_text_roles_on_a_legacy_store():
    db = _legacy()
    assert load_single_email(db, _meta(1), _extraction())
    roles = {r[0] for r in db.execute("SELECT role_in_email FROM email_people")}
    assert {"FYI", "reviewer", "sender", "recipient"} <= roles


def test_a_mention_that_gains_an_address_becomes_an_identity():
    db = create_database(":memory:")
    pid = find_or_create_person(db, "Nikos Papadopoulos")
    assert db.execute("SELECT 1 FROM person_mentions WHERE person_id = ?", (pid,)).fetchone()
    assert find_or_create_person(db, "Nikos Papadopoulos", "np@corp.example") == pid
    assert not db.execute("SELECT 1 FROM person_mentions WHERE person_id = ?", (pid,)).fetchone()


# --- merges: hard identifiers only, the rest queued ---------------------------------------


def _two_nikos(db):
    a = db.execute(
        "INSERT INTO people (name, email) VALUES ('Nikos Papadopoulos', 'np@corp.example')"
    ).lastrowid
    b = find_or_create_person(db, "NIKOS PAPADOPOULOS")
    mail = f.email(db, subject="Okapi", sender=ALICE, to=())
    person_identity.link_role(db, mail, b, "FYI")
    db.commit()
    return a, b, mail


def test_a_name_only_match_is_queued_never_merged_on_a_gated_store(tmp_path):
    path = tmp_path / "b.db"
    db = create_database(str(path))
    a, b, _ = _two_nikos(db)
    db.close()
    results = dedup_people.run_dedup(str(path))
    db = sqlite3.connect(path)
    assert {a, b} <= _people(db)
    queued = db.execute("SELECT person_a, person_b, rule, status FROM person_review").fetchall()
    assert [tuple(r) for r in queued] == [(a, b, "phase2", "pending")]
    assert results["queued"] == 1
    assert db.execute("SELECT COUNT(*) FROM person_merges").fetchone()[0] == 0


def test_a_shared_address_merges_by_itself_and_is_logged(tmp_path):
    path = tmp_path / "b.db"
    db = create_database(str(path))
    a = db.execute(
        "INSERT INTO people (name, email) VALUES ('np@corp.example', 'np@corp.example')"
    ).lastrowid
    b = db.execute(
        "INSERT INTO people (name, email) VALUES ('Nikos Papadopoulos', 'NP@corp.example')"
    ).lastrowid
    db.commit()
    db.close()
    dedup_people.run_dedup(str(path))
    db = sqlite3.connect(path)
    assert len({a, b} & _people(db)) == 1
    method, evidence = db.execute("SELECT method, evidence FROM person_merges").fetchone()
    assert method == "address" and evidence == "address:np@corp.example"


def test_the_legacy_store_still_merges_by_name(tmp_path):
    path = tmp_path / "b.db"
    db = _legacy(path)
    a, b, _ = _two_nikos(db)
    db.close()
    dedup_people.run_dedup(str(path))
    db = sqlite3.connect(path)
    assert _people(db) & {a, b} == {a}


def test_a_rejected_pair_is_not_queued_again(tmp_path, monkeypatch, capsys):
    path = tmp_path / "b.db"
    db = create_database(str(path))
    _two_nikos(db)
    db.close()
    dedup_people.run_dedup(str(path))
    db = sqlite3.connect(path)
    review = db.execute("SELECT id FROM person_review").fetchone()[0]
    db.close()
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(path), "maint", "people-review", "--reject", str(review)
    )
    assert code == 0, out
    dedup_people.run_dedup(str(path))
    rows = sqlite3.connect(path).execute("SELECT status FROM person_review").fetchall()
    assert [r[0] for r in rows] == ["rejected"]


def test_the_owner_clears_the_queue_with_one_command_and_every_merge_is_logged(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "b.db"
    db = create_database(str(path))
    a, b, mail = _two_nikos(db)
    db.close()
    dedup_people.run_dedup(str(path))
    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "maint", "people-review")
    assert code == 0 and "NIKOS PAPADOPOULOS" in out
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(path), "maint", "people-review", "--merge-all"
    )
    assert code == 0, out
    db = sqlite3.connect(path)
    assert _people(db) & {a, b} == {a}
    assert db.execute("SELECT status FROM person_review").fetchone()[0] == "merged"
    kept, merged, method = db.execute(
        "SELECT kept_id, merged_id, method FROM person_merges"
    ).fetchone()
    assert (kept, merged, method) == (a, b, "review")
    assert (
        db.execute(
            "SELECT person_id FROM email_people WHERE email_id = ? AND role_in_email = 'mentioned'",
            (mail,),
        ).fetchone()[0]
        == a
    )


def test_a_merge_is_undone_with_its_references(tmp_path, monkeypatch, capsys):
    path = tmp_path / "b.db"
    db = create_database(str(path))
    a, b, mail = _two_nikos(db)
    f.teams_message(db, sender=("NIKOS PAPADOPOULOS", None), sender_person_id=b)
    db.execute(
        "INSERT INTO commitments (email_id, commitment, by_person_id) VALUES (?, 'Send it', ?)",
        (mail, b),
    )
    db.commit()
    merge = person_identity.merge(db, a, b, method="review", evidence="name:nikos papadopoulos")
    db.commit()
    assert b not in _people(db)
    db.close()
    code, out, _ = brain(
        monkeypatch, capsys, "--db", str(path), "maint", "people-unmerge", str(merge)
    )
    assert code == 0, out
    db = sqlite3.connect(path)
    assert db.execute("SELECT name, email FROM people WHERE id = ?", (b,)).fetchone() == (
        "NIKOS PAPADOPOULOS",
        None,
    )
    assert db.execute(
        "SELECT person_id, role_text FROM email_people WHERE email_id = ? AND person_id = ?",
        (mail, b),
    ).fetchone() == (b, "FYI")
    assert not db.execute(
        "SELECT 1 FROM email_people WHERE email_id = ? AND person_id = ?", (mail, a)
    ).fetchone()
    assert db.execute("SELECT sender_person_id FROM teams_messages").fetchone()[0] == b
    assert db.execute("SELECT by_person_id FROM commitments").fetchone()[0] == b
    assert db.execute("SELECT 1 FROM person_mentions WHERE person_id = ?", (b,)).fetchone()
    assert db.execute("SELECT undone_at FROM person_merges").fetchone()[0] is not None


def test_the_owner_row_always_survives_a_merge():
    db = create_database(":memory:")
    owner_row = f.person(db, *OWNER)
    other = db.execute("INSERT INTO people (name) VALUES ('Olga Owner')").lastrowid
    person_identity.set_owner(db, owner_row)
    person_identity.merge(db, other, owner_row, method="review", evidence="name:olga owner")
    assert owner_row in _people(db) and other not in _people(db)
    assert person_identity.owner_id(db) == owner_row


def test_the_merge_log_records_the_row_it_removed():
    db = create_database(":memory:")
    a, b, _ = _two_nikos(db)
    merge = person_identity.merge(db, a, b, method="review", evidence="name:x")
    row = db.execute(
        "SELECT merged_row, moved FROM person_merges WHERE id = ?", (merge,)
    ).fetchone()
    assert json.loads(row[0])["name"] == "NIKOS PAPADOPOULOS"
    assert json.loads(row[1])["email_people"]
