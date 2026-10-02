#!/usr/bin/env bash
# Hand a reviewed pull request to the merge queue (squash) and report the merge SHA.
#
# Usage: scripts/factory-merge.sh <pr-number> [expected-head-sha]
#
# Refuses, with a one-line reason and a non-zero exit, unless the PR is open, not a draft, targets
# `main` and has no conflicts; its head equals expected-head-sha when given (a prefix of at least 7
# hex digits is accepted); the latest review verdict for that head, a PR comment or review line
# `Review: MERGE @ <sha-prefix>` by an OWNER, MEMBER or COLLABORATOR, is MERGE (a later REVISE
# overrides an earlier MERGE); the `check` run for the head succeeded; and, if the PR changes an
# ARCHITECTURE.md, its body's **Architecture change** section has content, not just the heading.
# The JSON filters live in scripts/factory-merge.jq.
# Being up to date with main is not required (there is no merge queue on a personal-account repo,
# packages/neptune-platform/docs/adr/0005-merge-without-a-queue.md). Instead, holding a machine-wide
# lock so coordinators merge one at a time, it refuses while the latest `check` on main failed (unless
# the PR carries the `fix-main` label), and, when the PR is behind main, asks scripts/merge_freshness.py
# whether anything main changed since the merge base could change the jobs that tested the PR; if so it
# refuses with "needs a refresh" and the reason, otherwise it merges the PR as it stands.
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
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
[[ -f $here/factory-merge.jq ]] || refuse "missing $here/factory-merge.jq"

repo=${GH_REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}

# One merge at a time on this machine: every coordinator's decision sees the main the merge lands on.
lock=${FACTORY_MERGE_LOCK:-$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || echo /tmp)/factory-merge.lock}
if command -v flock >/dev/null; then
  exec 9>"$lock"
  flock -w "${LOCK_TIMEOUT:-1800}" 9 || refuse "another merge held $lock for ${LOCK_TIMEOUT:-1800}s"
fi

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
# review body by an OWNER, MEMBER or COLLABORATOR, whose SHA prefixes the head (scripts/factory-merge.jq).
verdict=$(
  {
    gh api --paginate "repos/$repo/issues/$pr/comments" \
      --jq '.[] | {at: .created_at, association: .author_association, body}'
    gh api --paginate "repos/$repo/pulls/$pr/reviews" \
      --jq '.[] | {at: .submitted_at, association: .author_association, body}'
  } | jq -rs -L "$here" --arg head "$head" 'include "factory-merge"; verdict($head)'
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

files=$(gh api --paginate "repos/$repo/pulls/$pr/files" --jq '.[].filename')

# Stop the line: nothing merges on top of a red main except a fix for it.
main_check=$(gh api "repos/$repo/commits/main/check-runs?check_name=check" \
  --jq '[.check_runs[] | select(.status == "completed")] | sort_by(.completed_at) | last | .conclusion // empty')
if [[ $main_check == failure ]] &&
  ! jq -e '[.labels[]?.name] | index("fix-main")' <<<"$pull" >/dev/null; then
  refuse "the latest check on main failed; fix main first (label the fixing PR 'fix-main')"
fi

# Behind main: merge as is only when nothing main changed since the merge base reaches what the PR
# changed (scripts/merge_freshness.py); the compare API lists at most 300 files, so more is a refresh.
compare=$(gh api "repos/$repo/compare/main...$head" --jq '{behind: .behind_by, base: .merge_base_commit.sha}')
if [[ $(jq -r .behind <<<"$compare") != 0 ]]; then
  main_files=$(gh api "repos/$repo/compare/$(jq -r .base <<<"$compare")...main" --jq '.files[].filename')
  [[ $(grep -c . <<<"$main_files") -lt 300 ]] ||
    refuse "needs a refresh: main changed 300+ files since the merge base; merge origin/main into it"
  scratch=$(mktemp -d)
  trap 'rm -rf "$scratch"' EXIT
  printf '%s\n' "$files" >"$scratch/pr"
  printf '%s\n' "$main_files" >"$scratch/main"
  freshness=$(python3 "$here/merge_freshness.py" "$scratch/pr" "$scratch/main") ||
    refuse "needs a refresh (${freshness#refresh: }); merge origin/main into it, wait for check, re-verdict"
  echo "#$pr is $(jq -r .behind <<<"$compare") commit(s) behind main; nothing it reaches changed, merging as is" >&2
fi

# ARCHITECTURE.md is a shared diagram: a PR that edits one must say what changed in it.
if grep -Eq '(^|/)ARCHITECTURE\.md$' <<<"$files"; then
  jq -e -L "$here" 'include "factory-merge"; architecture_change_filled' <<<"$pull" >/dev/null ||
    refuse "PR edits ARCHITECTURE.md but its body has no filled **Architecture change** section"
fi

if [[ ${DRY_RUN:-0} != 0 ]]; then
  echo "would squash-merge #$pr '$title' at $head into $base via the merge queue" >&2
  exit 0
fi

rest_merge() {
  [[ $mergeable_state == clean || $mergeable_state == unstable || $mergeable_state == behind ]] ||
    refuse "auto-merge was rejected and mergeable_state is '$mergeable_state'"
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
