#!/usr/bin/env bash
# pii-gauntlet.sh — verify no PII leaked into plessas-marketplace.
#
# TWO MODES:
#   --mode=ci      Scan only git-tracked files. Used by GitHub Actions to gate
#                  pushes. Any hit = FAIL = exit 1.
#   --mode=doctor  (default) Scan the entire working tree. Distinguishes tracked
#                  hits (FAIL — these would ship publicly) from gitignored hits
#                  (INFO — local-only, never pushed). Exit 1 only on tracked hits.
#
# Why two modes:
#   The CI mode is the actual safety gate.
#   The doctor mode helps the maintainer notice PII drift in their LOCAL files
#   before they accidentally `git add` something. It must NOT scare a teammate
#   running the script casually — "FAIL" on a gitignored file would teach them
#   to ignore the script entirely, defeating the point.
#
# Self-exclusion: this script contains the very patterns it searches for, so
# `--exclude=pii-gauntlet.sh` is essential to avoid self-match false positives.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Default to CI mode on a runner. Three repositories invoke this script with no
# --mode flag at all, so they got doctor mode, which fails hard when the private
# denylist is absent -- and on a runner it is always absent. Keying off the
# environment rather than the caller means a workflow cannot get this wrong by
# omission. GitHub, GitLab and most others set CI=true.
if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
  MODE="ci"
else
  MODE="doctor"
fi
_MODE_FROM_ENV="$MODE"
for arg in "$@"; do
  case "$arg" in
    --mode=ci)     MODE="ci" ;;
    --mode=doctor) MODE="doctor" ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

echo "=== PII Gauntlet (mode: $MODE) ==="
echo "Repo: $REPO_ROOT"
echo

FAIL=0
INFO=0

# `git ls-files` renders any non-ASCII path as octal escapes under the default
# core.quotePath=true, so a Greek filename becomes "\316\244\316\225...".
# That is not cosmetic: the escaped string is not a path that exists, so xargs
# hands grep a missing file, the error goes to /dev/null, and the CONTENTS of
# every Greek-named tracked file are silently never scanned. Verified by planting
# one, with an @nbg.gr address inside it that the gate could not see. -c
# overrides the setting for this process without touching the user's config.
GIT_LS="git -c core.quotePath=false ls-files"

# This script's path relative to the repo root. Derived, not hardcoded: the
# same file lives in installers/ in some repos and scripts/ in others, and
# hand-maintained copies are what let them drift apart in the first place.
# Both modes need it now, so it is computed before either branch.
SELF_REL=$($GIT_LS --full-name -- "$0" 2>/dev/null | head -1)
[ -z "$SELF_REL" ] && SELF_REL="scripts/pii-gauntlet.sh"

# Case sensitivity for the scanners below. Every check is case-INSENSITIVE by
# default, which is right for almost all of them. One is not: an ALL-CAPS Greek
# personal name is a SHAPE, and folding case destroys the shape, so that check
# flips this to empty for its own run via check_cs.
CASE_FLAG=i

# Build the file list once. CI mode = tracked only. Doctor mode = working tree.
if [ "$MODE" = "ci" ]; then
  # Exclude self + auto-generated lockfiles at any depth (lockfiles contain SHAs / hashes that
  # collide with the 9-digit-ID regex but carry no PII risk).
  TRACKED=$($GIT_LS \
    | grep -v "^$SELF_REL$" \
    | grep -vE '(^|/)LICENSE(\.md|\.txt)?$' \
    | grep -vE '(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock)$' \
    || true)
  TRACKED_TMP=$(mktemp)
  printf '%s\n' "$TRACKED" > "$TRACKED_TMP"
fi

# Helper: get the tracked-vs-untracked status of a file.
file_is_tracked() {
  git -c core.quotePath=false ls-files --error-unmatch "$1" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# The tracked TREE, as distinct from file CONTENTS
# ---------------------------------------------------------------------------
#
# Neither mode could see a FILENAME. Both grep file CONTENTS, and both skip
# binaries, so for a .png or an attachment fixture the name is the only
# readable surface there is. In the marketplace copy of this script that blind
# spot hid two tracked files disclosing an internal project name in their own
# filenames while every content check passed and the gate printed PASS.
#
# TREE_PATHS deliberately includes binaries, for exactly that reason. A
# filename is text no matter what the file contains.
TREE_PATHS=$($GIT_LS | grep -v "^$SELF_REL$" || true)
UNTRACKED_PATHS=$($GIT_LS --others --exclude-standard 2>/dev/null || true)

# Pair every path with its separator-normalised form as "orig<TAB>normalised",
# so a hit already carries its own path. Matching runs against the whole pasted
# line, so a hit on EITHER form counts, which is the union wanted.
#
# The normalisation matters more than case does. Filenames join words with _ .
# or -, so a pattern written "two words" matches prose and misses Two_words.png.
# Without it this check finds nothing at all, which would make it a check that
# exists only to look reassuring.
#
# One consequence: a `$`-anchored pattern would anchor to the normalised half.
# No pattern in use is `$`-anchored.
#
# Hits are emitted as "path:(filename)" so they share the "path:" shape the
# doctor-mode tracked/untracked splitter already parses. They are short by
# construction, so the cut -c1-200 further down never truncates one.
scan_paths() {
  local pattern="$1"
  local list="$2"
  [ -n "$list" ] || return 0
  paste <(printf '%s\n' "$list") <(printf '%s\n' "$list" | tr '_.-' '   ') \
    | grep -${CASE_FLAG}E "$pattern" 2>/dev/null \
    | cut -f1 \
    | sed 's/$/:(filename)/' \
    || true
}

scan_doctor() {
  local pattern="$1"
  grep -r${CASE_FLAG}E \
    --exclude-dir=.git \
    --exclude-dir=node_modules \
    --exclude-dir=dist \
    --exclude-dir=__pycache__ \
    --exclude-dir=.venv \
    --exclude-dir=venv \
    --exclude-dir=.remember \
    --exclude-dir=data \
    --exclude-dir=attachments \
    --exclude-dir=backups \
    --exclude-dir=archive \
    --exclude-dir=.next \
    --exclude-dir=dist \
    --exclude-dir=coverage \
    --exclude-dir=installers/deps \
    `# Tool caches. They hold no authored text, so a hit in one is noise by` \
    `# construction, and .mypy_cache alone is 16 MB / 423 files of minified` \
    `# JSON. Scanning them made the pre-commit run 3 minutes instead of 19s` \
    `# and dumped a single 243 KB line into the report. A slow, noisy gate is` \
    `# a gate that gets bypassed.` \
    --exclude-dir=.mypy_cache \
    --exclude-dir=.ruff_cache \
    --exclude-dir=.pytest_cache \
    --exclude-dir=htmlcov \
    --exclude=pii-gauntlet.sh \
    --exclude=LICENSE \
    --exclude=LICENSE.md \
    --exclude=LICENSE.txt \
    --exclude=PII-GAUNTLET.md \
    --exclude=package-lock.json \
    --binary-files=without-match \
    "$pattern" . 2>/dev/null \
    || true
}

scan_ci() {
  local pattern="$1"
  # Search only git-tracked files. NUL-delimit the list and use `xargs -0`
  # (portable on BSD/macOS and GNU). The old `xargs -a FILE -d '\n'` form is
  # GNU-only: on macOS it errors "invalid option -- a", gets swallowed by
  # 2>/dev/null, and the gate silently PASSES while scanning nothing.
  # -i, matching scan_doctor above. Without it the GATE was case-sensitive
  # while the local doctor was not, so the check that blocks a push was the
  # weaker of the two, which is backwards.
  if [ -s "$TRACKED_TMP" ]; then
    tr '\n' '\0' < "$TRACKED_TMP" | xargs -0 grep -${CASE_FLAG}nE --binary-files=without-match "$pattern" 2>/dev/null || true
  fi
}

# Drop hits that are documentation rather than live configuration.
#
# Two kinds of exclusion, and they are NOT interchangeable:
#   $2 content: matched anywhere on the grep line. Keep it narrow, because a
#      broad content exclusion is indistinguishable from switching the check off.
#   $3 path: matched against the `path:` prefix only. A path-shaped pattern let
#      loose on the whole line also matches CONTENT, which is how `<[^>]+>`
#      came to drop any hit on a line containing an HTML tag
#      (`<td>firstname.surname@<employer-domain></td>` excluded itself).
#
# The two used to be one argument, anchored to the path, with the content list
# hardcoded inside this function as PLACEHOLDER_CONTENT where no caller could
# see or override it. Splitting them is what lets the path blob go away.
apply_exclusion() {
  local hits="$1"
  local exclude="$2"
  local exclude_path="${3:-}"
  # No early return, and no filter that only some callers get. The old version
  # short-circuited when $exclude was empty, so a denylist entry with a blank
  # third field skipped the placeholder filters entirely while a generic check
  # got them. Every caller now runs the same two filters, each a no-op when its
  # argument is empty.
  #
  # There used to be a third, unconditional filter here dropping any line that
  # contained "(c) YYYY" or the word "copyright", case-insensitively, in the
  # name of licence attribution. LICENSE, LICENSE.md and LICENSE.txt are already
  # removed from the file list in both modes, so it protected nothing and
  # instead handed anyone a one-word suppression token: "Copyright 2026 - reach
  # me at <real address>" was dropped silently while the same address without
  # the word was caught. It is gone.
  if [ -n "$hits" ] && [ -n "$exclude_path" ]; then
    hits=$(printf '%s\n' "$hits" | grep -vE "^[^:]*($exclude_path)" || true)
  fi
  if [ -n "$hits" ] && [ -n "$exclude" ]; then
    hits=$(printf '%s\n' "$hits" | grep -vE "$exclude" || true)
  fi
  printf '%s' "$hits"
}

# Run one check case-SENSITIVELY. Restores the flag afterwards so nothing else
# is affected, and takes the same arguments as check.
check_cs() {
  local prev="$CASE_FLAG"
  CASE_FLAG=""
  check "$@"
  CASE_FLAG="$prev"
}

check() {
  local label="$1"
  local pattern="$2"
  local exclude="${3:-}"
  local exclude_path="${4:-}"
  local hits

  if [ "$MODE" = "ci" ]; then
    # Contents AND filenames, in one verdict per check. Folding them together
    # rather than bolting on a separate section is deliberate: it means a check
    # added later cannot silently skip the tree, which is the shape of every
    # hole found in this file.
    hits=$(printf '%s\n%s' "$(scan_ci "$pattern")" "$(scan_paths "$pattern" "$TREE_PATHS")" | grep -v '^$' || true)
    hits=$(apply_exclusion "$hits" "$exclude" "$exclude_path")
    if [ -n "$hits" ]; then
      echo "FAIL [$label]:"
      echo "$hits" | head -20
      echo
      FAIL=1
    else
      echo "OK   [$label]"
    fi
    return
  fi

  # Doctor mode — separate tracked from gitignored.
  #
  # Untracked FILENAMES are included here and not in CI, matching how doctor
  # already treats untracked CONTENT: an untracked path never ships, so it is
  # INFO rather than FAIL, but doctor exists to surface local drift before
  # someone stages it.
  hits=$(printf '%s\n%s\n%s' \
    "$(scan_doctor "$pattern")" \
    "$(scan_paths "$pattern" "$TREE_PATHS")" \
    "$(scan_paths "$pattern" "$UNTRACKED_PATHS")" | grep -v '^$' || true)
  hits=$(apply_exclusion "$hits" "$exclude" "$exclude_path")
  if [ -z "$hits" ]; then
    echo "OK   [$label]"
    return
  fi

  local tracked_hits=""
  local untracked_hits=""
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # line format: ./path/to/file:matched-text
    local path="${line%%:*}"
    path="${path#./}"
    if file_is_tracked "$path"; then
      tracked_hits+="$line"$'\n'
    else
      untracked_hits+="$line"$'\n'
    fi
  done <<< "$hits"

  # `cut -c1-200` before `head`: a hit inside a minified file is ONE line that
  # can be hundreds of KB, so a line cap alone does not bound the report. The
  # point of a hit is the path, which is at the front of the line.
  if [ -n "$tracked_hits" ]; then
    echo "FAIL [$label]:                 (tracked — would ship publicly)"
    printf '%s' "$tracked_hits" | cut -c1-200 | head -20
    echo
    FAIL=1
  fi
  if [ -n "$untracked_hits" ]; then
    echo "INFO [$label]:                 (gitignored / untracked — local-only)"
    printf '%s' "$untracked_hits" | cut -c1-200 | head -10
    echo
    INFO=1
  fi
  if [ -z "$tracked_hits" ] && [ -z "$untracked_hits" ]; then
    echo "OK   [$label]"
  fi
}

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Generic organisation tells (safe to keep in a public repo)
# ---------------------------------------------------------------------------
#
# These are patterns, not names, so publishing them discloses nothing. They are
# also the checks that were missing entirely: the four exposures found in the
# 2026-08-24 audit all passed the gauntlet green, because nothing here looked
# for the employer's name, its mail domain, or a tenant hostname.

# Placeholders are the whole point of a good example, so they must not trip the
# check that exists to catch the real thing. Without these excludes the gauntlet
# flags `contoso.sharepoint.com` (the correct placeholder) and its own CHANGELOG
# entries describing the removal of the real host. A check that fires on
# deliberate, already-disclosed content teaches you to ignore it, which is how
# the four real exposures sat unnoticed next to a green gauntlet.
# Doc placeholders. Extend this when a new invented example host trips the check:
# being asked once "is this a real tenant?" is the check doing its job, and is a
# far better failure mode than the silence it replaces.
#
# Every exclusion below names a placeholder VALUE. None of them exempts a FILE.
# The variable that used to sit here did three other jobs, and all three were
# holes:
#
#   `example|sample|template` was matched against the path of every hit, so any
#   path merely CONTAINING one of those words was exempt from the employer-name,
#   mail-domain and tenant checks. Three tracked files were invisible outright:
#   .env.example, examples/example_exporter.py and examples/sample-batch.json.
#   An .env example is exactly the file a real host or token gets pasted into.
#   Grepped against every check, the only hit inside the three is
#   `.env.example:33 SHAREPOINT_HOST=contoso.sharepoint.com`, and contoso is
#   already excluded by value below, so removing the token changes no verdict
#   and the three files are now scanned for the first time.
#
#   `<[^>]+>` matched any HTML or XML tag; the moment it was tested against a
#   whole line it cancelled every hit on a line containing a tag.
#
#   `user@|name@` were unanchored substrings, exempting `realuser@...` and
#   `firstname@...` as readily as the placeholders they named. Every occurrence
#   in this repo is `user@1000.service` in src/llm_deadline.py and its test, a
#   systemd unit rather than an address, and none is an @nbg.gr address, so they
#   excluded nothing the mail-domain check could have fired on. Dropped.
PLACEHOLDER='contoso|firstname\.lastname|your\.email|recipient\.name|your[-_.]?tenant'

# Fixture hostnames, for the tenant check only. Two rules, both of which the
# previous list broke.
#
# 1. Anchored on the left by a character that cannot appear inside a host label,
#    so a fixture name has to be the WHOLE label. The list was UNANCHORED, which
#    is the same defect as the bare `[a-z]` it was written to replace: `x`
#    matched the last character of onyx.sharepoint.com, and `y` matched the `y`
#    of every `<tenant>-my.sharepoint.com`, which is the OneDrive-for-Business
#    half of the very thing this check looks for. Anchoring is what makes the
#    remaining single letter safe.
# 2. Only fixture names this repo actually uses, each grepped over the tracked
#    tree: test (2), dummy (1), partner (3), mastercard (2) and x (8). The
#    dropped names, overridden, envvar, placeholder, foo, bar, a, b, y and z,
#    appear nowhere; each was a live exemption for a tenant label nobody had
#    written. partner and mastercard are the two "some other tenant" fixtures in
#    tests/export/test_sharepoint_fetcher.py.
PLACEHOLDER_TENANT="$PLACEHOLDER"'|(^|[^a-z0-9.-])(test|dummy|partner|mastercard|x)(-my)?\.sharepoint'

# Synthetic id runs the fixtures use in place of a real one. These are literal
# VALUES that cannot occur by accident, which is why they are safe to apply to
# the denylist checks below, where the private denylist ships one exclude column
# per entry and cannot know about a placeholder specific to this repo. Only the
# four that actually appear in the tracked tree are kept; 012345678 was listed
# and is used nowhere.
PLACEHOLDER_VALUE='123456789|987654321|111111111|000000000'

# Some repos name the employer on purpose: a marketplace written for colleagues
# says so in its README by design. Those opt out with a repo-root marker rather
# than carrying a permanent red light.
if [ ! -f ".pii-gauntlet-allow-employer-name" ]; then
  check "Employer name" '(^|[^A-Za-z0-9])(NBG|ΕΤΕ)([^A-Za-z0-9]|$)|Εθνική Τράπεζα|National Bank of Greece' "$PLACEHOLDER"
else
  echo "OK   [Employer name] (opted out via .pii-gauntlet-allow-employer-name)"
fi

check "Employer mail domain" '[A-Za-z0-9._%+-]+@nbg\.gr' "$PLACEHOLDER"
check "SharePoint tenant"    '[a-z0-9-]+\.sharepoint\.com' "$PLACEHOLDER_TENANT"
# A bare UUID is not a finding: fixtures, generated filenames and message ids
# are full of them, and flagging all of them is how a check earns the right to
# be ignored. What matters is a GUID sitting where a TENANT id sits, so key on
# the surrounding context rather than on the shape alone.
check "Azure AD tenant id" \
  '(tenant[_-]?id|tenantId|\"tid\"|authority|login\.microsoftonline\.com/|realm)[^0-9a-f]{0,24}[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  '00000000-0000-0000-0000-000000000000|11111111-2222-3333-4444-555555555555|00000000-|11111111-|22222222-|33333333-|44444444-|55555555-|66666666-|77777777-|88888888-|99999999-|aaaaaaaa-|bbbbbbbb-|cccccccc-|dddddddd-|eeeeeeee-|ffffffff-|/common|/organizations|/consumers'

# Two or more consecutive ALL-CAPS Greek words is how Greek corporate systems
# write a person: SURNAME FORENAME. In source it is almost never anything else.
#
# This is a SHAPE check, and it is case-SENSITIVE for that reason: both scanners
# are -i now, and folding case turns every Greek word into a match. It exists
# because the denylist can only ever hold names somebody remembered to add, and
# a real direct report's full name sat in a tracked test file of this PUBLIC repo
# from the initial release until 2026-09-09 with the denylist loaded and the
# gauntlet printing PASS. A shape catches the next one without being told.
#
# Greek capitals carry no accent, so the class is the plain 24 uppercase letters.
# Measured over the whole tracked tree: this fires on nothing except the
# synthetic placeholder excluded below.
# A bracket expression over multibyte letters is LOCALE-DEPENDENT. Under C or
# POSIX it decays into individual BYTES, so {n,} counts bytes rather than
# characters. Measured with BSD grep, which is what runs this: the same pattern
# found 3 matches under en_US.UTF-8 and 4 under C, the extra being a two-letter
# article that is four bytes long and so cleared a three-CHARACTER minimum. The
# gate would be stricter on a runner than in local testing, in the direction that
# invents findings. An alternation of literal characters has no collation to
# resolve and measured identically under en_US.UTF-8, C.UTF-8, C and POSIX.
#
# Dialytika is in the set because Greek all-caps drops the tonos but KEEPS the
# dialytika, so a name containing it would otherwise break mid-word.
GRK_CAP='(Α|Β|Γ|Δ|Ε|Ζ|Η|Θ|Ι|Κ|Λ|Μ|Ν|Ξ|Ο|Π|Ρ|Σ|Τ|Υ|Φ|Χ|Ψ|Ω|Ϊ|Ϋ)'
# Four letters, not three. At three the rule fires on the whole Greek acronym
# class, since every Greek acronym in these repos is two or three letters, and on
# articles and conjunctions: a bare acronym-plus-conjunction pair matched, as did
# a logo caption in a sibling repo. Four kills both classes. Five would start
# missing genuine four-letter forenames.
check_cs "All-caps Greek personal name" \
  "${GRK_CAP}{4,}[[:space:]]+${GRK_CAP}{4,}" \
  'ΠΑΠΑΔΟΠΟΥΛΟΥ ΜΑΡΙΝΑ'


# ---------------------------------------------------------------------------
# Name-based checks, loaded from a private denylist
# ---------------------------------------------------------------------------
#
# The literal terms used to live in this file. That made the guard the
# disclosure: this script is anonymously readable, and it enumerated colleague
# names, a family name, a personal mobile and address, personal emails,
# unreleased internal project names, the partner list and every private repo
# path, complete with comment headers explaining which was which.
#
# They now live outside every public repo. Nothing here reveals what is checked.

PII_DENYLIST="${PII_DENYLIST:-$HOME/.claude/private/pii-denylist.conf}"

if [ -r "$PII_DENYLIST" ]; then
  # Tab-delimited: the patterns are full of regex alternation pipes, so "|"
  # cannot be the field separator.
  loaded=0
  # The repo-local placeholder VALUES are ORed onto each entry's own exclude
  # column. The denylist is shared by several repos and cannot know about the
  # synthetic id runs this repo's fixtures use, and editing it from here would
  # change every repo's gate at once. These used to reach the denylist checks
  # via a filter applied unconditionally inside apply_exclusion; passing them
  # explicitly means a reader can see which checks they affect.
  while IFS=$'\t' read -r label pattern exclude; do
    case "$label" in ''|\#*) continue ;; esac
    [ -z "$pattern" ] && continue
    if [ -n "$exclude" ]; then
      exclude="$exclude|$PLACEHOLDER_VALUE"
    else
      exclude="$PLACEHOLDER_VALUE"
    fi
    check "$label" "$pattern" "$exclude"
    loaded=$((loaded + 1))
  done < "$PII_DENYLIST"
  echo "     ($loaded name-based checks loaded from the private denylist)"
else
  # Absence is handled differently by mode, on purpose.
  #
  # In CI on a public repo the denylist legitimately does not exist and never
  # will, so failing here would just paint six repos red forever. Say plainly
  # that the name checks did not run, and let the generic ones stand.
  #
  # Locally the file should always be there. Its absence is a real
  # misconfiguration, and a guard that cannot evaluate its condition must
  # refuse rather than pass. That distinction is the whole point: the previous
  # version of this script reported OK on every check while, on macOS, scanning
  # exactly zero files.
  if [ "$MODE" = "ci" ]; then
    echo "SKIP [name-based checks]: no denylist at $PII_DENYLIST"
    echo "     Generic org-tell checks above still ran. Name, family, partner and"
    echo "     private-path checks did NOT. This is expected in public CI."
  else
    echo "FAIL [name-based checks]: no denylist at $PII_DENYLIST"
    echo "     Locally this file must exist. Without it the name, family, partner"
    echo "     and private-path checks are silently absent, which is exactly the"
    echo "     false green this rewrite removes."
    echo "     Fix: ensure ~/.claude/private -> claude-config/private is linked,"
    echo "     or set PII_DENYLIST to the file."
    FAIL=1
  fi
fi

if [ $FAIL -eq 0 ]; then
  if [ "$MODE" = "doctor" ] && [ $INFO -ne 0 ]; then
    echo "=== GAUNTLET PASS (with INFO on gitignored files — local-only, not in git) ==="
  else
    echo "=== GAUNTLET PASS ==="
  fi
  exit 0
else
  echo "=== GAUNTLET FAIL ==="
  if [ "$MODE" = "doctor" ]; then
    echo "Tracked PII detected. These files would ship publicly. Fix before committing."
  else
    echo "Fix the PII leaks above before any public push."
  fi
  exit 1
fi
