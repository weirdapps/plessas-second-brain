#!/usr/bin/env bash
# pii-gauntlet.sh — verify no PII leaked into plessas-marketplace.
#
# THREE MODES:
#   --mode=ci      Scan only git-tracked files. Used by GitHub Actions to gate
#                  pushes. Any hit = FAIL = exit 1. Prints path:line, never the
#                  matched text: an Actions log on a public repo is public.
#   --mode=doctor  (default) Scan the entire working tree. Distinguishes tracked
#                  hits (FAIL — these would ship publicly) from gitignored hits
#                  (INFO — local-only, never pushed). Exit 1 only on tracked hits.
#   --mode=history Scan every line and filename ever ADDED, on every ref. Local
#                  only: needs the private denylist. Prints the commit and the
#                  check, never the matched text. Exit 1 on any hit.
#
# Why these modes:
#   The CI mode is the actual safety gate.
#   The doctor mode helps the maintainer notice PII drift in their LOCAL files
#   before they accidentally `git add` something. It must NOT scare a teammate
#   running the script casually — "FAIL" on a gitignored file would teach them
#   to ignore the script entirely, defeating the point.
#   The history mode sees what the other two cannot: content a fix removed from
#   the tree but not from the published history.
#
# Self-exclusion: this script contains the very patterns it searches for, so
# `--exclude=pii-gauntlet.sh` is essential to avoid self-match false positives.
# History mode keeps the script in scope for the denylist checks only, because
# the denylist once lived inside it.

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
    --mode=ci)      MODE="ci" ;;
    --mode=doctor)  MODE="doctor" ;;
    --mode=history) MODE="history" ;;
    -h|--help)
      sed -n '2,25p' "$0"
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

# History mode reads added lines and added filenames from every ref, each tagged
# with the short hash of the commit that introduced it. Two content streams: the
# generic checks read history WITHOUT this script, whose own text is full of the
# patterns, and the denylist checks read it WITH the script, because an earlier
# version of this very file carried the denylist inline.
#
# --all covers local refs only. A pull request's head stays reachable on GitHub
# through refs/pull/N/head, merged or closed, and survives a history rewrite, so
# those heads are fetched into temporary refs for the run and deleted after it.
# --full-history on the pathspec'd stream: without it git simplifies history and
# skips the side of a merge that ends tree-identical, which is exactly where
# content added and removed inside a merged PR lives.
if [ "$MODE" = "history" ]; then
  HISTORY_ALL=$(mktemp)
  HISTORY_NOSELF=$(mktemp)
  HISTORY_NAMES=$(mktemp)
  cleanup_history() {
    rm -f "$HISTORY_ALL" "$HISTORY_NOSELF" "$HISTORY_NAMES"
    git for-each-ref --format='%(refname)' refs/gauntlet-pr/ | while read -r ref; do
      git update-ref -d "$ref"
    done
  }
  trap cleanup_history EXIT
  # Never prompt: ssh passphrase, host-key and credential prompts go to the
  # terminal, not stderr, and would stall an unattended run. A fetch that fails
  # fails the audit, because GitHub still serves what it could not see.
  PR_HEADS_UNSCANNED=0
  if git remote get-url origin >/dev/null 2>&1; then
    if ! GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=15' \
        git fetch -q --no-tags origin '+refs/pull/*/head:refs/gauntlet-pr/*' 2>/dev/null; then
      PR_HEADS_UNSCANNED=1
    fi
  fi
  # Count what the run covers, so a PASS over no pull-request heads cannot pass
  # for a PASS over all of them.
  if [ "$PR_HEADS_UNSCANNED" -eq 1 ]; then
    echo "Pull-request heads scanned: none (the fetch from origin failed)"
  elif git remote get-url origin >/dev/null 2>&1; then
    echo "Pull-request heads scanned: $(git for-each-ref --format='%(refname)' refs/gauntlet-pr/ | wc -l | tr -d ' ')"
  else
    echo "Pull-request heads scanned: 0 (no origin remote)"
  fi
  echo
  # A licence names its author by design; ci and doctor mode skip it too.
  NOT_LICENCE=(':(exclude,glob)**/LICENSE' ':(exclude,glob)**/LICENSE.md' ':(exclude,glob)**/LICENSE.txt')
  git -c core.quotePath=false log --all --full-history -p -U0 --no-color --no-ext-diff \
    --format='commit %h' -- . "${NOT_LICENCE[@]}" > "$HISTORY_ALL"
  git -c core.quotePath=false log --all --full-history -p -U0 --no-color --no-ext-diff \
    --format='commit %h' -- . ':(exclude,glob)**/pii-gauntlet.sh' "${NOT_LICENCE[@]}" > "$HISTORY_NOSELF"
  git -c core.quotePath=false log --all --diff-filter=AR --name-only \
    --format='commit %h' > "$HISTORY_NAMES"
  HISTORY_SRC="$HISTORY_NOSELF"
fi

# Emit "<commit>:<added line>" for every line the diffs ADD, then filter. State
# tracking rather than a "+++ " test, so an added line that happens to start
# with "++" is still content. Filenames go through the same separator
# normalisation scan_paths uses, as "<commit>:<path>:(filename)", so the
# placeholder exclusions still see the path; only the commit is ever printed.
# awk runs under LC_ALL=C: history holds bytes that are not valid UTF-8 (a small
# binary with no NUL byte is diffed as text), BSD awk aborts on them in a UTF-8
# locale, and every pattern awk matches here is ASCII. iconv -c then drops the
# invalid bytes rather than the line: a UTF-8 grep silently skips any line that
# carries one, which hid every line of a legacy cp1253 or Latin-1 file. grep
# keeps the caller's locale, which the Greek checks below were measured under.
#
# A line holding a regex escape is also emitted with the escapes removed, after a
# tab on the same line, so a published regex is checked as the value it encodes.
# The old inline denylist published its entries that way, dots escaped, and a
# pattern for the value never matched its escaped spelling: history mode printed
# OK for an address that was in the published text. One line either way, so the
# count stays one per added line.
scan_history() {
  local pattern="$1"
  LC_ALL=C awk '/^commit [0-9a-f]+$/ { c = $2; hunk = 0; next }
       /^diff --git / { hunk = 0; next }
       /^@@ / { hunk = 1; next }
       hunk && /^\+/ {
         l = substr($0, 2); u = l
         if (index(u, "\\")) { gsub(/\\[.]/, ".", u); gsub(/\\-/, "-", u); gsub(/\\[+]/, "+", u); gsub(/\\@/, "@", u) }
         if (u != l) l = l "\t" u
         print c ":" l
       }' "$HISTORY_SRC" \
    | iconv -f UTF-8 -t UTF-8 -c \
    | grep -${CASE_FLAG}E "$pattern" 2>/dev/null || true
  LC_ALL=C awk '/^commit [0-9a-f]+$/ { c = $2; next } NF { print c "\t" $0 }' "$HISTORY_NAMES" \
    | LC_ALL=C awk -F'\t' '{ n = $2; gsub(/[_.-]/, " ", n); print $1 "\t" $2 "\t" n }' \
    | iconv -f UTF-8 -t UTF-8 -c \
    | grep -${CASE_FLAG}E "$pattern" 2>/dev/null \
    | cut -f1,2 \
    | LC_ALL=C awk -F'\t' '{ print $1 ":" $2 ":(filename)" }' || true
}

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
  # -H: xargs can hand the last grep a single file, and grep then drops the
  # filename, so the hit would have no path to report.
  if [ -s "$TRACKED_TMP" ]; then
    tr '\n' '\0' < "$TRACKED_TMP" | xargs -0 grep -${CASE_FLAG}nHE --binary-files=without-match "$pattern" 2>/dev/null || true
  fi
}

# path:line for a CI-mode hit, never the text. The matched text is what a hit
# says must not be published, and printing it into a public Actions log is
# publishing it again. A filename hit is already its own location.
hit_locations() {
  LC_ALL=C awk '/:\(filename\)$/ { print; next }
    match($0, /:[0-9]+:/) { print substr($0, 1, RSTART + RLENGTH - 2); next }
    { print "(location withheld)" }'
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

# A pattern grep cannot compile makes it exit 2, the error goes to /dev/null
# with every other grep error here, and the check prints OK having searched for
# nothing. GNU grep on a runner and BSD grep on a Mac do not accept the same
# patterns, so the private denylist is exactly where that would first happen.
pattern_compiles() {
  printf '' | grep -E -e "$1" >/dev/null 2>&1
  [ $? -ne 2 ]
}

# The shape checks. grep finds the lines that LOOK like a phone number, an IBAN,
# a card number, an IPv4 address or a tailnet host; this keeps a line only if it
# holds a value of that kind that is real: it passes the kind's own test (Luhn,
# mod-97, a public address range) and is not one of the placeholders. A random
# 16-digit number passes Luhn one time in ten, so the issuer range, the length,
# more than two distinct digits and a number standing on its own carry the rest.
#
# Placeholders are compared as normalised VALUES (digits only, compact capitals,
# the tailnet label), so a line holding a placeholder and a real value still
# fails, which a line-level exclusion would let through.
#
# Portable awk on purpose (BSD awk, mawk, gawk): no interval expressions, no
# gensub, no backreferences. LC_ALL=C because every value is ASCII and a line of
# Greek prose is bytes to skip, not characters to understand.
SHAPE=""
SHAPE_PLACEHOLDERS=""
shape_filter() {
  SHAPE="$SHAPE" SHAPE_PLACEHOLDERS="$SHAPE_PLACEHOLDERS" LC_ALL=C awk '
    function isdig(c) { return c != "" && index("0123456789", c) > 0 }
    function isalnum(c) {
      return c != "" && index("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", c) > 0
    }
    function placeholder(v) { return index(" " PH " ", " " v " ") > 0 }
    function at(s, i) { return i >= 1 ? substr(s, i, 1) : "" }
    # A number standing on its own: not inside a word or an id, not the
    # fraction of a decimal, and for a card not the digits after a "+".
    function left_ok(s, i, kind,   c) {
      c = at(s, i - 1)
      if (isalnum(c)) return 0
      if (c == "." && isdig(at(s, i - 2))) return 0
      if (kind == "card" && c == "+") return 0
      return 1
    }
    function luhn(d,   i, n, sum, alt) {
      sum = 0; alt = 0
      for (i = length(d); i >= 1; i--) {
        n = substr(d, i, 1) + 0
        if (alt) { n = n * 2; if (n > 9) n = n - 9 }
        sum += n; alt = !alt
      }
      return sum % 10 == 0
    }
    function distinct(d,   i, c, seen, n) {
      split("", seen); n = 0
      for (i = 1; i <= length(d); i++) { c = substr(d, i, 1); if (!(c in seen)) { seen[c] = 1; n++ } }
      return n
    }
    # Groups a..b of the current run as a printed card: one run of 13-19 digits,
    # fours with a shorter last group, or the 4-6-5 and 4-6-4 issuer layouts.
    function card_layout(a, b,   k, total) {
      if (a == b) return length(G[a]) >= 13 && length(G[a]) <= 19
      total = 0
      for (k = a; k <= b; k++) total += length(G[k])
      if (total < 13 || total > 19) return 0
      if (b - a == 2 && length(G[a]) == 4 && length(G[a + 1]) == 6 && length(G[b]) >= 4 && length(G[b]) <= 5) return 1
      for (k = a; k < b; k++) if (length(G[k]) != 4) return 0
      return length(G[b]) <= 4
    }
    function card_ok(d) {
      return index("23456", substr(d, 1, 1)) > 0 && distinct(d) > 2 && luhn(d) && !placeholder(d)
    }
    # +30 or 0030, then a 69 mobile or a 21-28 landline: ten national digits.
    function phone_ok(d, before) {
      if (before == "+" && substr(d, 1, 2) == "30") d = substr(d, 3)
      else if (substr(d, 1, 4) == "0030") d = substr(d, 5)
      if (length(d) != 10) return 0
      if (substr(d, 1, 2) != "69" && !(substr(d, 1, 1) == "2" && index("12345678", substr(d, 2, 1)) > 0)) return 0
      return !placeholder(d)
    }
    # Runs of digit groups joined by one separator from seps; every run of
    # consecutive groups is a candidate, so a row number or a year printed in
    # front of a number does not hide it.
    function scan_runs(s, seps, kind,   n, i, j, first, ng, a, b, d, L, R) {
      n = length(s); i = 1
      while (i <= n) {
        if (!isdig(substr(s, i, 1))) { i++; continue }
        first = i; L = at(s, i - 1); ng = 0
        while (1) {
          j = i
          while (j <= n && isdig(substr(s, j, 1))) j++
          ng++; G[ng] = substr(s, i, j - i); S[ng] = substr(s, j, 1)
          if (S[ng] != "" && index(seps, S[ng]) > 0 && isdig(substr(s, j + 1, 1))) { i = j + 1; continue }
          break
        }
        R = substr(s, j, 1)
        for (a = 1; a <= ng; a++) {
          if (a == 1 && !left_ok(s, first, kind)) continue
          d = ""
          for (b = a; b <= ng && b <= a + 4; b++) {
            if (b > a && S[b - 1] != S[a]) break
            d = d G[b]
            if (b == ng && isalnum(R)) continue
            if (kind == "card" && card_layout(a, b) && card_ok(d)) return 1
            if (kind == "phone" && phone_ok(d, (a == 1) ? L : " ")) return 1
          }
        }
        i = j + 1
      }
      return 0
    }
    function iban_ok(v,   r, i, c, m) {
      r = substr(v, 5) substr(v, 1, 4); m = 0
      for (i = 1; i <= length(r); i++) {
        c = substr(r, i, 1)
        if (isdig(c)) m = (m * 10 + c) % 97
        else m = (m * 100 + index("ABCDEFGHIJKLMNOPQRSTUVWXYZ", c) + 9) % 97
      }
      return m == 1
    }
    # GR, two check digits and 23 more characters, compact or printed in groups.
    function scan_iban(s,   u, pos, start, i, c, v, got) {
      u = toupper(s); pos = 1
      while (pos <= length(u) && match(substr(u, pos), /GR[0-9][0-9]/)) {
        start = pos + RSTART - 1; pos = start + 1
        if (isalnum(at(u, start - 1))) continue
        v = substr(u, start, 4); got = 0; i = start + 4
        while (i <= length(u) && got < 23) {
          c = substr(u, i, 1)
          if (isalnum(c)) { v = v c; got++ }
          else if (!(c == " " && isalnum(substr(u, i + 1, 1)))) break
          i++
        }
        if (got == 23 && !isalnum(substr(u, i, 1)) && iban_ok(v) && !placeholder(v)) return 1
      }
      return 0
    }
    # Private (RFC 1918), loopback, link-local, this-network, documentation
    # (RFC 5737), multicast and reserved addresses are not findings. Everything
    # else is, including 100.64.0.0/10: shared address space, which is where a
    # tailnet numbers its machines.
    function ip_public(a, b, c) {
      if (a == 0 || a == 10 || a == 127 || a >= 224) return 0
      if (a == 169 && b == 254) return 0
      if (a == 172 && b >= 16 && b <= 31) return 0
      if (a == 192 && b == 168) return 0
      if (a == 192 && b == 0 && c == 2) return 0
      if (a == 198 && b == 51 && c == 100) return 0
      if (a == 203 && b == 0 && c == 113) return 0
      return 1
    }
    function scan_ipv4(s,   pos, start, m, q, i, ok) {
      pos = 1
      while (pos <= length(s) && match(substr(s, pos), /[0-9]+[.][0-9]+[.][0-9]+[.][0-9]+/)) {
        start = pos + RSTART - 1; pos = start + 1
        m = substr(s, start, RLENGTH)
        ok = !isalnum(at(s, start - 1)) && at(s, start - 1) != "."
        ok = ok && !isalnum(substr(s, start + RLENGTH, 1))
        ok = ok && !(substr(s, start + RLENGTH, 1) == "." && isdig(substr(s, start + RLENGTH + 1, 1)))
        if (!ok) continue
        split(m, q, ".")
        for (i = 1; i <= 4; i++) if (length(q[i]) > 3 || q[i] + 0 > 255) ok = 0
        if (ok && ip_public(q[1] + 0, q[2] + 0, q[3] + 0) && !placeholder(m)) return 1
      }
      return 0
    }
    # <machine>.<tailnet>.ts.net; the placeholder is the tailnet label.
    function scan_tailnet(s,   l, pos, k, j, c, host, label) {
      l = tolower(s); pos = 1
      while ((k = index(substr(l, pos), ".ts.net")) > 0) {
        k = pos + k - 1; pos = k + 1
        c = substr(l, k + 7, 1)
        if (isalnum(c) || c == "-") continue
        j = k - 1
        while (j >= 1) {
          c = substr(l, j, 1)
          if (!(isalnum(c) || c == "-" || c == ".")) break
          j--
        }
        host = substr(l, j + 1, k - j - 1)
        sub(/^[.]+/, "", host)
        if (host == "") continue
        label = host; sub(/.*[.]/, "", label)
        if (!placeholder(label)) return 1
      }
      return 0
    }
    BEGIN { KIND = ENVIRON["SHAPE"]; PH = ENVIRON["SHAPE_PLACEHOLDERS"] }
    KIND == "card" && scan_runs($0, " -", "card") { print; next }
    KIND == "phone" && scan_runs($0, " ", "phone") { print; next }
    KIND == "iban" && scan_iban($0) { print; next }
    KIND == "ipv4" && scan_ipv4($0) { print; next }
    KIND == "tailnet" && scan_tailnet($0) { print; next }'
}

# A shape check: check, then shape_filter over its hits.
check_shape() {
  SHAPE="$3"
  SHAPE_PLACEHOLDERS="${4:-}"
  check "$1" "$2"
  SHAPE=""
  SHAPE_PLACEHOLDERS=""
}

# A filter that cannot run prints nothing, which would clear every candidate and
# pass the check having decided nothing. Fail closed instead: report them all.
keep_shapes() {
  local hits="$1" kept
  if [ -z "$SHAPE" ] || [ -z "$hits" ]; then
    printf '%s' "$hits"
  elif kept=$(printf '%s\n' "$hits" | shape_filter); then
    printf '%s' "$kept"
  else
    echo "pii-gauntlet: the $SHAPE filter failed, so every candidate line is reported" >&2
    printf '%s' "$hits"
  fi
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

  if ! pattern_compiles "$pattern"; then
    echo "FAIL [$label]: the pattern does not compile on this grep, so nothing was checked"
    FAIL=1
    return
  fi

  if [ "$MODE" = "history" ]; then
    # The commit and the check, never the text: the point is to locate history
    # that needs rewriting, not to reprint what it discloses.
    hits=$(scan_history "$pattern")
    hits=$(apply_exclusion "$hits" "$exclude" "$exclude_path")
    hits=$(keep_shapes "$hits")
    if [ -n "$hits" ]; then
      local count commits
      count=$(printf '%s\n' "$hits" | wc -l | tr -d ' ')
      commits=$(printf '%s\n' "$hits" | cut -d: -f1 | sort -u | tr '\n' ' ')
      echo "FAIL [$label]: $count added line(s) or filename(s) in commit(s): $commits"
      FAIL=1
    else
      echo "OK   [$label]"
    fi
    return
  fi

  if [ "$MODE" = "ci" ]; then
    # Contents AND filenames, in one verdict per check. Folding them together
    # rather than bolting on a separate section is deliberate: it means a check
    # added later cannot silently skip the tree, which is the shape of every
    # hole found in this file.
    hits=$(printf '%s\n%s' "$(scan_ci "$pattern")" "$(scan_paths "$pattern" "$TREE_PATHS")" | grep -v '^$' || true)
    hits=$(apply_exclusion "$hits" "$exclude" "$exclude_path")
    hits=$(keep_shapes "$hits")
    if [ -n "$hits" ]; then
      echo "FAIL [$label]:"
      printf '%s\n' "$hits" | hit_locations | head -20
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
  hits=$(keep_shapes "$hits")
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
#
# The Greek name in every case and accent: the genitive, the archaic genitive,
# no accents, capitals. The nominative alone let the other forms through. Each
# letter is an alternation of literal characters rather than a bracket
# expression, for the locale reason given at GRK_CAP below, and both cases are
# spelled out because -i folds Greek only in a UTF-8 locale.
EMPLOYER_GR='(Ε|ε)(Θ|θ)(Ν|ν)(Ι|ι|Ί|ί)(Κ|κ)(Η|η|Ή|ή)(Σ|ς)?[[:space:]]+(Τ|τ)(Ρ|ρ)(Α|α|Ά|ά)(Π|π)(Ε|ε|Έ|έ)(Ζ|ζ)(Α|α|Ά|ά|Η|η|Ή|ή)(Σ|ς)?'
if [ ! -f ".pii-gauntlet-allow-employer-name" ]; then
  check "Employer name" "(^|[^A-Za-z0-9])(NBG|ΕΤΕ)([^A-Za-z0-9]|$)|$EMPLOYER_GR|National Bank of Greece" "$PLACEHOLDER"
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

# Shapes of personal and infrastructure data. A 2026-10-11 audit planted 24 leak
# kinds in a scratch repo and the checks above caught 6: phone numbers, IBANs,
# card numbers, IP addresses and tailnet hosts all passed, with the private
# denylist loaded or not. grep below only finds candidate lines; shape_filter
# decides, by checksum, range and placeholder.
#
# Placeholders, as normalised values (shape_filter compares whole values):
#   phone: the two invented numbers tests/test_redact_payment_data.py uses,
#          and 2147483647, the largest 32-bit integer, which has a landline's
#          shape.
#   card:  published test numbers (Visa, Mastercard, Amex, Discover). This
#          repo's fixtures build theirs at run time, so none is a literal.
#   IBAN:  the Greek example from the SWIFT IBAN registry.
#   IPv4:  none listed; the RFC 5737 documentation ranges are the placeholders,
#          excluded with the private ranges inside shape_filter.
#   host:  tailnet labels a document would invent.
check_shape "Greek phone number" '(69|2[1-8])( ?[0-9]){8}' phone \
  '2101234567 6912345678 2147483647'
check_shape "Greek IBAN" 'GR[0-9]{2}' iban \
  'GR1601101250000000012300695'
check_shape "Card number" \
  '[0-9]{13}|[0-9]{4}[ -][0-9]{4}[ -][0-9]{4}[ -][0-9]|[0-9]{4}[ -][0-9]{6}[ -][0-9]{4}' card \
  '4012888888881881 5105105105105100 378282246310005 6011111111111117'
check_shape "Public IPv4 address" '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' ipv4
check_shape "Tailnet host" '\.ts\.net' tailnet \
  'example tailnet-name your-tailnet'


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

# The denylist once lived inside this script, so its history is in scope here.
[ "$MODE" = "history" ] && HISTORY_SRC="$HISTORY_ALL"

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
  # In CI the denylist exists only where a PII_DENYLIST secret is set and GitHub
  # hands it over, which it never does for a fork or a Dependabot run, so
  # failing here would just paint those runs red forever. Say plainly that the
  # name checks did not run, and let the generic ones stand.
  #
  # Locally the file should always be there. Its absence is a real
  # misconfiguration, and a guard that cannot evaluate its condition must
  # refuse rather than pass. That distinction is the whole point: the previous
  # version of this script reported OK on every check while, on macOS, scanning
  # exactly zero files.
  if [ "$MODE" = "ci" ]; then
    echo "SKIP [name-based checks]: no denylist at $PII_DENYLIST"
    echo "     Generic org-tell checks above still ran. Name, family, partner and"
    echo "     private-path checks did NOT. Expected where no PII_DENYLIST secret"
    echo "     reaches the job: a fork, a Dependabot run, another repository."
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

if [ "$MODE" = "history" ] && [ "${PR_HEADS_UNSCANNED:-0}" -eq 1 ]; then
  echo "FAIL [pull-request heads]: could not fetch them from origin, so they were not scanned."
  FAIL=1
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
  elif [ "$MODE" = "history" ]; then
    echo "Published history carries PII. Removing it means rewriting history and"
    echo "force-pushing, which is irreversible: an owner decision, not a fix to automate."
    echo "After a rewrite, GitHub keeps old commits reachable through pull-request refs"
    echo "and cached views until GitHub Support purges them."
  else
    echo "Fix the PII leaks above before any public push."
  fi
  exit 1
fi
