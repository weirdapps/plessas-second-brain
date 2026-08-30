"""Tests for the transfer diagnostics in scripts/wrappers/launchd/sync-documents-to-vps.sh.

On 2026-08-28 this job stalled for six hours and its log said almost nothing
useful about why, because every diagnostic in it was written against the wrong
rsync:

  * the failure branch logged `tail -3` of rsync's output. rsync 3.5.0 puts the
    line that NAMES the unreadable file first and a generic "(code 23)" line
    last, and openrsync emits its one error line first and nothing at the end,
    so `tail -3` reliably captured the `--stats` footer, the only part of the
    output with no diagnostic value at all;
  * the success branch parsed `Number of files transferred`, which is
    openrsync's wording. rsync 3.5.0, the one actually on PATH, says
    `Number of regular files transferred`, so the pattern never matched and
    every run logged "(? files transferred)": a real sync and an empty one were
    indistinguishable in the log.

Both were position-and-wording assumptions about an implementation that was
never checked against the implementation. So these tests do not paraphrase the
script: they extract its real sed program, its real grep program and its real
retry loop out of the file and run them against `--stats` and error output
captured verbatim from both rsync 3.5.0 and Apple's openrsync.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_WRAPPER = (
    Path(__file__).parent.parent / "scripts" / "wrappers" / "launchd" / "sync-documents-to-vps.sh"
)

# --- Output captured verbatim from both implementations (paths neutralised) ---

_RSYNC3_STATS = """
Number of files: 3 (reg: 2, dir: 1)
Number of created files: 2 (reg: 2)
Number of deleted files: 0
Number of regular files transferred: 2
Total file size: 12 bytes
Total transferred file size: 12 bytes
Literal data: 12 bytes
Matched data: 0 bytes
File list size: 0
File list generation time: 0.001 seconds
File list transfer time: 0.000 seconds
Total bytes sent: 179
Total bytes received: 54

sent 179 bytes  received 54 bytes  466.00 bytes/sec
total size is 12  speedup is 0.05
"""

_OPENRSYNC_STATS = """Number of files: 3
Number of files transferred: 2
Total file size: 12 B
Total transferred file size: 12 B
Unmatched data: 12 B
Matched data: 0 B
File list size: 67 B
File list generation time: 0.001 seconds
File list transfer time: 0.000 seconds
Total sent: 181 B
Total received: 64 B

sent 181 bytes  received 64 bytes  13535 bytes/sec
total size is 12  speedup is 0.05
"""

_DENIED_FILE = "/Users/u/Documents/National/report.docx"

_RSYNC3_FAILURE = f"""rsync: [sender] send_files failed to open "{_DENIED_FILE}": Permission denied (13)

Number of files: 3 (reg: 2, dir: 1)
Number of created files: 3 (reg: 2, dir: 1)
Number of deleted files: 0
Number of regular files transferred: 2
Total file size: 10 bytes
Total transferred file size: 10 bytes
Literal data: 3 bytes
Matched data: 0 bytes
File list size: 0
File list generation time: 0.008 seconds
File list transfer time: 0.000 seconds
Total bytes sent: 155
Total bytes received: 57

sent 155 bytes  received 57 bytes  424.00 bytes/sec
total size is 10  speedup is 0.05
rsync error: some files/attrs were not transferred (see previous errors) (code 23) at main.c(1394) [sender=3.5.0]
"""

_OPENRSYNC_FAILURE = f"""rsync(8514): error: {_DENIED_FILE}: open (2) in /Users/u: Permission denied
Number of files: 3
Number of files transferred: 1
Total file size: 10 B
Total transferred file size: 3 B
Unmatched data: 3 B
Matched data: 0 B
File list size: 73 B
File list generation time: 0.001 seconds
File list transfer time: 0.000 seconds
Total sent: 141 B
Total received: 70 B

sent 141 bytes  received 70 bytes  16106 bytes/sec
total size is 10  speedup is 0.05
"""

# Apple's openrsync. The `Platform identifier` line is the tell that matters:
# a platform binary can never become its own TCC subject, so it has no grant to
# lose and the tripwire has nothing to warn about.
_CODESIGN_PLATFORM = """Executable=/usr/bin/rsync
Identifier=com.apple.rsync
Format=Mach-O universal (x86_64 arm64e)
CodeDirectory v=20400 size=1000 flags=0x0(none) hashes=26+2 location=embedded
Platform identifier=26
Hash type=sha256 size=32
CandidateCDHash sha256=4814f304003fa39362a86bb791afba32cff40037
CDHash=4814f304003fa39362a86bb791afba32cff40037
Authority=macOS Software Signing
TeamIdentifier=not set
"""

_CODESIGN_ADHOC = """Executable=/opt/homebrew/Cellar/rsync/9.9.9/bin/rsync
Identifier=rsync-0000000000000000000000000000000000000000
Format=Mach-O thin (arm64)
CodeDirectory v=20400 size=4263 flags=0x2(adhoc) hashes=127+2 location=embedded
Hash type=sha256 size=32
CandidateCDHash sha256={cdhash}
CDHash={cdhash}
Signature=adhoc
TeamIdentifier=not set
"""

_ZSH = shutil.which("zsh")
_needs_zsh = pytest.mark.skipif(_ZSH is None, reason="zsh not installed")


def _wrapper_text() -> str:
    return _WRAPPER.read_text()


def _extract(pattern: re.Pattern[str], what: str) -> str:
    matches = pattern.findall(_wrapper_text())
    assert len(matches) == 1, f"expected exactly one {what} in {_WRAPPER}, found {len(matches)}"
    return matches[0]


def _shell_const(name: str) -> str:
    """The value of a top-level `NAME=value` assignment in the wrapper."""
    return _extract(re.compile(rf"^{name}=\"?([^\"\n]+)\"?$", re.M), f"{name} assignment")


def _count_sed_program() -> str:
    """The sed program the wrapper uses to read the transferred-file count."""
    return _extract(re.compile(r"sed -n -E '([^']+)'"), "count-parse sed program")


def _error_grep_program() -> str:
    """The grep pattern the wrapper uses to pick the diagnostic lines out of rsync."""
    return _extract(re.compile(r"grep -iE '([^']+)'"), "error-filter grep pattern")


def _parse_count(stats: str) -> str:
    proc = subprocess.run(
        ["sed", "-n", "-E", _count_sed_program()],
        input=stats,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.stdout.splitlines()[0].strip() if proc.stdout.strip() else ""


def _filter_errors(output: str) -> str:
    """Reproduce the wrapper's pipeline: grep -iE ... | head -5 | tr '\\n' ' '."""
    proc = subprocess.run(
        ["grep", "-iE", _error_grep_program()],
        input=output,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return " ".join(proc.stdout.splitlines()[:5])


class TestCountParse:
    """Every run logged "(? files transferred)"; a number must come out now."""

    def test_rsync_3_wording_yields_a_number(self):
        assert _parse_count(_RSYNC3_STATS) == "2"

    def test_openrsync_wording_yields_a_number(self):
        assert _parse_count(_OPENRSYNC_STATS) == "2"

    def test_a_failed_run_still_reports_what_did_get_through(self):
        assert _parse_count(_RSYNC3_FAILURE) == "2"
        assert _parse_count(_OPENRSYNC_FAILURE) == "1"


class TestErrorFilter:
    """`tail -3` caught the stats footer; the filter must catch the file name."""

    def test_rsync_3_error_naming_the_file_is_kept(self):
        kept = _filter_errors(_RSYNC3_FAILURE)
        assert _DENIED_FILE in kept
        assert "Permission denied" in kept

    def test_openrsync_error_naming_the_file_is_kept(self):
        kept = _filter_errors(_OPENRSYNC_FAILURE)
        assert _DENIED_FILE in kept
        assert "Permission denied" in kept

    @pytest.mark.parametrize(
        "output", [_RSYNC3_FAILURE, _OPENRSYNC_FAILURE], ids=["rsync3", "openrsync"]
    )
    def test_the_stats_footer_is_dropped(self, output):
        kept = _filter_errors(output)
        assert "speedup is" not in kept
        assert "Total file size" not in kept
        assert "File list generation time" not in kept


def _stub_bin(tmp_path: Path, name: str, body: str) -> Path:
    """A stub executable, first on PATH, standing in for a real tool."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/zsh\n" + body)
    p.chmod(0o755)
    return d


def _run_zsh(
    tmp_path: Path, body: str, stub_dir: Path, extra_env: dict
) -> subprocess.CompletedProcess:
    harness = tmp_path / "harness.zsh"
    harness.write_text(body)
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:/usr/bin:/bin:/usr/sbin:/sbin"
    env.update(extra_env)
    return subprocess.run(
        [_ZSH or "zsh", str(harness)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=tmp_path,
    )


@_needs_zsh
class TestRetry:
    """One retry, then give up: hydration is flaky, a second failure is real."""

    _LOOP_RE = re.compile(r"^  attempt=1$.*?^  done$", re.M | re.S)

    def _run_loop(self, tmp_path: Path, rsync_output: str, rsync_rc: int):
        calls = tmp_path / "calls.txt"
        captured = tmp_path / "log.txt"
        out_file = tmp_path / "rsync-output.txt"
        out_file.write_text(rsync_output)
        stub_dir = _stub_bin(
            tmp_path,
            "rsync",
            f'print -r -- call >> "{calls}"\ncat "{out_file}"\nexit {rsync_rc}\n',
        )

        loop = _extract(self._LOOP_RE, "transfer retry loop")
        body = "\n".join(
            [
                "#!/bin/zsh",
                "set -uo pipefail",
                f'log() {{ print -r -- "$*" >> "{captured}" }}',
                f"RSYNC_MAX_ATTEMPTS={_shell_const('RSYNC_MAX_ATTEMPTS')}",
                "RSYNC_RETRY_SLEEP=0",  # the wrapper's real value is asserted separately
                'src="/tmp/src"',
                'VPS="vps"',
                'tree="National"',
                loop,
                'print -r -- "rc=$rsync_rc"',
            ]
        )
        proc = _run_zsh(tmp_path, body, stub_dir, {})
        assert proc.returncode == 0, f"harness failed: {proc.stdout}\n{proc.stderr}"
        n = len(calls.read_text().splitlines()) if calls.exists() else 0
        return n, (captured.read_text() if captured.exists() else ""), proc.stdout

    def test_a_persistent_failure_is_attempted_exactly_twice(self, tmp_path):
        n, logged, stdout = self._run_loop(tmp_path, _RSYNC3_FAILURE, 23)
        assert n == 2, f"expected exactly one retry, rsync ran {n} times"
        assert "rc=23" in stdout
        assert "attempt 1" in logged and "attempt 2" in logged
        assert "attempt 3" not in logged

    def test_a_successful_transfer_is_not_retried(self, tmp_path):
        n, logged, stdout = self._run_loop(tmp_path, _RSYNC3_STATS, 0)
        assert n == 1, f"a successful transfer must not retry, rsync ran {n} times"
        assert "rc=0" in stdout
        assert "FAILED" not in logged

    def test_the_failure_log_names_the_file_not_the_footer(self, tmp_path):
        """The filter has to be wired into the loop, not merely present in the file."""
        _, logged, _ = self._run_loop(tmp_path, _RSYNC3_FAILURE, 23)
        assert _DENIED_FILE in logged
        assert "speedup is" not in logged

    def test_the_retry_sleep_covers_a_hydration_round_trip(self):
        """Long enough for OneDrive to finish materialising, short enough to stay in-run."""
        assert 30 <= int(_shell_const("RSYNC_RETRY_SLEEP")) <= 60


@_needs_zsh
class TestCdhashTripwire:
    """A tripwire that bricks the job is worse than the bomb, so it must fail OPEN."""

    _FN_RE = re.compile(r"^rsync_cdhash_mismatch\(\) \{$.*?^\}$", re.M | re.S)

    def _run_tripwire(self, tmp_path: Path, codesign_body: str):
        captured = tmp_path / "log.txt"
        stub_dir = _stub_bin(tmp_path, "rsync", "exit 0\n")
        _stub_bin(tmp_path, "codesign", codesign_body)

        body = "\n".join(
            [
                "#!/bin/zsh",
                "set -uo pipefail",
                f'log() {{ print -r -- "$*" >> "{captured}" }}',
                f'EXPECTED_RSYNC_CDHASH="{_shell_const("EXPECTED_RSYNC_CDHASH")}"',
                _extract(self._FN_RE, "cdhash tripwire function"),
                # Called exactly the way the wrapper calls it, so the harness
                # cannot pass under a calling convention production never uses.
                "cdhash_problem=$(rsync_cdhash_mismatch)",
                'print -r -- "exit=$?"',
                'print -r -- "reason=$cdhash_problem"',
            ]
        )
        proc = _run_zsh(tmp_path, body, stub_dir, {})
        lines = proc.stdout.splitlines()
        status = [ln for ln in lines if ln.startswith("exit=")]
        reasons = [ln for ln in lines if ln.startswith("reason=")]
        assert status and reasons, f"harness produced no result: {proc.stdout}\n{proc.stderr}"
        reason = reasons[-1][len("reason=") :]
        return reason, status[-1], (captured.read_text() if captured.exists() else "")

    def test_no_signature_at_all_does_not_abort(self, tmp_path):
        """codesign missing, or the binary unsigned: unknown is not a mismatch."""
        reason, status, logged = self._run_tripwire(tmp_path, "exit 1\n")
        assert reason == "", f"tripwire fired on an unreadable signature: {reason}"
        assert status == "exit=0"
        assert "tripwire" in logged.lower(), "a skipped tripwire must say so in the log"

    def test_a_platform_binary_does_not_abort(self, tmp_path):
        reason, status, _ = self._run_tripwire(tmp_path, f"cat <<'EOF'\n{_CODESIGN_PLATFORM}EOF\n")
        assert reason == "", f"tripwire fired on a platform binary: {reason}"
        assert status == "exit=0"

    def test_the_pinned_cdhash_passes_silently(self, tmp_path):
        pinned = _shell_const("EXPECTED_RSYNC_CDHASH")
        reason, status, _ = self._run_tripwire(
            tmp_path, f"cat <<'EOF'\n{_CODESIGN_ADHOC.format(cdhash=pinned)}EOF\n"
        )
        assert reason == ""
        assert status == "exit=0"

    def test_a_rebuilt_binary_names_what_a_human_must_do(self, tmp_path):
        """The whole point: a brew upgrade must alarm, not stall silently."""
        reason, status, _ = self._run_tripwire(
            tmp_path, f"cat <<'EOF'\n{_CODESIGN_ADHOC.format(cdhash='0' * 40)}EOF\n"
        )
        assert status == "exit=1"
        assert "System Settings" in reason
        assert "Files and Folders" in reason
        # The reason is interpolated into a single-quoted remote ssh command by
        # the failure-marker block, so an apostrophe in it would break the write.
        assert "'" not in reason
