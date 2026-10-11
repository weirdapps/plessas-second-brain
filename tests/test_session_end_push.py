"""A session's transcript reaches the producer when the session ends.

scripts/hooks/session-end-push.sh is the Claude Code SessionEnd hook: it reads the
hook's JSON on stdin, leaves agent sessions out (a subagent's, an in-process
teammate's, a split-pane teammate's, a scripted run's), and starts
scripts/wrappers/launchd/sync-transcripts-to-vps.sh in the background, detached,
so the session's 1.5 s budget is never spent on the network.

The push sends that one transcript, keeps the WhatsApp push's stamp and failure
marker contract, and keeps what it could not send for the next push. ssh and
rsync are stubbed with scripts that act on a directory standing in for the
producer's home. Every transcript here is synthetic.
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
HOOK = REPO / "scripts" / "hooks" / "session-end-push.sh"
PUSH = REPO / "scripts" / "wrappers" / "launchd" / "sync-transcripts-to-vps.sh"
MARKER = "zqx-private-transcript-7731"
PROJECT = "-work-okapi"
SESSION = "0b7e3c1a-1111-4222-8333-944455556666"

PUSH_STUB = """#!/bin/bash
printf '%s\\n' "$@" >> "$RECORD"
"""

SLOW_PUSH_STUB = """#!/bin/bash
sleep 5
printf '%s\\n' "$@" >> "$RECORD"
"""

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

RSYNC_STUB = """#!/bin/bash
[ -n "${RSYNC_FAIL:-}" ] && exit 23
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -e) shift 2 ;;
    -*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
src="${args[0]}"; dest="${args[1]#*:}"
cp -p "$src" "$REMOTE_HOME/$dest"
"""


def _script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _transcript(root: Path, *parts: str, team: bool = False) -> Path:
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": "user", "message": {"role": "user", "content": f"plan it {MARKER}"}}
    if team:
        record |= {"agentName": "w1", "teamName": "audit"}
    # An owner's session may quote the key in a message: escaped, it is not a key.
    quoted = {"type": "user", "message": {"role": "user", "content": 'the "teamName":"x" key'}}
    path.write_text(json.dumps(record) + "\n" + json.dumps(quoted) + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    remote = tmp_path / "remote"
    bindir = tmp_path / "bin"
    for d in (home, remote, bindir, remote / ".claude" / "projects"):
        d.mkdir(parents=True)
    projects = home / ".claude" / "projects"
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "REMOTE_HOME": str(remote),
        "RECORD": str(tmp_path / "pushed.txt"),
        "SB_TRANSCRIPTS_LOG": str(tmp_path / "push.log"),
        # Named transport stubs AND a host that cannot resolve, as the WhatsApp
        # push's tests do: a PATH stub alone once reached the real producer.
        "SB_TRANSCRIPTS_SSH": str(_script(bindir / "ssh", SSH_STUB)),
        "SB_TRANSCRIPTS_RSYNC": str(_script(bindir / "rsync", RSYNC_STUB)),
        "SB_TRANSCRIPTS_VPS": "sb-transcripts-test.invalid",
        "SB_TRANSCRIPTS_PUSH": str(_script(bindir / "push", PUSH_STUB)),
        "TMPDIR": str(tmp_path),
    }
    return {
        "env": env,
        "home": home,
        "remote": remote,
        "projects": projects,
        "bin": bindir,
        "record": tmp_path / "pushed.txt",
        "log": tmp_path / "push.log",
    }


# ------------------------------------------------------------------ the hook


def _hook(world, payload: dict, **extra):
    env = {**world["env"], **extra}
    start = time.monotonic()
    out = subprocess.run(
        ["/bin/bash", str(HOOK)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return out, time.monotonic() - start


def _pushed(world, wait: float = 5.0) -> list[str]:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if world["record"].exists():
            return world["record"].read_text().split()
        time.sleep(0.05)
    return []


def _payload(path: Path, **fields) -> dict:
    return {
        "session_id": SESSION,
        "transcript_path": str(path),
        "cwd": "/work/okapi",
        "hook_event_name": "SessionEnd",
        "reason": "prompt_input_exit",
        **fields,
    }


def test_the_hook_pushes_the_owners_transcript(world):
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")

    out, _ = _hook(world, _payload(path))

    assert out.returncode == 0
    assert _pushed(world) == [str(path)]
    assert out.stdout == out.stderr == ""


@pytest.mark.parametrize(
    "fields, extra",
    [
        ({"agent_type": "Explore", "agent_id": "a1b2"}, {}),
        ({"agent_id": "a1b2"}, {}),
        ({}, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}),
    ],
    ids=["subagent", "in-process-teammate", "scripted-run"],
)
def test_the_hook_leaves_agent_sessions_out(world, fields, extra):
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")

    out, _ = _hook(world, _payload(path, **fields), **extra)

    assert out.returncode == 0
    assert "skip" in world["log"].read_text()
    assert _pushed(world, wait=0.5) == []


def test_the_hook_leaves_a_teammates_session_out(world):
    """A split-pane teammate is its own process; its records name its team."""
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl", team=True)

    _hook(world, _payload(path))

    assert "skip" in world["log"].read_text()
    assert _pushed(world, wait=0.5) == []


def test_the_hook_never_sends_a_subagent_transcript(world):
    path = _transcript(world["projects"], PROJECT, SESSION, "subagents", "agent-a1.jsonl")

    _hook(world, _payload(path))

    assert _pushed(world, wait=0.5) == []


@pytest.mark.parametrize("payload", [{}, {"transcript_path": "/nowhere/x.jsonl"}, "not json"])
def test_the_hook_does_nothing_without_a_transcript(world, payload):
    out = subprocess.run(
        ["/bin/bash", str(HOOK)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        env=world["env"],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert out.returncode == 0
    assert _pushed(world, wait=0.5) == []


def test_the_hook_returns_within_the_budget_while_the_push_runs_on(world):
    """Claude Code gives every SessionEnd hook 1.5 s together. The push is
    detached: holding the hook's stdout or stderr open would make the caller wait."""
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")
    slow = _script(world["bin"] / "slow-push", SLOW_PUSH_STUB)

    out, took = _hook(world, _payload(path), SB_TRANSCRIPTS_PUSH=str(slow))

    assert out.returncode == 0
    assert took < 1.5
    assert _pushed(world, wait=10) == [str(path)]


def test_the_hook_parses_with_the_interpreter_it_names():
    assert HOOK.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["/bin/bash", "-n", str(HOOK)], check=True)


# ------------------------------------------------------------------ the push


def _push(world, *args, **extra):
    env = {**world["env"], **extra}
    return subprocess.run(
        ["/bin/bash", str(PUSH), *args], env=env, capture_output=True, text=True, timeout=30
    )


def _landed(world, name=f"{SESSION}.jsonl") -> Path:
    return world["remote"] / ".claude" / "projects" / PROJECT / name


def test_the_transcript_lands_where_the_producer_reads_it_owner_only(world):
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")

    out = _push(world, str(path))

    assert out.returncode == 0, world["log"].read_text()
    assert _landed(world).read_bytes() == path.read_bytes()
    assert stat.S_IMODE(_landed(world).stat().st_mode) == 0o600
    assert stat.S_IMODE(_landed(world).parent.stat().st_mode) == 0o700


def test_success_stamps_both_hosts_and_clears_a_failure(world):
    state = world["home"] / ".second-brain"
    state.mkdir()
    (state / "transcripts-sync.fail").write_text("x\nold\n")
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")

    _push(world, str(path))

    assert (state / "transcripts-sync.stamp").is_file()
    assert (world["remote"] / ".second-brain" / "transcripts-sync.stamp").is_file()
    assert not (state / "transcripts-sync.fail").exists()


@pytest.mark.parametrize(
    "parts",
    [
        (PROJECT, SESSION, "subagents", "agent-a1.jsonl"),
        ("elsewhere", "notes.jsonl"),
    ],
    ids=["subagent", "outside-projects"],
)
def test_only_a_sessions_own_transcript_is_sent(world, parts):
    root = world["projects"] if parts[0] == PROJECT else world["home"]
    path = _transcript(root, *parts)

    out = _push(world, str(path))

    assert out.returncode == 0
    assert not any((world["remote"] / ".claude" / "projects").rglob("*.jsonl"))
    assert not (world["home"] / ".second-brain" / "transcripts-sync.stamp").exists()


def test_the_log_carries_no_transcript_text(world):
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")

    out = _push(world, str(path))

    for text in (world["log"].read_text(), out.stdout, out.stderr):
        assert MARKER not in text


def test_a_transcript_that_could_not_go_waits_for_the_next_push(world):
    first = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")
    second = _transcript(world["projects"], PROJECT, "second.jsonl")

    out = _push(world, str(first), SSH_DOWN="1")
    assert out.returncode == 0
    assert "skipping" in world["log"].read_text()
    assert not _landed(world).exists()

    out = _push(world, str(second))
    assert out.returncode == 0, world["log"].read_text()
    assert _landed(world).is_file() and _landed(world, "second.jsonl").is_file()
    assert not (world["home"] / ".second-brain" / "transcripts-sync.pending").exists()


def test_a_failed_push_writes_the_failure_marker_on_both_hosts(world):
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")

    out = _push(world, str(path), RSYNC_FAIL="1")

    assert out.returncode == 1
    state = world["home"] / ".second-brain"
    assert "did not reach" in (state / "transcripts-sync.fail").read_text()
    assert (world["remote"] / ".second-brain" / "transcripts-sync.fail").is_file()
    assert str(path) in (state / "transcripts-sync.pending").read_text()


def test_a_long_outage_pages_through_the_failure_marker(world):
    path = _transcript(world["projects"], PROJECT, f"{SESSION}.jsonl")
    for _ in range(12):
        out = _push(world, str(path), SSH_DOWN="1")

    assert out.returncode == 1
    reason = (world["home"] / ".second-brain" / "transcripts-sync.fail").read_text()
    assert "unreachable" in reason
    pending = (world["home"] / ".second-brain" / "transcripts-sync.pending").read_text()
    assert pending.split() == [str(path)]


def test_the_push_parses_with_the_interpreter_it_names():
    assert PUSH.read_text().startswith("#!/bin/bash\n")
    subprocess.run(["/bin/bash", "-n", str(PUSH)], check=True)
    assert os.access(PUSH, os.R_OK)
