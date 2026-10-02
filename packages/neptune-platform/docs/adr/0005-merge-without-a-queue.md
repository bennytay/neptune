# 0005 — Merge without a queue: no strict up-to-date rule, a freshness check, stop the line on a red main

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-192
- Amends: [0001](0001-monorepo-workspace-and-merge-queue.md) §5 (the merge queue stays the target once the
  repository moves to an organisation)

## Context

ADR 0001 relied on GitHub's merge queue. GitHub rejects the `merge_queue` rule on `bennytay/neptune` because
merge queues need an organisation-owned repository, and the maintainer does not want an organisation yet.
The fallback kept the strict "branch must be up to date" rule, so only one PR in the whole repository could
be mergeable at a time: every merge forced every coordinator to refresh every open PR and wait for CI again.
With four coordinators that loop was the throughput ceiling and a large share of token spend.

Most refreshes protect nothing. A compiler adapter PR and a Ledger PR do not share code; re-running the
adapter's tests on top of the Ledger change cannot change their result.

## Decision

1. **`main` drops the strict up-to-date rule.** It keeps: PR required, `check` required, linear history,
   no force-push, no deletion.
2. **`scripts/factory-merge.sh` decides when a refresh is needed**, using `scripts/merge_freshness.py`.
   When the PR is behind `main`, it lists what `main` changed since the merge base and maps both sides to
   units: root plumbing and `contracts/**` are everything; `packages/<name>/**` is that member (`harness/**`
   is the platform's); `src/neptune/adapters/<format>/**` is that adapter alone; the template is its own
   unit; every other path is the compiler core. Each unit reaches the members that depend on it,
   transitively, through the workspace graph CI already uses (`ci_plan.py`); the compiler core also reaches
   every adapter. If the two reached sets are disjoint the PR merges as it stands; otherwise the script
   refuses with "needs a refresh" and the reason, and the coordinator refreshes as before.
3. **Stop the line.** `push` to `main` runs every CI job. While the latest completed `check` on `main` is a
   failure, the script refuses every merge except a PR labelled `fix-main`.
4. **One merge at a time per machine.** The script holds an `flock` on `<git-common-dir>/factory-merge.lock`
   from reading the PR through the merge, so coordinators on this machine never decide against a stale
   `main`.
5. **Conservative where cheap.** The merge base is older than or equal to the base CI tested, so the listed
   `main` changes are a superset of what CI missed. Three hundred or more changed files (the compare API's
   limit) count as a refresh.

## Alternatives considered

- **Move to a GitHub organisation** (MVL-192's original scope). Correct long term, declined for now by the
  maintainer. Remains the target: once the queue is live, this ADR's freshness check becomes redundant and
  is removed by a superseding ADR.
- **Keep strict up-to-date and refresh by hand.** Measured cost: every merge invalidated every open PR.
- **Drop strict with no check.** Semantic conflicts between packages that depend on each other (a compiler
  core change under a Ledger PR) would reach `main` unseen until the next push run.
- **A self-hosted merge bot that serialises merges and tests the merged tree.** Equivalent to a queue but a
  new service to run; the freshness rule plus stop-the-line gets most of the benefit with one script.

## Consequences

- PRs in independent units merge without refresh: adapters against adapters, adapters against members,
  members that do not depend on each other. Compiler-core, `uv.lock` and `contracts/` changes still force
  refreshes of everything they reach; that is the point.
- A semantic break the rule misses shows up as a red `check` on `main` within one CI run, and the line
  stops until a `fix-main` PR lands. Coordinators treat a red `main` as their first issue.
- The adapter isolation the rule relies on (adapters never import each other; no member imports an adapter)
  is an AGENTS.md rule; a PR that breaks it must change this ADR.
