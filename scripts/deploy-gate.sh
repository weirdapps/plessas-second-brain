#!/usr/bin/env bash
# deploy-gate.sh: the producer runs only a commit whose required CI checks passed.
#
#   deploy-gate.sh pull [checkout]
#
# The hourly puller (repo-autoupdate, in the owner's config repo) calls this in place of
# `git pull --ff-only` for this repository. It fetches master, then fast-forwards the
# checkout to the NEWEST first-parent commit of origin/master whose required checks
# (REQUIRED_CHECKS) all concluded `success` on GitHub, read with `gh api`. A commit that
# is newer but not yet verified (CI running, red, or never run) is held. The puller runs
# scripts/deploy.sh only when HEAD moved, as before.
#
# Why: the master ruleset requires these checks before a merge, but a repository role can
# bypass it, and a pull took whatever master held. The gate makes the producer's own
# check of the verdict the last word. (Open item of the 2026-11-01 security audit.)
#
# The last line of the output is the verdict, which the puller logs:
#   OK        HEAD is origin/master.
#   DEPLOYED  fast-forwarded to a verified commit this run.
#   HOLD      origin/master is ahead and not verified. Exit 0 while the newest unverified
#             commit is younger than HOLD_GRACE_SECS (CI may still be running), then 1, so
#             a red or missing verdict fails the puller's unit and pages.
#   SKIP      not on master, or a dirty tree: a person is working here. Nothing moves.
#   FAIL      fetch or fast-forward failed. Exit 1.
#
# Undo: remove this repository from the puller's GATES list; it then pulls ungated.
# Bash 3.2 clean, so the tests run on stock macOS too.
set -uo pipefail

REQUIRED_CHECKS="lint
test (3.12)
test (3.14)
types (3.12)
types (3.14)
pii-gauntlet
wrappers"
HOLD_GRACE_SECS="${DEPLOY_GATE_GRACE_SECS:-7200}"
REPO_SLUG="${DEPLOY_GATE_REPO:-weirdapps/plessas-second-brain}"

short() { printf '%s' "${1:0:9}"; }

# verified <sha>: rc 0 when every required check has a run on <sha> that concluded success.
verified() {
  local sha="$1" passed name
  passed="$(gh api "repos/$REPO_SLUG/commits/$sha/check-runs?per_page=100" \
    --jq '.check_runs[] | select(.conclusion == "success") | .name' 2>/dev/null)" || return 1
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    printf '%s\n' "$passed" | grep -Fxq -- "$name" || return 1
  done <<EOF
$REQUIRED_CHECKS
EOF
  return 0
}

pull() {
  local repo="${1:-$PWD}" head target sha newest age
  cd "$repo" || { echo "FAIL cannot enter $repo"; return 1; }
  if [ "$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" != "master" ]; then
    echo "SKIP not on master"
    return 0
  fi
  if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    echo "SKIP dirty tree"
    return 0
  fi
  if ! git fetch -q origin master 2>&1; then
    echo "FAIL fetch"
    return 1
  fi
  head="$(git rev-parse HEAD)"
  target="$(git rev-parse origin/master)"
  if [ "$head" = "$target" ]; then
    echo "OK $(short "$head")"
    return 0
  fi
  if ! git merge-base --is-ancestor "$head" "$target"; then
    echo "SKIP HEAD $(short "$head") is not behind origin/master"
    return 0
  fi
  newest=""
  for sha in $(git rev-list --first-parent "$head..$target"); do
    [ -z "$newest" ] && newest="$sha"
    if verified "$sha"; then
      if ! git merge -q --ff-only "$sha" 2>&1; then
        echo "FAIL fast-forward to $(short "$sha")"
        return 1
      fi
      if [ "$sha" = "$target" ]; then
        echo "DEPLOYED $(short "$sha")"
      else
        echo "DEPLOYED $(short "$sha"), holding $(short "$target") (not verified yet)"
      fi
      return 0
    fi
  done
  age=$(($(date +%s) - $(git show -s --format=%ct "$newest")))
  echo "HOLD $(short "$head"), origin/master $(short "$target") not verified (${age}s old)"
  [ "$age" -lt "$HOLD_GRACE_SECS" ]
}

case "${1:-}" in
  pull) shift; pull "$@" ;;
  *) echo "usage: $0 pull [checkout]" >&2; exit 2 ;;
esac
