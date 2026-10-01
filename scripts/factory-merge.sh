#!/usr/bin/env bash
# Squash-merge a reviewed pull request into main.
#
# Usage: scripts/factory-merge.sh <pr-number> [expected-head-sha]
#
# Refuses, with a one-line reason and a non-zero exit, unless the PR is open, targets `main`,
# GitHub reports mergeable_state "clean" (up to date, no conflicts, required checks green), the
# `check` run for the head commit succeeded, and the head equals the SHA the reviewer approved
# (a prefix of at least 7 hex digits is accepted). Then merges pinned to that head with the PR
# title as the commit title and the PR body as the commit message, and prints the merge SHA.
#
# Set DRY_RUN=1 to run every check and report what would be merged without merging.
set -euo pipefail

usage() {
  echo "usage: $0 <pr-number> [expected-head-sha]" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
pr=$1
expected=${2:-}
[[ $pr =~ ^[0-9]+$ ]] || usage
[[ -z $expected || $expected =~ ^[0-9a-fA-F]{7,40}$ ]] || usage

refuse() {
  echo "refusing to merge #$pr: $*" >&2
  exit 1
}

command -v gh >/dev/null || refuse "gh is not installed"
command -v jq >/dev/null || refuse "jq is not installed"

repo=${GH_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}

# mergeable_state reads "unknown" for a few seconds after a push while GitHub recomputes it.
pull=""
for _ in 1 2 3 4 5 6; do
  pull=$(gh api "repos/$repo/pulls/$pr")
  [[ $(jq -r .mergeable_state <<<"$pull") == unknown ]] || break
  sleep 5
done

pr_state=$(jq -r .state <<<"$pull")
draft=$(jq -r .draft <<<"$pull")
base=$(jq -r .base.ref <<<"$pull")
head=$(jq -r .head.sha <<<"$pull")
mergeable_state=$(jq -r .mergeable_state <<<"$pull")
title=$(jq -r .title <<<"$pull")

[[ $pr_state == open ]] || refuse "PR is $pr_state"
[[ $draft == false ]] || refuse "PR is a draft"
[[ $base == main ]] || refuse "base is '$base', not 'main'"
[[ -z $expected || $head == "${expected,,}"* ]] || refuse "head $head is not the reviewed SHA $expected"
[[ $mergeable_state == clean ]] ||
  refuse "mergeable_state is '$mergeable_state', not 'clean' (needs: up to date with main, no conflicts, checks green)"

# CI reports as a GitHub Actions check run named "check"; a commit status of that name also counts.
conclusion=$(gh api "repos/$repo/commits/$head/check-runs?check_name=check" \
  --jq '[.check_runs[] | select(.status == "completed")] | sort_by(.completed_at) | last | .conclusion // empty')
if [[ -z $conclusion ]]; then
  conclusion=$(gh api "repos/$repo/commits/$head/status" \
    --jq '[.statuses[] | select(.context == "check")] | last | .state // empty')
fi
[[ $conclusion == success ]] || refuse "check for $head is '${conclusion:-missing}', not 'success'"

if [[ ${DRY_RUN:-0} != 0 ]]; then
  echo "would squash-merge #$pr '$title' at $head into $base" >&2
  exit 0
fi

jq -n --arg sha "$head" --arg title "$title (#$pr)" --arg body "$(jq -r '.body // ""' <<<"$pull")" \
  '{merge_method: "squash", sha: $sha, commit_title: $title, commit_message: $body}' |
  gh api --method PUT "repos/$repo/pulls/$pr/merge" --input - --jq .sha
