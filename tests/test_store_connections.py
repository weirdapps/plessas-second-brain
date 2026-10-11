"""How the store is opened for reading: read-only everywhere, with locks unless nothing writes.

Every reader opened the store read-write, a replica included, where a write is
replaced by the next pull or, left in a WAL, replayed over the next copy
(2026-08-29). The read-only open keeps SQLite's locking unless the store is a
pulled copy that nothing writes: the producer serves with BRAIN_ROLE=replica while
its timers write the same file, and immutable=1 there could read a page mid-write.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from src import config
from src.store.schema import get_connection
from src.store.sql_readonly import connect_read_only
from tests.test_secure_delete import _store

needs_permissions = pytest.mark.skipif(
    os.geteuid() == 0, reason="root creates files in a read-only directory"
)


def _pulled_copy(tmp_path: Path, monkeypatch) -> None:
    """The Mac: the pull stamps its copy, and BRAIN_ROLE is unset."""
    stamp = tmp_path / "db-pull.stamp"
    stamp.write_text("2026-10-11T00:00:00+00:00")
    monkeypatch.setattr(config, "REPLICA_STAMP", stamp)
    monkeypatch.delenv("BRAIN_ROLE")


def _serving_on_the_producer(tmp_path: Path, monkeypatch) -> None:
    """The producer's HTTP server: BRAIN_ROLE=replica and no pull stamp."""
    monkeypatch.setattr(config, "REPLICA_STAMP", tmp_path / "no-pull-stamp")
    monkeypatch.setenv("BRAIN_ROLE", "replica")


@contextmanager
def _uncheckpointed_row(path: Path) -> Iterator[None]:
    """A row committed to the WAL and not yet copied into the file, as a timer leaves one."""
    writer = sqlite3.connect(path)
    try:
        writer.execute("INSERT INTO topics (name, display_name) VALUES ('fresh', 'Fresh')")
        writer.commit()
        yield
    finally:
        writer.close()


def _topics(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM topics")}


def _sidecars(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name != path.name)


# ----------------------------------------------------------------- replicas


def test_on_a_replica_get_connection_cannot_write_even_a_writable_file(tmp_path, monkeypatch):
    path = _store(tmp_path)
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    assert os.access(path, os.W_OK)

    conn = get_connection(str(path))
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO topics (name, display_name) VALUES ('t', 'T')")
        # Still what every reader expects: rows by column name, the store's functions.
        assert conn.execute("SELECT sb_fold('ώρας') AS folded").fetchone()["folded"] == "ωρασ"
    finally:
        conn.close()
    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT count(*) FROM topics").fetchone()[0] == 0
    finally:
        check.close()


@pytest.mark.parametrize(
    "role, stamped, pulled",
    [
        pytest.param(None, True, True, id="the Mac: a stamp, no role"),
        pytest.param("replica", True, True, id="a stamped replica"),
        pytest.param(None, False, False, id="a host that builds its own store"),
        pytest.param("replica", False, False, id="the producer's HTTP server"),
        pytest.param("producer", True, False, id="a stamp left on a producer"),
    ],
)
def test_only_a_stamped_host_that_is_not_the_producer_holds_a_pulled_copy(
    tmp_path, monkeypatch, role, stamped, pulled
):
    stamp = tmp_path / "db-pull.stamp"
    if stamped:
        stamp.write_text("2026-10-11T00:00:00+00:00")
    monkeypatch.setattr(config, "REPLICA_STAMP", stamp)
    if role is None:
        monkeypatch.delenv("BRAIN_ROLE")
    else:
        monkeypatch.setenv("BRAIN_ROLE", role)

    assert config.is_pulled_copy() is pulled


# --------------------------------------------------------- the read-only open


def test_the_producer_serving_role_reads_with_locks_even_when_asked_for_immutable(
    tmp_path, monkeypatch
):
    path = _store(tmp_path)
    _serving_on_the_producer(tmp_path, monkeypatch)

    with _uncheckpointed_row(path):
        conn = connect_read_only(path, immutable=True)
        try:
            # Only a locking open reads the WAL a writer is filling.
            assert "fresh" in _topics(conn)
        finally:
            conn.close()


def test_a_pulled_copy_reads_with_locks_unless_the_caller_asks(tmp_path, monkeypatch):
    path = _store(tmp_path)
    _pulled_copy(tmp_path, monkeypatch)

    with _uncheckpointed_row(path):
        conn = connect_read_only(path)
        try:
            assert "fresh" in _topics(conn)
        finally:
            conn.close()


def test_a_pulled_copy_read_as_asked_is_the_file_alone(tmp_path, monkeypatch):
    path = _store(tmp_path)
    _pulled_copy(tmp_path, monkeypatch)

    with _uncheckpointed_row(path):
        # A stray WAL beside the copy the pull verified is not replayed onto it.
        conn = connect_read_only(path, immutable=True)
        try:
            assert "fresh" not in _topics(conn)
        finally:
            conn.close()
    conn = connect_read_only(path, immutable=True)
    try:
        conn.execute("SELECT count(*) FROM emails").fetchone()
    finally:
        conn.close()

    assert _sidecars(path) == []


@needs_permissions
def test_a_pulled_copy_falls_back_to_immutable_when_the_locking_open_fails(tmp_path, monkeypatch):
    # The pull copies brain.db alone. A locking read-only open needs a -shm beside it,
    # and fails where this process cannot create one.
    path = _store(tmp_path)
    _pulled_copy(tmp_path, monkeypatch)
    path.parent.chmod(0o555)
    try:
        conn = connect_read_only(path)
        try:
            assert conn.execute("SELECT count(*) FROM emails").fetchone()[0] == 0
        finally:
            conn.close()
        assert _sidecars(path) == []
    finally:
        path.parent.chmod(0o755)


@needs_permissions
def test_a_store_something_writes_is_never_opened_immutable(tmp_path, monkeypatch):
    path = _store(tmp_path)
    _serving_on_the_producer(tmp_path, monkeypatch)
    path.parent.chmod(0o555)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connect_read_only(path, immutable=True)
    finally:
        path.parent.chmod(0o755)
