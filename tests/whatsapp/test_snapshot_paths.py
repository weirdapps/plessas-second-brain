"""The snapshot builder writes only where a snapshot belongs, and reads only the bridge store.

SonarCloud flagged the builder on PR #105 (pythonsecurity:S8707, S8706): both paths
came from the command line and went unchecked into os.remove() and into a sqlite3
URI. The wrapper always passes a private temp directory, but the script can also be
run by hand or by an agent, and building a snapshot deletes whatever is at DEST. So
DEST must be whatsapp-snapshot.db inside a private directory of this user's under
the temp directory, and SOURCE must be an existing messages.db, opened read-only
through a URI that pathlib builds, which percent-encodes every character a URI
could read as a parameter.
"""

import os
import subprocess
import sys
from pathlib import Path

from tests.whatsapp.conftest import ALICE, DIRECT_JID, make_bridge_store

SCRIPT = Path(__file__).parents[2] / "scripts" / "whatsapp_snapshot.py"
SENTINEL = b"not a snapshot; must survive\n"
MESSAGES = [("m1", DIRECT_JID, ALICE, "hello", "2026-09-01 10:00:00+03:00", 0, "", "")]


def _run(source: Path, dest: Path, temp_root: Path):
    env = {**os.environ, "TMPDIR": str(temp_root)}
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(dest)],
        capture_output=True,
        text=True,
        env=env,
    )


def _world(tmp_path: Path):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir(mode=0o700)
    private = temp_root / "whatsapp-sync.abc123"
    private.mkdir(mode=0o700)
    bridge = tmp_path / "bridge"
    bridge.mkdir(mode=0o700)
    source = make_bridge_store(bridge / "messages.db", MESSAGES)
    return temp_root, private, source


def test_the_wrappers_layout_is_accepted(tmp_path):
    temp_root, private, source = _world(tmp_path)
    out = _run(source, private / "whatsapp-snapshot.db", temp_root)
    assert out.returncode == 0, out.stderr
    assert (private / "whatsapp-snapshot.db").stat().st_mode & 0o077 == 0


def test_a_dest_outside_the_temp_directory_is_refused_and_left_alone(tmp_path):
    temp_root, _private, source = _world(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    victim = elsewhere / "whatsapp-snapshot.db"
    victim.write_bytes(SENTINEL)
    out = _run(source, victim, temp_root)
    assert out.returncode == 64
    assert victim.read_bytes() == SENTINEL


def test_a_dest_with_another_name_is_refused_and_left_alone(tmp_path):
    temp_root, private, source = _world(tmp_path)
    victim = private / "notes.db"
    victim.write_bytes(SENTINEL)
    out = _run(source, victim, temp_root)
    assert out.returncode == 64
    assert victim.read_bytes() == SENTINEL


def test_a_dest_in_a_shared_directory_is_refused(tmp_path):
    temp_root, _private, source = _world(tmp_path)
    shared = temp_root / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    out = _run(source, shared / "whatsapp-snapshot.db", temp_root)
    assert out.returncode == 64
    assert not (shared / "whatsapp-snapshot.db").exists()


def test_a_dest_directly_in_the_temp_directory_is_refused(tmp_path):
    temp_root, _private, source = _world(tmp_path)
    out = _run(source, temp_root / "whatsapp-snapshot.db", temp_root)
    assert out.returncode == 64
    assert not (temp_root / "whatsapp-snapshot.db").exists()


def test_a_source_that_is_not_the_bridge_store_is_refused(tmp_path):
    temp_root, private, source = _world(tmp_path)
    other = source.with_name("contacts.db")
    other.write_bytes(source.read_bytes())
    out = _run(other, private / "whatsapp-snapshot.db", temp_root)
    assert out.returncode == 66
    assert not (private / "whatsapp-snapshot.db").exists()


def test_uri_characters_in_the_source_path_stay_part_of_the_path(tmp_path):
    """A directory named like URI parameters must not become parameters.

    Were the path pasted into the URI unencoded, "?mode=rwc" would reopen the
    store read-write and "#" would cut the path short.
    """
    temp_root, private, _source = _world(tmp_path)
    odd = tmp_path / "we?mode=rwc&cache=shared#frag"
    odd.mkdir(mode=0o700)
    source = make_bridge_store(odd / "messages.db", MESSAGES)
    before = (source.stat().st_mtime_ns, source.read_bytes())
    out = _run(source, private / "whatsapp-snapshot.db", temp_root)
    assert out.returncode == 0, out.stderr
    assert (source.stat().st_mtime_ns, source.read_bytes()) == before
