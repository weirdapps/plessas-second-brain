"""The people dedup after a sync takes the rows that run wrote, not the whole table.

Step 4 ran all seven phases over every person whenever one email loaded: 34,122 people
and no merge on 2026-10-10. Phase 5 alone looks up each name that is an address with a
scan of the table, and those rows stay (a local part without a dot is never rewritten), so
the scans came back every hour. A sync now hands the dedup the people its load added or
changed; each phase acts on the groups those rows fall in, together with every row added
since the last pass whoever added it. The whole table is still taken by the standalone
command, by `sync --full-dedup`, on a store no pass has run on, and once a day.
"""

import random
import shutil
import sqlite3
import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.store import dedup_people
from src.store.dedup_people import FULL_AT_KEY, HIGH_WATER_KEY, run_dedup
from src.store.schema import create_database


def _store(tmp_path, name="brain.db"):
    path = tmp_path / name
    create_database(str(path)).close()
    return str(path)


def _add(db, name, email=None):
    conn = sqlite3.connect(db)
    cur = conn.execute("INSERT INTO people (name, email) VALUES (?, ?)", (name, email))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _people(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id, name, email FROM people ORDER BY id").fetchall()
    conn.close()
    return rows


def _ids(db):
    return {row[0] for row in _people(db)}


def _passed(db, full_hours_ago=1.0):
    """Mark the store as one a pass has run on: every row so far was in view, and the
    whole table was taken `full_hours_ago`."""
    conn = sqlite3.connect(db)
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM people").fetchone()[0]
    when = (datetime.now(UTC) - timedelta(hours=full_hours_ago)).isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        [(HIGH_WATER_KEY, str(top)), (FULL_AT_KEY, when)],
    )
    conn.commit()
    conn.close()


def _metadata(db, key):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT value FROM sync_metadata WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def test_a_scoped_pass_takes_the_new_rows_groups_and_leaves_the_rest(tmp_path, capsys):
    db = _store(tmp_path)
    a1 = _add(db, "Maria Papadopoulou")
    _add(db, "Maria Papadopoulou")  # a pair a whole pass merges (phase 3)
    u = _add(db, "Nikos Papas", "np@corp.example")
    _passed(db)
    _add(db, "Nikos Papas")  # added since, by any writer: a namesake of u without an address

    run_dedup(db, touched=set())

    assert _ids(db) == {a1, a1 + 1, u}
    run_dedup(db)
    assert _ids(db) == {a1, u}


def test_an_old_row_the_load_changed_is_taken_when_named(tmp_path):
    """find_or_create_person renames a row whose name was its address. Below the high
    water mark, only the load's own report brings that row into the pass."""
    db = _store(tmp_path)
    renamed = _add(db, "np@corp.example", "np@corp.example")
    namesake = _add(db, "Nikos Papas")
    _passed(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE people SET name = 'Nikos Papas' WHERE id = ?", (renamed,))
    conn.commit()
    conn.close()

    run_dedup(db, touched=set())
    assert _ids(db) == {renamed, namesake}

    run_dedup(db, touched={renamed})
    assert _ids(db) == {renamed}


def _traced(monkeypatch):
    """Every statement the dedup's connection runs."""
    statements: list[str] = []
    real = dedup_people.get_connection

    def traced(db_path=None):
        conn = real(db_path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(dedup_people, "get_connection", traced)
    return statements


def test_the_scoped_pass_does_not_grow_with_the_rows_it_was_not_given(tmp_path, monkeypatch):
    """Rows whose name is their address, which no pass rewrites, cost a look-up each in a
    whole pass; a scoped one looks up only the rows in its groups."""
    statements = _traced(monkeypatch)
    counts = {}
    for whole in (False, True):
        for untouched in (10, 100):
            db = _store(tmp_path, f"{whole}-{untouched}.db")
            for i in range(untouched):
                _add(db, f"desk{i}@corp.example", f"desk{i}@corp.example")
            _passed(db)
            _add(db, "news@corp.example", "news@corp.example")
            statements.clear()
            run_dedup(db, touched=None if whole else set())
            counts[whole, untouched] = len(statements)

    assert counts[False, 10] == counts[False, 100]
    assert counts[True, 100] > counts[True, 10]


def test_a_store_no_pass_has_run_on_is_taken_whole(tmp_path):
    db = _store(tmp_path)
    keep = _add(db, "Maria Papadopoulou")
    _add(db, "Maria Papadopoulou")

    result = run_dedup(db, touched=set())

    assert _ids(db) == {keep}
    assert result["scope"] is None
    assert _metadata(db, HIGH_WATER_KEY) == str(keep)
    assert _metadata(db, FULL_AT_KEY) is not None


def test_the_whole_table_is_taken_once_a_day(tmp_path):
    db = _store(tmp_path)
    keep = _add(db, "Maria Papadopoulou")
    _add(db, "Maria Papadopoulou")
    _passed(db, full_hours_ago=dedup_people.FULL_PASS_EVERY_H + 1)

    run_dedup(db, touched=set())

    assert _ids(db) == {keep}


def test_a_scoped_pass_moves_the_high_water_mark_and_not_the_whole_pass_time(tmp_path):
    db = _store(tmp_path)
    _add(db, "Maria Papadopoulou")
    _passed(db)
    before = _metadata(db, FULL_AT_KEY)
    newest = _add(db, "Eleni Ioannou")

    result = run_dedup(db, touched=set())

    assert result["scope"] == 1
    assert _metadata(db, HIGH_WATER_KEY) == str(newest)
    assert _metadata(db, FULL_AT_KEY) == before


def test_a_dry_run_moves_no_mark(tmp_path):
    db = _store(tmp_path)
    _add(db, "Maria Papadopoulou")

    run_dedup(db, dry_run=True)

    assert _metadata(db, HIGH_WATER_KEY) is None
    assert _metadata(db, FULL_AT_KEY) is None


def test_an_id_freed_at_the_top_and_used_again_is_still_taken(tmp_path):
    """SQLite gives the next row the id after the highest one left, so a pass that merges
    the top row away frees its id for the next insert. The mark must sit below it."""
    db = _store(tmp_path)
    v = _add(db, "Eleni Ioannou", "ei@corp.example")
    u = _add(db, "Nikos Papas", "np@corp.example")
    top = _add(db, "Nikos Papas")  # merged into u by the pass below
    run_dedup(db)
    assert _ids(db) == {v, u}

    again = _add(db, "Eleni Ioannou")
    assert again == top  # the premise: the freed id is taken again

    run_dedup(db, touched=set())

    assert _ids(db) == {v, u}


# --------------------------------------------------------- the same end as a whole pass

FIRST = [("Nikos", "Νίκος"), ("Maria", "Μαρία"), ("Eleni", "Ελένη")]
LAST = [("Papadopoulou", "Παπαδοπούλου"), ("Georgiou", "Γεωργίου"), ("Ioannou", "Ιωάννου")]


def _name(rng):
    script = rng.randrange(2)
    first, last = rng.choice(FIRST)[script], rng.choice(LAST)[script]
    return rng.choice(
        [
            f"{first} {last}",
            f"{last} {first}",
            f"{first.upper()} {last.upper()}",
            f"{first} {last} (E{rng.randrange(10000, 99999)})",
            f"{first.lower()}.{last.lower()}@corp.example",
        ]
    )


def _person(rng, used):
    name = _name(rng)
    if "@" in name and rng.random() < 0.5:
        email = name
    elif rng.random() < 0.3:
        email = None
    else:
        email = f"u{rng.randrange(60)}@{rng.choice(['gmail.com', 'corp.example'])}"
    if email in used:
        email = None
    if email:
        used.add(email)
    return name, email


def _links(db):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT email_id, person_id, role_in_email FROM email_people ORDER BY 1, 2, 3"
    ).fetchall()
    conn.close()
    return rows


def _settle(db):
    """Whole passes until one changes nothing: the state a store is in between syncs."""
    for _ in range(10):
        before = (_people(db), _links(db))
        run_dedup(db)
        if (_people(db), _links(db)) == before:
            return
    raise AssertionError("the whole pass did not settle")


@pytest.mark.parametrize("seed", [148, 149, 150, 151])
def test_a_scoped_pass_ends_where_a_whole_pass_ends(tmp_path, capsys, seed):
    """On a settled store, the people and links a scoped pass leaves are the ones a whole
    pass leaves: narrowing changes how much is read, not what is merged."""
    rng = random.Random(seed)
    db = _store(tmp_path)
    used: set[str] = set()
    conn = sqlite3.connect(db)
    for n in range(60):
        name, email = _person(rng, used)
        pid = conn.execute(
            "INSERT INTO people (name, email) VALUES (?, ?)", (name, email)
        ).lastrowid
        conn.execute(
            "INSERT INTO emails (message_id, date_received, subject) VALUES (?, '2026-10-01', 's')",
            (f"m{n}",),
        )
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, ?, 'sender')",
            (n + 1, pid),
        )
    conn.commit()
    conn.close()
    _settle(db)

    touched: set[int] = set()
    conn = sqlite3.connect(db)
    for _ in range(15):  # what a load adds
        name, email = _person(rng, used)
        pid = conn.execute(
            "INSERT INTO people (name, email) VALUES (?, ?)", (name, email)
        ).lastrowid
        conn.execute(
            "INSERT INTO email_people (email_id, person_id, role_in_email) "
            "VALUES (?, ?, 'recipient')",
            (rng.randrange(1, 61), pid),
        )
    existing = [r[0] for r in conn.execute("SELECT id FROM people ORDER BY id")]
    for pid in rng.sample(existing, 4):  # a display name replacing an address
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (_name(rng).split("@")[0], pid))
        touched.add(pid)
    for (pid,) in conn.execute("SELECT id FROM people WHERE email IS NULL LIMIT 3").fetchall():
        address = f"late{pid}@corp.example"  # an address given to a namesake that had none
        conn.execute("UPDATE people SET email = ? WHERE id = ?", (address, pid))
        touched.add(pid)
    # The address an untouched row is named after, given to a row with a real name, which
    # phase 5 then takes for that row's match.
    named_after = conn.execute(
        "SELECT name FROM people WHERE name LIKE '%@%' AND email IS NULL"
    ).fetchall()
    takers = conn.execute(
        "SELECT id FROM people WHERE email IS NULL AND name NOT LIKE '%@%'"
    ).fetchall()
    for (address,), (pid,) in zip(named_after[:2], takers[:2], strict=False):
        if address.lower() not in used:
            used.add(address.lower())
            conn.execute("UPDATE people SET email = ? WHERE id = ?", (address.lower(), pid))
            touched.add(pid)
    conn.commit()
    conn.close()
    twin = str(tmp_path / "twin.db")
    shutil.copyfile(db, twin)

    assert run_dedup(db, touched=touched)["total_reduced"] > 0
    run_dedup(twin)

    assert _people(db) == _people(twin)
    assert _links(db) == _links(twin)


# ------------------------------------------------------------------------- the sync side


def test_the_load_reports_the_people_it_added_or_changed(tmp_path):
    import json

    from src.store.loader import load_extractions

    db = tmp_path / "b.db"
    conn = create_database(str(db))
    linked = conn.execute(
        "INSERT INTO people (name, email) VALUES ('Nikos Papas', 'np@corp.example')"
    ).lastrowid
    renamed = conn.execute(
        "INSERT INTO people (name, email) VALUES ('mg@corp.example', 'mg@corp.example')"
    ).lastrowid
    conn.commit()
    conn.close()
    staging, extracted = tmp_path / "staging", tmp_path / "extracted"
    staging.mkdir()
    extracted.mkdir()
    email = {
        "message_id": "AAMk-1",
        "date_received": "2026-10-10T08:00:00Z",
        "subject": "figures",
        "sender": {"name": "Nikos Papas", "address": "np@corp.example"},
        "to_recipients": [
            {"name": "Maria Georgiou", "address": "mg@corp.example"},
            {"name": "Eleni Ioannou", "address": "ei@corp.example"},
        ],
        "mailbox_name": "Inbox",
        "content": "hello",
    }
    (staging / "batch-00001.json").write_text(json.dumps({"emails": [email]}))
    (extracted / "AAMk-1.json").write_text(json.dumps({"summary": "s"}))
    touched: set[int] = set()

    assert load_extractions(str(db), str(extracted), str(staging), touched_people=touched) == 1

    conn = sqlite3.connect(db)
    (added,) = conn.execute("SELECT id FROM people WHERE email = 'ei@corp.example'").fetchone()
    conn.close()
    assert touched == {renamed, added}
    assert linked not in touched


@pytest.fixture
def synced_store(tmp_path, monkeypatch):
    from src import llm_deadline

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.pipeline.sync.DATA_ROOT", tmp_path)
    monkeypatch.setattr("src.pipeline.sync.ATTACHMENTS_DIR", tmp_path / "attachments")
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda: None)
    return db_path


def _sync(db_path, load, **flags):
    from src import cli

    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=True, **flags
    )
    ok = {"extracted": 0, "failed": 0, "quota_paused": False}
    registered = {"ids": [], "registered": 0, "scanned": 0, "deferred": 0}
    with (
        patch("src.extract.local.run_extraction", return_value=ok),
        patch("src.store.loader.load_extractions", side_effect=load),
        patch(
            "src.export.outlook_attachments.register_downloaded_attachments",
            return_value=registered,
        ),
        patch("src.store.dedup_people.run_dedup", return_value={"total_reduced": 0}) as dedup,
        patch("src.store.embeddings.build_index"),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        cli.cmd_sync(args)
    return dedup


def test_sync_hands_the_dedup_the_people_its_load_wrote(synced_store):
    def load(*args, touched_people=None, **kwargs):
        touched_people.update({7, 9})
        return 1

    dedup = _sync(synced_store, load)

    dedup.assert_called_once()
    assert dedup.call_args.kwargs["touched"] == {7, 9}


def test_full_dedup_takes_the_whole_table_even_when_no_mail_loaded(synced_store):
    dedup = _sync(synced_store, lambda *args, **kwargs: 0, full_dedup=True)

    dedup.assert_called_once()
    assert dedup.call_args.kwargs["touched"] is None


def test_without_mail_or_the_flag_there_is_no_dedup(synced_store):
    dedup = _sync(synced_store, lambda *args, **kwargs: 0)

    dedup.assert_not_called()


def test_full_dedup_reaches_cmd_sync_from_the_command_line(monkeypatch, tmp_path):
    from src import cli

    seen = []
    monkeypatch.setattr(cli, "cmd_sync", lambda args: seen.append(args.full_dedup))
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    for argv, expected in (([], False), (["--full-dedup"], True)):
        monkeypatch.setattr(
            sys, "argv", ["brain", "--db", str(tmp_path / "brain.db"), "sync", *argv]
        )
        cli.main()
        assert seen[-1] is expected
