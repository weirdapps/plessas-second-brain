#!/usr/bin/env bash
# deploy-gate.sh: the producer runs only a commit whose required CI checks passed.
#
#   deploy-gate.sh pull [checkout]
#
# The hourly puller (repo-autoupdate, in the owner's config repo) calls this in place of
# `git pull --ff-only` for this repository. It fetches master, then fast-forwards the
# checkout to the NEWEST first-parent commit of origin/master whose required checks
# (REQUIRED_CHECKS) all concluded `success` on GitHub, read with `gh api` (for an
# auto-merged squash commit, on its PR head: see verified()). A commit that
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

# checks_passed <sha>: rc 0 when every required check has a run on <sha> that concluded
# success.
checks_passed() {
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

# verified <sha>: its own checks passed, or it is the squash merge of a pull request whose
# head passed them with the same content.
#
# Auto-merge pushes the squash commit with GITHUB_TOKEN, and GitHub starts no workflow for
# such a push, so a Dependabot merge never gets a check run of its own and the gate held it
# until someone dispatched CI by hand (#327, held 12h on 2026-11-16). The PR's checks
# count for the squash commit only when all of these hold:
#   - GitHub names a pull request whose merge_commit_sha IS <sha>;
#   - the PR head has the same tree as <sha>: the merged files are the tested files;
#   - <sha>'s parent is an ancestor of the PR head (compare status ahead or identical), so
#     the head already held the base it landed on and CI's merge ref equals that tree;
#   - every required check passed on the PR head.
verified() {
  local sha="$1" pr_head tree status
  checks_passed "$sha" && return 0
  pr_head="$(gh api "repos/$REPO_SLUG/commits/$sha/pulls" \
    --jq ".[] | select(.merge_commit_sha == \"$sha\") | .head.sha" 2>/dev/null | head -n 1)"
  [ -n "$pr_head" ] || return 1
  tree="$(gh api "repos/$REPO_SLUG/commits/$pr_head" --jq .commit.tree.sha 2>/dev/null)" || return 1
  [ -n "$tree" ] && [ "$tree" = "$(git rev-parse "$sha^{tree}")" ] || return 1
  status="$(gh api "repos/$REPO_SLUG/compare/$(git rev-parse "$sha^")...$pr_head" \
    --jq .status 2>/dev/null)" || return 1
  case "$status" in
    ahead | identical) ;;
    *) return 1 ;;
  esac
  checks_passed "$pr_head"
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
