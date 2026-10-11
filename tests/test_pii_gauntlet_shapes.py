"""pii-gauntlet generic shapes: phone numbers, IBANs, card numbers, public IPv4
addresses, tailnet hosts and the employer's name in its other spellings.

On 2026-10-11 an audit planted 24 synthetic leak kinds in a scratch repository and
the gauntlet caught 6 (code-tests-ci-02). A name list catches only the names
someone remembered to add; a shape catches the next value without being told.
These checks run in every mode, and in CI mode a hit prints where it is, never
what it is, because the Actions log of a public repository is public.

Every value is built at run time (tests/payment_data.py and the helpers below),
so this file carries no literal the checks would fire on. The words of the
employer's name are listed apart and only joined in the planted file.
"""

import os
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

from tests.payment_data import card, digits, grouped, iban, luhn_ok, not_luhn, printed

SCRIPT = Path(__file__).parent.parent / "scripts" / "pii-gauntlet.sh"


def _ip(*octets: int) -> str:
    return ".".join(str(o) for o in octets)


def _capitals(text: str) -> str:
    """Greek in capitals drops the accents, which str.upper() keeps."""
    decomposed = unicodedata.normalize("NFD", text.upper())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


MOBILE = "69" + digits(8)
LANDLINE = "21" + digits(8, 5)
GR_IBAN = iban("GR", digits(23, 1))
VISA = card("4", 16)
# One block past a documentation range, so the range's edge is tested too.
PUBLIC_IP = _ip(203, 0, 114, 7)
TAILNET_IP = _ip(100, 101, 7, 9)
HOST = f"box.tail{digits(4)}.ts.net"
FIRST = ("Εθνική", "Εθνικής", "Εθνικη")
SECOND = ("Τράπεζα", "Τράπεζας", "Τραπεζα")

PHONE = "Greek phone number"
IBAN = "Greek IBAN"
CARD = "Card number"
IP = "Public IPv4 address"
TAILNET = "Tailnet host"
EMPLOYER = "Employer name"

# file -> (the check that must fire, the planted text)
LEAKS = {
    "mobile_plus.txt": (PHONE, f"τηλ. +30 {MOBILE}"),
    "mobile_plus_compact.txt": (PHONE, f"+30{MOBILE}"),
    "mobile_0030.txt": (PHONE, f"call 0030{MOBILE} today"),
    "mobile_spaced.txt": (PHONE, f"κινητό {MOBILE[:3]} {MOBILE[3:6]} {MOBILE[6:]}"),
    "landline.txt": (PHONE, f"tel:{LANDLINE}"),
    "landline_spaced.txt": (PHONE, f"{LANDLINE[:3]} {LANDLINE[3:6]} {LANDLINE[6:]}"),
    "iban.txt": (IBAN, f"IBAN {GR_IBAN} για την πληρωμή"),
    "iban_printed.txt": (IBAN, f"ΙΒΑΝ: {printed(GR_IBAN)}."),
    "card.txt": (CARD, f"pan {VISA}"),
    "card_grouped.txt": (CARD, grouped(card("53", 16), sep="-")),
    "card_amex.txt": (CARD, grouped(card("37", 15), (4, 6, 5))),
    "card_after_a_row_number.txt": (CARD, f"07 {grouped(card('4', 16, 5))} 12/27"),
    "ip.txt": (IP, f"ssh root@{PUBLIC_IP}"),
    "ip_tailnet.txt": (IP, f"http://{TAILNET_IP}:8765/mcp"),
    "host.txt": (TAILNET, f"https://{HOST}/mcp"),
    "bank_genitive.txt": (EMPLOYER, f"της {FIRST[1]} {SECOND[1]}"),
    "bank_unaccented.txt": (EMPLOYER, f"{FIRST[2]} {SECOND[2]}"),
    "bank_capitals.txt": (EMPLOYER, _capitals(f"{FIRST[1]} {SECOND[1]}")),
}

# Shapes that must NOT fire: the repo's placeholders and the near misses.
CLEAN = {
    "placeholders.txt": "τηλ. 210 123 4567, κινητό 69 1234 5678",
    "not_luhn.txt": f"pan {not_luhn(VISA)}",
    "no_issuer.txt": f"{card('1', 16)} {card('9', 16)}",
    "iban_bad_check.txt": "GR" + str((int(GR_IBAN[2:4]) + 1) % 100).zfill(2) + GR_IBAN[4:],
    "private_ips.txt": " ".join(
        _ip(*o)
        for o in (
            (10, 1, 2, 3),
            (172, 16, 5, 4),
            (172, 31, 255, 254),
            (192, 168, 1, 1),
            (127, 0, 0, 1),
            (169, 254, 1, 1),
            (0, 0, 0, 0),
            (192, 0, 2, 10),
            (198, 51, 100, 7),
            (203, 0, 113, 9),
            (224, 0, 0, 1),
            (255, 255, 255, 255),
        )
    ),
    "versions.txt": "types-requests==2.33.0.20260906 and 1.12.3.4.5",
    "numbers.txt": "1791448325652 2026101109 202610110930 2147483647 0." + VISA,
    "inside_a_longer_run.txt": f"ref 4{MOBILE}7, id{VISA} and {VISA}ab",
    "hash.txt": f"sha256:a{VISA}b" + "0123456789abcdef" * 3,
    "ts_placeholder.txt": "https://example.ts.net, *.ts.net and docs.ts.network",
    "bank_other.txt": f"{FIRST[0]} Ασφαλιστική και {SECOND[0]} Πειραιώς",
}


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    done = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "pii-gauntlet.sh")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _plant(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (repo / name).write_text(f"first line\n{text}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "plant")
    return _git(repo, "rev-parse", "--short", "HEAD")


def _run(repo: Path, mode: str = "ci", denylist: Path | None = None, bin_dir: Path | None = None):
    env = {k: v for k, v in os.environ.items() if k not in ("CI", "GITHUB_ACTIONS")}
    env["PII_DENYLIST"] = str(denylist or repo.parent / "absent.conf")
    if bin_dir:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", "scripts/pii-gauntlet.sh", f"--mode={mode}"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def _failures(stdout: str) -> dict[str, list[str]]:
    """{label: the lines printed under its FAIL header}."""
    out: dict[str, list[str]] = {}
    label = None
    for line in stdout.splitlines():
        if line.startswith("FAIL ["):
            label = line[len("FAIL [") : line.index("]")]
            out[label] = []
        elif label and line.strip():
            if line.startswith(("OK ", "SKIP ", "INFO ", "===")):
                label = None
            else:
                out[label].append(line)
        else:
            label = None
    return out


def test_every_planted_shape_fails_its_check_and_is_printed_by_location_only(repo):
    _plant(repo, {name: text for name, (_, text) in LEAKS.items()})

    result = _run(repo)

    assert result.returncode == 1, result.stdout
    failures = _failures(result.stdout)
    for name, (label, _) in LEAKS.items():
        assert f"{name}:2" in failures.get(label, []), (name, label, result.stdout)
    for _, text in LEAKS.values():
        assert text not in result.stdout


def test_placeholders_and_near_misses_pass(repo):
    _plant(repo, CLEAN)

    result = _run(repo)

    assert result.returncode == 0, result.stdout


def test_a_card_filler_with_two_distinct_digits_passes(repo):
    filler = next(f"4{b}" * 8 for b in "98765" if luhn_ok(f"4{b}" * 8))
    _plant(repo, {"filler.txt": filler})

    assert _run(repo).returncode == 0


@pytest.mark.parametrize("mode", ["doctor", "history"])
def test_the_shapes_run_in_every_mode(repo, tmp_path, mode):
    denylist = tmp_path / "empty.conf"
    denylist.write_text("# nothing\n")
    planted = _plant(repo, {"leak.txt": f"pan {VISA}"})
    if mode == "history":
        (repo / "leak.txt").unlink()
        _git(repo, "commit", "-q", "-am", "remove")

    result = _run(repo, mode, denylist)

    assert result.returncode == 1, result.stdout
    assert f"FAIL [{CARD}]" in result.stdout
    if mode == "history":
        assert planted in result.stdout


def test_a_filter_that_cannot_run_reports_every_candidate_instead_of_none(repo, tmp_path):
    """An awk that rejects the program prints nothing and exits non-zero, which
    would clear every candidate line: the check would print OK having decided
    nothing. When the filter fails, every candidate counts as a hit."""
    fake = tmp_path / "bin" / "awk"
    fake.parent.mkdir()
    real = shutil.which("awk")
    fake.write_text(f'#!/bin/sh\ncase "$1" in *scan_runs*) exit 2 ;; esac\nexec {real} "$@"\n')
    fake.chmod(0o755)
    _plant(repo, {"card.txt": f"pan {not_luhn(VISA)}"})

    result = _run(repo, bin_dir=fake.parent)

    assert result.returncode == 1, result.stdout
    assert "card.txt:2" in _failures(result.stdout).get(CARD, [])
