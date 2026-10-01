#!/usr/bin/env bash
# Hand a reviewed pull request to the merge queue (squash) and report the merge SHA.
#
# Usage: scripts/factory-merge.sh <pr-number> [expected-head-sha]
#
# Refuses, with a one-line reason and a non-zero exit, unless the PR is open, not a draft, targets
# `main` and has no conflicts; its head equals expected-head-sha when given (a prefix of at least 7
# hex digits is accepted); the latest review verdict for that head, a PR comment or review line
# `Review: MERGE @ <sha-prefix>`, is MERGE; the `check` run for the head succeeded; and, if the PR
# changes an ARCHITECTURE.md, its body fills the template's **Architecture change** section.
# Being up to date with main is not required: the merge queue tests the PR on top of main.
#
# Then runs `gh pr merge --squash --auto --match-head-commit <head>` with the PR title (#N) as the
# commit title and the PR body as the commit message, so the queue merges it. If GitHub rejects
# auto-merge (no queue and auto-merge disabled, or the PR is already mergeable), falls back to the
# REST merge pinned to the head, which needs mergeable_state "clean". See packages/neptune-platform/
# docs/adr/0001-monorepo-workspace-and-merge-queue.md.
#
# Waits for the merge and prints the merge SHA (WAIT=0 returns once queued; MERGE_TIMEOUT seconds,
# default 3600, bounds the wait; exit 3 on timeout). DRY_RUN=1 runs every check and merges nothing.
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
body=$(jq -r '.body // ""' <<<"$pull")

[[ $pr_state == open ]] || refuse "PR is $pr_state"
[[ $draft == false ]] || refuse "PR is a draft"
[[ $base == main ]] || refuse "base is '$base', not 'main'"
[[ -z $expected || $head == "${expected,,}"* ]] || refuse "head $head is not the reviewed SHA $expected"
[[ $mergeable_state != dirty ]] || refuse "PR has conflicts with main; merge origin/main into it"

# The reviewer's verdict: the latest `Review: <VERDICT> @ <sha-prefix>` line, in an issue comment or a
# review body, whose SHA prefixes the head. Markdown emphasis or a quote marker around it is tolerated.
verdict=$(
  {
    gh api --paginate "repos/$repo/issues/$pr/comments" --jq '.[] | {at: .created_at, body}'
    gh api --paginate "repos/$repo/pulls/$pr/reviews" --jq '.[] | {at: .submitted_at, body}'
  } | jq -rs --arg head "$head" '
    [ .[] | .at as $at | (.body // "") | split("\n")[]
      | capture("^[\\s>*_`]*Review:[\\s*_`]*(?<v>[A-Za-z_-]+)[\\s*_`]*@[\\s*_`]*(?<sha>[0-9a-fA-F]{7,40})")
      | select(.sha as $s | $head | startswith($s | ascii_downcase))
      | {at: $at, v: (.v | ascii_upcase)} ]
    | sort_by(.at) | last | .v // empty'
)
[[ $verdict == MERGE ]] ||
  refuse "no 'Review: MERGE @ ${head:0:7}' verdict for the head (latest: '${verdict:-none}')"

# CI reports as a GitHub Actions check run named "check"; a commit status of that name also counts.
conclusion=$(gh api "repos/$repo/commits/$head/check-runs?check_name=check" \
  --jq '[.check_runs[] | select(.status == "completed")] | sort_by(.completed_at) | last | .conclusion // empty')
if [[ -z $conclusion ]]; then
  conclusion=$(gh api "repos/$repo/commits/$head/status" \
    --jq '[.statuses[] | select(.context == "check")] | last | .state // empty')
fi
[[ $conclusion == success ]] || refuse "check for $head is '${conclusion:-missing}', not 'success'"

# ARCHITECTURE.md is a shared diagram: a PR that edits one must say what changed in it.
files=$(gh api --paginate "repos/$repo/pulls/$pr/files" --jq '.[].filename')
if grep -Eq '(^|/)ARCHITECTURE\.md$' <<<"$files"; then
  jq -e '(.body // "") | gsub("<!--[\\s\\S]*?-->"; "") | test("\\*\\*Architecture change\\*\\*")' \
    <<<"$pull" >/dev/null ||
    refuse "PR edits ARCHITECTURE.md but its body has no filled **Architecture change** section"
fi

if [[ ${DRY_RUN:-0} != 0 ]]; then
  echo "would squash-merge #$pr '$title' at $head into $base via the merge queue" >&2
  exit 0
fi

rest_merge() {
  [[ $mergeable_state == clean ]] ||
    refuse "auto-merge was rejected and mergeable_state is '$mergeable_state', not 'clean'"
  jq -n --arg sha "$head" --arg title "$title (#$pr)" --arg body "$body" \
    '{merge_method: "squash", sha: $sha, commit_title: $title, commit_message: $body}' |
    gh api --method PUT "repos/$repo/pulls/$pr/merge" --input - --jq .sha
}

if ! err=$(gh pr merge "$pr" --repo "$repo" --squash --auto --match-head-commit "$head" \
  --subject "$title (#$pr)" --body "$body" 2>&1 >/dev/null); then
  echo "auto-merge rejected (${err//$'\n'/ }); using the pinned REST merge" >&2
  rest_merge
  exit 0
fi

if [[ ${WAIT:-1} == 0 ]]; then
  echo "queued #$pr '$title' at $head; the merge queue will merge it" >&2
  exit 0
fi

owner=${repo%%/*}
name=${repo#*/}
deadline=$((SECONDS + ${MERGE_TIMEOUT:-3600}))
while :; do
  sleep "${POLL_SECONDS:-15}"
  # shellcheck disable=SC2016 # GraphQL variables, not shell ones
  state=$(gh api graphql -F number="$pr" -f owner="$owner" -f name="$name" -f query='
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          state isInMergeQueue mergeCommit { oid } autoMergeRequest { enabledAt }
        }
      }
    }' --jq .data.repository.pullRequest)
  case $(jq -r .state <<<"$state") in
    MERGED)
      jq -r .mergeCommit.oid <<<"$state"
      exit 0
      ;;
    CLOSED) refuse "PR was closed while queued" ;;
  esac
  jq -e '.isInMergeQueue or .autoMergeRequest != null' <<<"$state" >/dev/null ||
    refuse "PR left the merge queue unmerged (the merge group's check failed or it was dequeued)"
  ((SECONDS < deadline)) || {
    echo "#$pr is still queued after ${MERGE_TIMEOUT:-3600}s; re-run with WAIT=0 to stop waiting" >&2
    exit 3
  }
done
