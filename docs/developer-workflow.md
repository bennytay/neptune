# Developer workflow

Status: agreed 2026-09-30; software-factory mode since 2026-10-01 (MVL-74). `AGENTS.md` carries the short
form; this is the reference.

## Setup

```bash
git clone git@github.com:bennytay/neptune.git && cd neptune
make setup            # uv sync --all-groups; installs Python from .python-version if needed
uv run pre-commit install
make check
```

## Issue lifecycle

```
Todo ──(branch created, first commit)──▶ In Progress ──(PR open, acceptance met)──▶ In Review ──(merged)──▶ Done
```

- Start only issues whose `blockedBy` are all Done, or whose blocker's PR is CI-green and In Review (branch
  from that PR). Resume issues already In Progress first. The selection rule is in `AGENTS.md` under *Picking
  the next issue*.
- Branch name = the issue's `gitBranchName` (`benjamintay07/mvl-N-slug`). Linear's GitHub integration links
  PRs by this name and transitions status on merge; enable it once in Linear → Settings → Integrations.
- Comment on the issue when moving to In Review: PR link + acceptance checklist. On Done: the coordinator
  comments the merge SHA.

## Branching and merging

- Trunk-based. `main` is protected: PR required, CI (`check`) green, linear
  history, no force-push, no deletion. Merged branches are deleted automatically.
- One branch per issue. PR title = issue title. Keep the branch current with `git merge origin/main`; never
  rebase or force-push a branch that has been pushed (a reviewer may be reading it).
- **The coordinator merges**, with `scripts/factory-merge.sh <pr> <reviewed-head-sha>`, after an independent
  reviewer agent returns MERGE and CI is green. Implementers open the PR, move the issue to In Review and
  stop: no merging, approving or auto-merge, even on green CI.
- Tags: `m1-gate`, `m2-gate` at review gates; semver `v0.x.y` from M3 onward.

## Commits

Conventional commits, scope = package:

```
feat(model): add TimestampDomain and Timestamp
fix(identity): chunk boundary off-by-one for files under one chunk
docs(adr): 0005 timestamp domains
test(adapters/mcap): truncated-chunk salvage fixture

Refs: MVL-4
```

Attribution trailers required by the tooling in use are appended after `Refs:`.

## Pull requests

Use the template. Reviewers read the visible part in about 30 seconds and skip the rest, so write bottom line
up front (BLUF):

| Section | Limit | Contents |
|---|---|---|
| `Closes MVL-N` | 1 line | links the issue |
| **TL;DR** | 1 line | what now exists that didn't before |
| **Decided** | ≤5 bullets, ≤15 words each | decisions and trade-offs, not files |
| **Progress** | 1 line | milestone count + next issue, e.g. `M1 3/9 · next: MVL-40` |
| Details (collapsed) | — | acceptance checklist, `make check` result, docs/ADRs touched, follow-ups |

No paragraphs in the visible part. Design depth belongs in ADRs and docs, linked rather than pasted. An
unticked acceptance box means the PR stays a draft. Linear comments on status changes follow the same shape.

There is no "Your call" section. Agents make every decision with their best judgement and state it under
**Decided**, with an ADR when it is architectural. PRs, Linear comments and reports never ask the maintainer
to choose; the maintainer can still overrule anything.

**Architecture change.** `ARCHITECTURE.md` is a single diagram, never prose. After each PR, ask whether it
changes a box, an arrow, which box owns a responsibility, or a box's built/partial/not-built styling. If it
does, update the diagram in the same PR and add the template's *Architecture change* section. Phrase it
conceptually ("added this box", "moved responsibility X → Y"), with a tiny before → after Mermaid diagram if
that helps. If it does not, touch neither.

## Review checklist

The implementer applies this before opening the PR; the reviewer agent applies it again, cold.

- Non-negotiables in `AGENTS.md` hold (provenance, explicit unknowns, no silent conversions, determinism).
- Parsing separated from normalisation; no inferred values in `model/`.
- Malformed-input, boundary and determinism tests present. Fixtures real, small, committed.
- Docs updated where a contract changed; ADR written where a decision was made.
- No changes to `model/` after the M1 gate without an ADR.

## Software factory

One coordinator agent runs the project to completion; the maintainer is not in the per-PR loop. Three roles,
each its own agent session:

| Role | Does | Never |
|---|---|---|
| Coordinator (one) | selects issues, spawns implementers and reviewers, refreshes branches, merges, keeps Linear current | writes feature code; merges without a MERGE verdict |
| Implementer (≤ cap) | one issue in one worktree: branch → implement → `make check` → PR → In Review → stop | merges, approves, touches another issue's branch |
| Reviewer (≤2 at once) | reads one PR cold against the checklist above and the non-negotiables; returns a verdict | edits the branch; merges |

**Caps.** 5 implementers in total once the MVL-57 gate is Done (3 before), adapter issues included; 2
reviewers at once. The machine is RAM and CPU bound, so the cap does not rise for adapter work, though
adapter PRs get one review round (below).

### Model policy and review rounds

| Work | Implementer and reviewer model | Review rounds |
|---|---|---|
| ADRs, contracts, milestone gates, `model/`, `store/`, `schema`, `runtime/`, `discovery/` | Opus | two: REVISE goes back to the implementer and the new head is re-reviewed |
| Adapters (`src/neptune/adapters/<format>/` plus their tests and fixtures), connectors, fixtures, docs | Sonnet | one: the reviewer's blockers are fixed in place and the coordinator merges once CI is green on the fixed head and the reviewer confirms each blocker; a second full review is only for a REJECT |

A PR that touches both kinds follows the stricter row. A parser bug found after merge is a new issue, not a
reason to add a round.

### Rules every PR follows

- **Schema changes.** A package-schema change bumps `SCHEMA_VERSION` and the package-schema contract with its
  goldens under `contracts/`. The consumer pin rows in `packages/*/docs/contracts.md` and `contracts/lock.toml`
  move in the same PR, and `make contracts-check` passes. Accepted ADRs are never edited in place: a changed
  decision is a new ADR that supersedes or amends the old one (the index shows "amended by").
- **Expected outcomes.** The PR whose change alters an expected outcome (a golden, a count, a finding code, a
  documented behaviour) updates that expectation and says so; a later PR never "fixes" a test it did not break.
- **Test scratch space.** Tests write under a `TMPDIR` on disk, e.g.
  `TMPDIR=$HOME/.cache/neptune-tmp/mvl-N make test-fast`; `/tmp` is a small tmpfs and fills under parallel
  runs. Delete the directory when the issue is done. With many agents on one machine run targeted tests
  locally and let CI run the full suite.
- **`ARCHITECTURE.md`.** `scripts/factory-merge.sh` already refuses a PR that edits it without a filled
  **Architecture change** section in the body (`architecture_change_filled` in `factory-merge.jq`), so there is
  no separate CI guard.

### Coordinator loop

1. **Select** per *Picking the next issue* in `AGENTS.md`. A Todo issue whose blocker PR is CI-green and In
   Review starts from that PR's branch, and its PR targets that branch until the blocker merges.
2. **Spawn** one implementer per selected issue in its own worktree:
   `git worktree add <path> -b <gitBranchName> origin/main` (or the blocker's branch). Move the issue to In
   Progress. Prompt: issue id, base branch, "Follow AGENTS.md", and the implementer report format below.
3. **Review.** When the implementer reports, spawn a reviewer with the PR number and head SHA. Post the
   verdict as a PR comment (`Review: MERGE|REVISE|REJECT @ <sha>` plus the findings) so a resumed session can
   find it. REVISE goes back to the same implementer and the new head is re-reviewed (adapter PRs: one round, see
   *Model policy*). REJECT closes the PR.
4. **Refresh only when asked.** `factory-merge.sh` merges a PR that is behind `main` when nothing `main`
   changed reaches it (packages/neptune-platform/docs/adr/0005-merge-without-a-queue.md). When it refuses with
   "needs a refresh", bring that PR up to date on its own branch: `git merge origin/main`,
   resolve conflicts (hotspots below), push, `gh pr checks <n> --watch`. A clean auto-merge keeps the verdict
   valid for the new head; a hand-resolved conflict needs a re-review. When a PR's base branch has just
   merged: `gh api -X PATCH repos/bennytay/neptune/pulls/<n> -f base=main`, then merge `origin/main`
   (`git merge -s ours origin/main` is safe only while `git diff origin/main <old-base-tip>` is empty).
5. **Merge** with `scripts/factory-merge.sh <pr> <reviewed-head-sha>`. It refuses unless the base is `main`,
   the PR has no conflicts, main's latest `check` is green (or the PR is labelled `fix-main`), the PR is fresh, the `check` run on that head succeeded and the head is the reviewed SHA; it
   squash-merges pinned to that SHA with the PR title as commit title and the PR body as the message.
6. **Linear.** The integration moves the issue to Done on merge; the coordinator comments the merge SHA, sets
   Done if the integration did not, and removes the worktree (`git worktree remove <path>`).
7. **Close parents.** When every child of a parent issue is Done, set the parent Done with a comment listing
   the children's merge SHAs. Run a gate issue (MVL-56, MVL-57) before any issue of the next milestone.

### Implementer report

Returned to the coordinator once the PR is open and the issue is In Review; at most 12 lines:
PR number · head SHA · files changed · anything the coordinator must do (ADR number taken, hotspot files
touched, a Linear update that failed, a follow-up issue to file).

### Reviewer report

Fresh session; the PR head checked out in its own worktree (`git worktree add <path> <sha>`). Reads the
issue, the PR, the full diff and the ADRs it cites; runs `make check`; applies the review checklist and the
non-negotiables; checks every acceptance box against the code. At most 15 lines:

```
Verdict: MERGE | REVISE | REJECT · PR #n · head <sha>
Acceptance: k/n met · make check: green | <what failed>
- [blocker|should|nit] path:line — one finding per line
```

MERGE needs zero blockers; nits never block. REVISE lists the blockers. REJECT is for a wrong approach (a
non-negotiable broken, work outside the issue's scope) and is rare.

### Conflict hotspots

Files most PRs touch; conflicts here are about ordering, not semantics:

- `ARCHITECTURE.md`: one Mermaid diagram. Keep both sides' boxes, arrows and status styling.
- ADR numbers. Implementers take the next free number after checking `main` and every open PR; when two PRs
  collide, the coordinator renumbers the later one at refresh time (file name, title, every `ADR 00NN`
  reference) and tells its implementer.
- `docs/adr/README.md` is generated, not a hotspot: `make fmt` (or `make adr-index`) rebuilds it from the ADR
  files' headings, `Status:` and `Amends:` lines, and `make check` fails when it is stale. A refresh conflict on
  it is resolved by `make adr-index`, never by hand; the same holds for each package's `docs/adr/README.md`.
- `src/neptune/adapters/builtin.py`: the built-in adapter list (lands with MVL-7). Keep the union of entries.
- Generated files (`docs/schema/`, golden packages): never hand-merge; run `make schema` / `make examples`
  after merging `origin/main` and commit the result.

### Resuming the factory

A fresh coordinator session needs nothing but the repository, `gh` and Linear:

```bash
git fetch origin --prune
git worktree list                                      # live implementer checkouts
gh pr list --state open --json number,title,headRefName,baseRefName,mergeStateStatus
gh api repos/bennytay/neptune/issues/<n>/comments --jq '.[].body'   # last "Review: … @ <sha>" verdict
# Linear: issues In Progress and In Review in project P-MVL-11 (team MVL)
```

Reconcile, then continue the loop from step 1:

| Found | Do |
|---|---|
| In Review, PR open, `Review: MERGE @ <sha>` matches the current head | step 5 |
| In Review, PR open, no verdict or a stale one | step 3 |
| In Progress, worktree and branch exist, no PR | respawn an implementer in that worktree: "Resume MVL-N" |
| In Progress, no worktree | step 2 again, reusing the remote branch if one exists |
| PR merged, issue not Done | step 6 |
| PR base is a deleted branch, or `mergeStateStatus` is BEHIND or DIRTY | step 4 |
| Worktree with no open PR and no In Progress issue | `git worktree remove` it |

## Gates

At `MVL-56` (M1) and `MVL-57` (M2) feature work stops; the gate issue's stress test is run on paper against
the current contract, findings are recorded in `docs/reviews/`, foundational problems are fixed, and `main`
is tagged. No M(n+1) issue starts before the M(n) gate is Done. These are the only two gates; after `MVL-57`
the dependency graph is the only sequencing control and the implementer cap rises to 5.
