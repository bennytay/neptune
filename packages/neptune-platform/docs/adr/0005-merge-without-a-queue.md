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
2. **Freshness is CI's own job selection.** When a PR is behind `main`, `scripts/factory-merge.sh` lists
   what the PR changed and what `main` changed since the merge base (both paths of a rename), and
   `scripts/merge_freshness.py` runs each list through `ci_plan.plan` with the workspace graph read at
   `origin/main`. A job selected by both sides ran on the PR against inputs `main` has since changed, so
   the PR needs a refresh; disjoint job sets mean every job the PR ran would give the same result today,
   and it merges as it stands.
3. **The platform job is excluded from that comparison.** Its harness ingests through the whole stack, so
   it depends on everything and would make every pair of PRs overlap. Integration breaks between two
   independently green PRs surface in the `push` run on `main`, which runs every job.
4. **Stop the line.** The newest `main` commit whose `check` has completed decides (a running one defers to
   its parent). While it is `failure`, `cancelled`, `timed_out` or `action_required`, the script refuses
   every merge except a PR labelled `fix-main`.
5. **One merge at a time per machine.** The script holds an `flock` on `<git-common-dir>/factory-merge.lock`
   from reading the PR through the merge, and refuses to run without `flock`.
6. **CI selection amended (ADR 0001 §4).** A change confined to format adapters' own subpackages
   (`src/neptune/adapters/<format>/**`) runs the compiler and the platform, not every member depending on
   the compiler: no member imports an adapter (a test enforces it) and only the harness ingests.
   `scripts/contracts.py` is plumbing, because every job runs it.
7. **Conservative where cheap.** The merge base is no newer than the base CI tested, so the listed `main`
   changes are a superset of what CI missed. Three hundred or more changed files (the compare API's
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

- A compiler adapter PR merges past Ledger, Memory, Context, Deploy and Learn merges without a refresh,
  and member PRs merge past each other unless one depends on the other. Two compiler PRs (adapter or core)
  still refresh each other, because discovery probes every adapter against every file.
- `uv.lock`, root plumbing, `contracts/` and `scripts/contracts.py` changes refresh everything they reach;
  that is the point.
- A semantic break the rule misses shows up as a red `check` on `main` within one CI run, and the line
  stops until a `fix-main` PR lands. Coordinators treat a red `main` as their first issue.
- If a member ever needs to import a format adapter, the guarding test fails and this ADR must be
  superseded first.
