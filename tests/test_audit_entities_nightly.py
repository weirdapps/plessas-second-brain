"""New people get entities in the nightly sync.

Entities were seeded once, by hand, on 2026-10-16 (`brain maint entities --apply`), and
nothing seeded them again, so the 69 people added since had none (audit 2026-10-20). The
seed reads every person, 37 s cold on the producer, so the nightly sb-daily-sync runs it
after the people dedup and the hourly units leave it to that run. Synthetic people only.
"""

import inspect

import pytest

from src.pipeline import sync
from src.store.schema import create_database
from tests.test_sync_budget import _sync_command_s


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute("INSERT INTO people (name, email) VALUES ('Alpha Example', 'alpha@example.com')")
    conn.commit()
    conn.close()
    return str(path)


def _entities(path):
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT e.name FROM entities e JOIN entity_aliases a ON a.entity_id = e.id"
            " WHERE a.kind = 'address'"
        ).fetchall()
    finally:
        conn.close()


def test_the_nightly_unit_seeds_entities_for_new_people(store, monkeypatch):
    monkeypatch.setattr("src.llm_deadline._detect_systemd_unit", lambda: "sb-daily-sync")

    seeded = sync._seed_entities_nightly(store)

    assert seeded == {"entities": 1, "aliases": 2}
    assert _entities(store) == [("Alpha Example",)]
    # A second night finds nothing new.
    assert sync._seed_entities_nightly(store) == {"entities": 0, "aliases": 0}


def test_the_nightly_seed_plans_outside_the_write_lock(store, monkeypatch):
    """The plan reads every person, 37 s cold on the producer on 2026-10-21, and
    seed_entities makes it under BEGIN IMMEDIATE, which held the hourly writers (60 s
    busy timeout) for all of it. Only the write takes the lock."""
    import sqlite3

    from src.store import entities

    monkeypatch.setattr("src.llm_deadline._detect_systemd_unit", lambda: "sb-daily-sync")
    real_plan = entities.plan_seed
    others_could_write = []

    def planning(conn):
        other = sqlite3.connect(store, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            others_could_write.append(True)
        except sqlite3.OperationalError:
            others_could_write.append(False)
        finally:
            other.close()
        return real_plan(conn)

    monkeypatch.setattr("src.store.entities.plan_seed", planning)

    assert sync._seed_entities_nightly(store) == {"entities": 1, "aliases": 2}
    assert others_could_write == [True]


@pytest.mark.parametrize("unit", ["sb-outlook-sync", "sb-noon-catchup", None])
def test_the_other_runs_leave_it_to_the_nightly_one(store, monkeypatch, unit):
    monkeypatch.setattr("src.llm_deadline._detect_systemd_unit", lambda: unit)

    assert sync._seed_entities_nightly(store) is None
    assert _entities(store) == []


def test_a_failed_seed_fails_no_sync(store, monkeypatch, capsys):
    monkeypatch.setattr("src.llm_deadline._detect_systemd_unit", lambda: "sb-daily-sync")

    def broken(conn):
        raise RuntimeError("seed broke")

    monkeypatch.setattr("src.store.entities.plan_seed", broken)

    assert sync._seed_entities_nightly(store) is None
    assert "seed broke" in capsys.readouterr().err


def test_the_sync_calls_it_after_the_people_dedup():
    source = inspect.getsource(sync.run)
    assert source.index("run_dedup(") < source.index("_seed_entities_nightly(")


@pytest.mark.parametrize(("unit", "held_back"), [("sb-daily-sync", 60.0), ("sb-outlook-sync", 0.0)])
def test_the_nightly_unit_holds_the_seed_back_from_extraction(monkeypatch, unit, held_back):
    """Step 4b runs after Step 2, so in the nightly unit Step 2 leaves it its time; the
    hourly units do not run it and keep theirs."""
    now = 1_000_000.0
    monkeypatch.setattr("src.llm_deadline._detect_systemd_unit", lambda: unit)
    monkeypatch.setattr("time.time", lambda: now)
    left = sync.SYNC_AFTER_EXTRACTION_S + 20.0 + held_back
    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(now + left))

    assert sync._extract_deadline_s() == pytest.approx(20.0)


def test_the_nightly_unit_still_fits_with_the_seed():
    from src.llm_deadline import _UNIT_TIMEOUT_SECONDS

    worst = _sync_command_s(sync.EXTRACT_DEADLINE_BY_UNIT_S["sb-daily-sync"])
    assert worst + sync.ENTITY_SEED_OBSERVED_S < _UNIT_TIMEOUT_SECONDS["sb-daily-sync"] * 0.85
