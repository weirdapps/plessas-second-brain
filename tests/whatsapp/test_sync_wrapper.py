"""scripts/wrappers/launchd/sync-whatsapp-to-vps.sh, the hourly push from the Mac.

ssh and scp are stubbed with scripts that act on a directory standing in for
the producer's home, so the tests see exactly what would land there, with which
modes, and what the job wrote to its log, without touching a network.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.whatsapp.conftest import ALICE, DIRECT_JID, make_bridge_store

REPO = Path(__file__).parents[2]
WRAPPER = REPO / "scripts" / "wrappers" / "launchd" / "sync-whatsapp-to-vps.sh"
HELPER = REPO / "scripts" / "whatsapp_snapshot.py"
MARKER = "zqx-private-marker-4412"

SSH_STUB = """#!/bin/bash
# Drop options, take the host, run the rest in the fake remote home.
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
[ -n "${SSH_DOWN:-}" ] && exit 255
cd "$REMOTE_HOME" && HOME="$REMOTE_HOME" bash -c "$*"
"""

SCP_STUB = """#!/bin/bash
[ -n "${SSH_DOWN:-}" ] && exit 1
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
src="${args[0]}"; dest="${args[1]#*:}"
cp -p "$src" "$REMOTE_HOME/$dest"
"""


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    remote = tmp_path / "remote"
    bindir = tmp_path / "bin"
    for d in (home, remote, bindir):
        d.mkdir()
    for name, body in (("ssh", SSH_STUB), ("scp", SCP_STUB)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    source = make_bridge_store(
        tmp_path / "messages.db",
        [("m1", DIRECT_JID, ALICE, f"hi {MARKER}", "2026-09-01 10:00:00+03:00", 0, "", "")],
    )
    log = tmp_path / "sync.log"
    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "REMOTE_HOME": str(remote),
        "SB_WHATSAPP_SOURCE": str(source),
        "SB_WHATSAPP_HELPER": str(HELPER),
        "SB_WHATSAPP_PYTHON": shutil.which("python3") or "/usr/bin/python3",
        "SB_WHATSAPP_LOG": str(log),
        # Named transport stubs AND a host that cannot resolve. The first version
        # of this file relied on PATH alone, the wrapper's own PATH line put
        # /usr/bin ahead of the stubs, and a run reached the real producer.
        "SB_WHATSAPP_SSH": str(bindir / "ssh"),
        "SB_WHATSAPP_SCP": str(bindir / "scp"),
        "SB_WHATSAPP_VPS": "sb-whatsapp-test.invalid",
        "TMPDIR": str(tmp_path),
    }
    return {"env": env, "home": home, "remote": remote, "log": log, "tmp": tmp_path}


def _run(world, **extra):
    env = {**world["env"], **extra}
    return subprocess.run(["/bin/bash", str(WRAPPER)], env=env, capture_output=True, text=True)


def test_the_snapshot_lands_owner_only_under_its_final_name(world):
    out = _run(world)
    assert out.returncode == 0, world["log"].read_text()
    remote_dir = world["remote"] / ".second-brain" / "whatsapp"
    snapshot = remote_dir / "whatsapp-snapshot.db"
    assert snapshot.is_file()
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(remote_dir.stat().st_mode) == 0o700
    assert list(remote_dir.iterdir()) == [snapshot]  # no .part left behind


def test_the_wrapper_logs_no_message_content(world):
    out = _run(world)
    log = world["log"].read_text()
    assert "1 messages" in log or '"messages": 1' in log
    for text in (log, out.stdout, out.stderr):
        assert MARKER not in text
        assert "hi " not in text


def test_success_stamps_both_hosts_and_clears_a_failure(world):
    (world["home"] / ".second-brain").mkdir()
    (world["home"] / ".second-brain" / "whatsapp-sync.fail").write_text("x\nold\n")
    _run(world)
    assert (world["home"] / ".second-brain" / "whatsapp-sync.stamp").is_file()
    assert (world["remote"] / ".second-brain" / "whatsapp-sync.stamp").is_file()
    assert not (world["home"] / ".second-brain" / "whatsapp-sync.fail").exists()


def test_no_copy_of_the_snapshot_is_left_on_the_mac(world):
    _run(world)
    leftovers = [p for p in world["tmp"].rglob("whatsapp-snapshot.db*") if "remote" not in p.parts]
    assert leftovers == []


def test_a_failed_build_writes_the_failure_marker_on_both_hosts(world):
    out = _run(world, SB_WHATSAPP_SOURCE=str(world["tmp"] / "absent.db"))
    assert out.returncode == 1
    local = (world["home"] / ".second-brain" / "whatsapp-sync.fail").read_text()
    assert "snapshot" in local
    assert (world["remote"] / ".second-brain" / "whatsapp-sync.fail").is_file()


def test_an_unset_store_path_fails_loudly(world):
    env = {k: v for k, v in world["env"].items() if k != "SB_WHATSAPP_SOURCE"}
    out = subprocess.run(["/bin/bash", str(WRAPPER)], env=env, capture_output=True, text=True)
    assert out.returncode == 1
    assert (
        "SB_WHATSAPP_SOURCE" in (world["home"] / ".second-brain" / "whatsapp-sync.fail").read_text()
    )


def test_an_unreachable_vps_is_skipped_quietly_at_first(world):
    out = _run(world, SSH_DOWN="1")
    assert out.returncode == 0
    assert "skipping" in world["log"].read_text()
    assert not (world["home"] / ".second-brain" / "whatsapp-sync.fail").exists()


def test_a_long_outage_pages_through_the_failure_marker(world):
    for _ in range(12):
        out = _run(world, SSH_DOWN="1")
    assert out.returncode == 1
    reason = (world["home"] / ".second-brain" / "whatsapp-sync.fail").read_text()
    assert "unreachable" in reason


def test_it_parses_with_the_interpreter_it_names():
    assert WRAPPER.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["/bin/bash", "-n", str(WRAPPER)], check=True)


def test_the_launchd_template_runs_it_hourly_at_minute_50():
    plist = WRAPPER.parent / "com.plessas.whatsapp-sync-vps.plist"
    text = plist.read_text()
    assert "<string>com.plessas.whatsapp-sync-vps</string>" in text
    assert "<key>Minute</key>" in text and "<integer>50</integer>" in text
    assert "sync-whatsapp-to-vps.sh" in text
    assert "<key>SB_WHATSAPP_SOURCE</key>" in text
    if shutil.which("plutil"):
        subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True)
    assert os.path.basename(WRAPPER) in text
