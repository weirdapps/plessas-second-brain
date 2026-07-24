#!/usr/bin/env bash
# pii-gauntlet.sh — verify no personal data leaked into TRACKED files in this
# repo before any public push.
#
# Portable: resolves its own repo via git rev-parse, runs from any cwd.
# Git-aware: scans only tracked files (`git ls-files`), so gitignored data/,
# local .env files, and on-disk personal data don't trigger false fails.
#
# Run: ./scripts/pii-gauntlet.sh   (exit 0 = clean, non-zero = PII detected)
#
# Self-exclusion: this script contains the patterns it searches for, so it is
# filtered out of the scanned file list.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
  echo "ERROR: script is not inside a git repository ($SCRIPT_DIR)" >&2
  exit 2
fi
cd "$REPO_ROOT"

echo "=== PII Gauntlet ==="
echo "Repo: $REPO_ROOT"
echo

FAIL=0

# Tracked files minus intentional exclusions (LICENSE copyright, lockfiles, self).
TRACKED_FILES=$(git ls-files \
  | grep -vE '(^|/)(pii-gauntlet\.sh|LICENSE|LICENSE\.md|uv\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock)$' \
  || true)

if [ -z "$TRACKED_FILES" ]; then
  echo "ERROR: no tracked files found" >&2
  exit 2
fi

check() {
  local label="$1"
  local pattern="$2"
  local exclude_substring="${3:-}"
  local hits
  hits=$(echo "$TRACKED_FILES" | tr '\n' '\0' | xargs -0 grep -inIE "$pattern" 2>/dev/null || true)
  if [ -n "$exclude_substring" ] && [ -n "$hits" ]; then
    hits=$(echo "$hits" | grep -viE "$exclude_substring" || true)
  fi
  if [ -n "$hits" ]; then
    echo "FAIL [$label]:"
    echo "$hits" | head -20
    echo
    FAIL=1
  else
    echo "OK   [$label]"
  fi
}

# Owner full name (single-token surname alone is allowed — it is the repo brand)
check "Owner full name (EN)" "Dimitris[[:space:]]+Plessas|Dimitrios[[:space:]]+Plessas|Plessas[[:space:]]*,[[:space:]]*Dimitri"
check "Owner full name (GR)" "Δημήτριος[[:space:]]+Πλέσσας|Πλέσσας[[:space:]]+Δημήτριος"

# Personal emails
check "Personal email" "dimitrios\.plessas@|d\.plessas@|plessas@nbg|plessas@gmail|plessas@yahoo"

# Colleague / family names
check "Peer/colleague names" "\b(Volioti|Bitrou|Sioutis|Theofilidi|Θεοφιλίδη|Lygeros|Oikonomou|Maraveas|Xona|Petropoulou|Laspas|Koutra|Giemelou|Argyriou)\b"
check "Family names" "\b(Kitrilaki|Κιτριλάκη)\b"

# Org tells
check "Managed tenant host" "groupnbg"
check "Org email domain" "nbg\.gr"
check "Org name" "\bNBG\b"

# Infrastructure
check "VPS IP" "167\.233\.42\.38"

# User-specific paths
check "Owner paths" "/Users/plessas|/home/plessas"

# Generic /Users/<user>/ paths — allow conventional placeholders only
check "Generic user paths" "/Users/[A-Za-z0-9_.-]+/" \
  "/Users/(you|user|USER|USERNAME|me|jdoe|Shared|Public|Library|Guest|\.\.\.)/"

# Personal GCP project IDs + Greek tax/ID refs
check "Personal GCP project IDs" "gen-lang-client-[0-9]+"
check "Tax authority refs" "ΑΑΔΕ|ΑΦΜ|ΑΔΤ|ΑΜΚΑ"

echo
if [ $FAIL -eq 0 ]; then
  echo "=== GAUNTLET PASS ==="
  exit 0
else
  echo "=== GAUNTLET FAIL ==="
  echo "Fix the leaks above before any public push."
  exit 1
fi
