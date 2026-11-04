"""Jobs refuse a store the code is ahead of (issue 173).

The numbered migrations run once, from scripts/deploy.sh (`brain maint migrate`), with the
timers stopped around them. A job that opens a store at an older version stops with an
error naming both versions instead of migrating it under the timers. A new store is built
whole, and BRAIN_MIGRATE_ON_OPEN=1 brings the old behaviour back on a host with no deploy
step. Every value is invented.
"""

import re
import sqlite3
from pathlib import Path

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.schema import (
    SchemaBehind,
    create_database,
    ensure_schema,
    get_schema_version,
    set_schema_version,
)
from tests.brain_cli import brain

ROOT = Path(__file__).resolve().parent.parent


def _behind(path):
    db = create_database(str(path))
    set_schema_version(db, CURRENT_SCHEMA_VERSION - 1)
    return db


def test_a_current_store_opens(tmp_path):
    db = create_database(str(tmp_path / "b.db"))
    ensure_schema(db)
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION


def test_a_behind_store_is_refused_and_left_as_it_is(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MIGRATE_ON_OPEN", "0")
    db = _behind(tmp_path / "b.db")
    with pytest.raises(SchemaBehind) as exc:
        ensure_schema(db)
    assert f"v{CURRENT_SCHEMA_VERSION - 1}" in str(exc.value)
    assert f"v{CURRENT_SCHEMA_VERSION}" in str(exc.value)
    assert "deploy.sh" in str(exc.value)
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION - 1


def test_migrate_on_open_brings_back_the_lazy_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MIGRATE_ON_OPEN", "1")
    db = _behind(tmp_path / "b.db")
    ensure_schema(db)
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION


def test_a_job_on_a_behind_store_exits_1_and_migrates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRAIN_MIGRATE_ON_OPEN", "0")
    path = tmp_path / "b.db"
    _behind(path).close()
    code, _, err = brain(monkeypatch, capsys, "--db", str(path), "maint", "classify-sources")
    assert code == 1
    assert "deploy.sh" in err
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION - 1
    db.close()


def test_brain_maint_migrate_still_migrates(tmp_path, monkeypatch, capsys):
    path = tmp_path / "b.db"
    _behind(path).close()
    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "maint", "migrate")
    assert code == 0 and "Migrated" in out
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    assert get_schema_version(db) == CURRENT_SCHEMA_VERSION
    db.close()


# The only places that may call run_migrations: the store's own builder and migrator, and
# `brain maint migrate`. Every job goes through ensure_schema.
ALLOWED = {
    "src/store/schema.py",
    "src/cli.py:cmd_migrate",
}


def test_no_job_calls_run_migrations_directly():
    offenders = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        func = None
        for n, line in enumerate(path.read_text().splitlines(), 1):
            m = re.match(r"def (\w+)", line)
            if m:
                func = m.group(1)
            if re.search(r"\brun_migrations\(", line) and not line.lstrip().startswith("#"):
                if f"{rel}:{func}" not in ALLOWED:
                    offenders.append(f"{rel}:{n}")
    assert offenders == []
