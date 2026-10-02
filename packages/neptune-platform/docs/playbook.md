# Multi-coordinator factory playbook

The operating procedure for running Neptune's seven Linear projects in parallel in one repository. Sources:
the programme document *Neptune Program — Stack, Tracks & Contracts* (§5 contracts, §6 tracks, §7b model
policy), ADR [0001](adr/0001-monorepo-workspace-and-merge-queue.md) (workspace, merge queue),
ADR [0002](adr/0002-contracts-registry-and-version-policy.md) (contracts) and
ADR [0003](adr/0003-multi-coordinator-playbook-and-versioning.md) (this playbook, versioning). The per-PR
loop itself is `docs/developer-workflow.md` § Software factory; this file adds what changes when several
coordinators share the repository, the budget and Linear.

## 1. Roles

| Role | Model | Owns | Never |
|---|---|---|---|
| Programme coordinator (one Zed thread) | Fable | programme document, `contracts/` registry policy, the cross-project gate edges of programme document §6 (§ 2), capacity split, budget, starting and pausing coordinators, the integration gate | is spawned as a subagent; writes feature code |
| Project coordinator (one Zed thread per live project) | Opus | its project's issues, implementers, reviewers, merges, Linear state, its package's gate tags and releases | edits outside `packages/<name>/` except `contracts/` with a version bump; talks to another coordinator directly |
| Implementer (worktree subagent) | per § 4 | one issue, one branch, one PR, stops at In Review | merges, approves, enables auto-merge, touches another branch |
| Reviewer (fresh subagent) | same tier as the work | one verdict on one head SHA | edits the branch, merges |

**Caps.** Default 3–4 project coordinators live; each runs at most 3 implementers and 1 reviewer. The compiler
keeps 5 implementers + 2 reviewers while M7 is on the critical path. Which projects are live is the current
wave in programme document §6; the programme coordinator starts a coordinator only for a project with work in
the current wave, and raises a cap only when the weekly check (§ 6) shows the budget idle.

## 2. Linear is the bus

- Coordinators never message each other. Every cross-project fact is a Linear issue, relation or comment;
  every artefact is a GitHub PR.
- **Reporting up.** A project coordinator reports to the programme coordinator only (a) when a gate issue is
  Done or (b) when it has had nothing selectable for more than one hour because of another project. It reports
  as one comment on the programme document: `[P-MVL-<n>] gate <code> Done @ <merge-sha>` or
  `[P-MVL-<n>] blocked: <MVL-x> waits on <MVL-y> (<project>) since <time>`.
- **Cross-project blockers.** The programme coordinator owns the cross-project gate edges of programme
  document §6. The only cross-project edge a project coordinator adds is the `blocks` edge of a contract
  request (below). A dependent issue becomes selectable when its cross-project blocker is **Done**; a
  coordinator never branches from another project's open PR (stacking stays inside one project). To clear a
  blocker the programme coordinator either raises the blocker's priority on its project or, if the edge is
  wrong, removes it with a comment saying why.
- **Contract change requests.** A consumer that needs a change in a contract it does not own:
  1. files an issue in the **owning** project titled `Contract <contract-id>: <change>`, stating the need,
     the consuming issue and whether it is breaking, additive or editorial;
  2. adds a `blocks` edge from that issue to its own consuming issue;
  3. waits. The owner's coordinator schedules it (contract work is Opus tier and is never paused); its PR
     bumps the owner's version constant and runs `scripts/contracts.py bump` (ADR 0002 §7), whose
     announcement lands on each consumer's current gate issue (`contracts/packages.toml`);
  4. on the announcement, each consumer's coordinator files an issue in its own project to raise its entry in
     `contracts/lock.toml`. Nobody edits another package's lock entry.

  If the owner declines or the two disagree on breaking vs additive, either side comments on the programme
  document and the programme coordinator decides on the issue; a design decision becomes an ADR in the owner.

## 3. Starting and resuming a project coordinator

**Starting** (programme coordinator):

1. Confirm the project has work in the current wave and its first issue's blockers are Done (§6).
2. Confirm `packages/<name>/` exists. If not, the project's scaffold issue is its first issue:
   `scripts/new-package.sh <name>`; the root `uv.lock` change it makes is the one permitted edit outside the
   package. The scaffold PR pastes the § 4 block into `packages/<name>/AGENTS.md`.
3. Confirm `contracts/packages.toml` names the project's current `gate_issue` and `contracts/lock.toml` has
   an entry if the package consumes a stable contract.
4. Move the Linear project to In Progress; its description links this playbook.
5. Open a Zed thread on Opus and paste the prompt below with the three placeholders filled from the table.
6. Comment on the programme document: `[P-MVL-<n>] coordinator started <date>`.

**Coordinator prompt (verbatim from programme document §6; fill the placeholders, change nothing else):**

> You coordinate <project name> (P-MVL-<n>) in `packages/<name>/` of `bennytay/neptune`. Read `AGENTS.md`, `packages/<name>/AGENTS.md` and the programme document's model policy. Run the software-factory loop in `docs/developer-workflow.md`. Caps: 3 implementers, 1 reviewer. Pick only issues in this project whose blockers are Done. Implementers and reviewers run on `sonnet` unless the issue is an ADR, a contract, a gate, or touches `store/`, `schema/`, `consolidate/`, `query/`; those run on `opus`. Merge with `scripts/factory-merge.sh`; refresh a PR only when it says so. Never edit outside `packages/<name>/` except `contracts/` with a version bump. Keep your context lean: never read agent transcripts, require 15-line reports, do not re-read the repo. Report to the programme coordinator only at a gate or when blocked for more than one hour.

| `<project name>` | `<n>` | `<name>` |
|---|---|---|
| Neptune Ledger — Catalog & Lakehouse | 12 | `neptune-ledger` |
| Neptune Memory — Bi-temporal Deployment Memory | 13 | `neptune-memory` |
| Neptune Context — Retrieval & Context Engine | 14 | `neptune-context` |
| Neptune Deploy — Deployment Record Layer & Connectors | 15 | `neptune-deploy` |
| Neptune Learn — Datasets & Flywheel | 16 | `neptune-learn` |
| Neptune Platform — Contracts, Security, Deployment & Integration | 17 | `neptune-platform` (also owns `contracts/`, `harness/`) |

The compiler (P-MVL-11, repository root) keeps its existing factory session under root `AGENTS.md`; it is not
started from this prompt.

**Resuming.** A dead coordinator thread is replaced by a new Zed thread with the same filled prompt. The loop's
first selection rule (resume In Progress issues with no live implementer) does the rest; the state to read is
`docs/developer-workflow.md` § Resuming the factory, restricted to this project's issues and branches.

## 4. Model map and token rules (block for every package `AGENTS.md`)

Each package's `AGENTS.md` carries this block verbatim, between the markers, after its layer-specific section.
This file is its source; change it here and re-copy it, never edit a copy.

```markdown
<!-- BEGIN model-policy: copied from packages/neptune-platform/docs/playbook.md § 4; do not edit here -->
## Model policy and token rules

| Work | Implementer | Reviewer | Review rounds |
|---|---|---|---|
| ADRs; anything in `contracts/`; gate issues; code under `store/`, `schema/`, `consolidate/`, `query/`, `runtime/`, `model/`; a module that exports a contract schema or version constant | opus | opus | up to two full reviews |
| Adapters, connectors, fixtures and generators, exporters, docs, scaffolds, console UI, dashboards | sonnet | sonnet | one review; REVISE blockers re-checked by the same reviewer |
| Mechanical: branch refresh, PR body edits, renames, generated files (`docs/adr/README.md` ADR indexes, `contracts/compatibility.md`) | sonnet | sonnet | as a commit on a PR that already has a reviewer: that PR's review covers it; as a standalone PR: one review and a posted verdict |

An issue that touches both tiers runs at the higher one. Every implementer and reviewer prompt includes the
token rules: read `AGENTS.md`, this file, the issue and only the files the issue names; no exploratory reads;
`make test-fast` (or `make check PKG=<this package>`) while iterating and `make check` once before the PR;
commit and push after every coherent step; report in at most 15 lines; after three fix rounds or two hours,
stop and report instead of grinding.

Budget pauses: before each selection, read this project's latest Linear status update. If it begins
`Paused:`, start no new issue of the classes it names; in-flight issues finish. Contracts and gates are never
paused.

Cross-project rules (reporting up, blockers, contract requests) are in
`packages/neptune-platform/docs/playbook.md` § 2; the merge flow is in § 5 of the same file.
<!-- END model-policy -->
```

**Implementer prompt** (coordinator fills `<…>`):

```text
You are the implementer for <MVL-N> (<title>; <project> P-MVL-<n>) in bennytay/neptune.
Worktree: <path>, branch <gitBranchName> based on <base>; open the PR against <base>. Work only in the
worktree. The issue is already In Progress.
Follow AGENTS.md and packages/<name>/AGENTS.md. Read the issue and only: <files>. No exploratory reads.
`make test-fast` while iterating; `make check` once before the PR. Commit and push per coherent step
(conventional commit with package scope, trailer `Refs: <MVL-N>`).
Open the PR with the template; wait for CI green; move <MVL-N> to In Review. Never merge, approve or
enable auto-merge. Stop and report after three fix rounds or two hours.
Report, at most 15 lines: PR number · head SHA · files · ADR number taken · actions for the coordinator.
```

Reports are at most 15 lines (programme document §6); this supersedes the 12-line implementer report in
`docs/developer-workflow.md` until that file is aligned.

**Reviewer prompt:**

```text
Review PR #<n> at head <sha> for <MVL-N> in bennytay/neptune, cold. `git worktree add <path> <sha>`.
Read AGENTS.md, packages/<name>/AGENTS.md, the issue, the PR body, the full diff and the ADRs it cites;
nothing else. Run `make check` once. Apply the review checklist in docs/developer-workflow.md and the
non-negotiables; check every acceptance item against the code. Do not edit, push, approve or merge.
Report, at most 15 lines:
Verdict: MERGE | REVISE | REJECT · PR #<n> · head <sha>
Acceptance: k/n met · make check: green | <what failed>
- [blocker|should|nit] path:line — one finding per line
```

## 5. Merge flow

1. Reviewer returns a verdict. The coordinator posts it on the PR as a comment whose first line is
   `Review: MERGE @ <sha>` (or `REVISE` / `REJECT`), followed by the findings.
2. `scripts/factory-merge.sh <pr> <sha>`. It refuses unless the PR is open, not draft, based on `main`,
   conflict-free, at the reviewed head with a matching `Review: MERGE` line, `check` green, and (if it edits an
   `ARCHITECTURE.md`) a filled **Architecture change** section. It then runs
   `gh pr merge --squash --auto --match-head-commit <sha>`. Once the queue is live (MVL-192; until then see
   the REST fallback below), the merge queue rebuilds the PR on top of `main`
   and the PRs ahead of it, runs `check` on `merge_group`, merges, and the script prints the merge SHA.
3. If the queue ejects the PR (conflict or red `check` in the group): merge `origin/main` into the branch,
   push, and get a new verdict for the new head. A conflict-free merge of `main` keeps the review valid; the
   coordinator posts a carried verdict (below). A hand-resolved conflict needs a fresh review.
4. Linear: comment the merge SHA on the issue, set Done if the integration did not, remove the worktree.
5. Stacked PR whose base just merged: `gh api -X PATCH repos/bennytay/neptune/pulls/<n> -f base=main`, then
   step 3.

**Carried verdicts.** A `MERGE` verdict carries to a new head only across commits that change nothing a
reviewer judged. The coordinator posts one line per new head, in one of two forms:

- `Review: MERGE @ <new-sha> (carried from <old-sha>, clean merge of main)` after a conflict-free merge of
  `origin/main` (step 3, hand refresh);
- `Review: MERGE @ <new-sha> (carried from <old-sha>, mechanical: <what>)` after a commit of the § 4
  Mechanical class (rename, generated file such as an ADR index or `contracts/compatibility.md`) on a PR
  that already has a verdict, `<what>` naming it in a few words.

Anything else (a code or doc change a reviewer would read, a resolved conflict) needs a fresh review.
`<old-sha>` is the head the original verdict named, so the chain traces back to a real review.

**Live today: merge without a queue (ADR [0005](adr/0005-merge-without-a-queue.md)).** Merge queues need an
organisation-owned repository, and `bennytay/neptune` stays on a personal account for now. `main` has no
strict up-to-date rule; instead `factory-merge.sh`:

- merges a PR that is behind `main` as it stands when nothing `main` changed since the merge base reaches
  what the PR changed (`scripts/merge_freshness.py`); otherwise it refuses with "needs a refresh" and the
  reason, and the coordinator refreshes (`git merge origin/main`, push, wait for `check`, carried verdict as in
  step 3) and runs it again;
- refuses everything while the latest `check` on `main` is red, except a PR labelled `fix-main`. A coordinator
  that sees this makes fixing `main` its first issue if its project broke it;
- holds a machine-wide lock, so coordinators merge one at a time.

Do not refresh PRs pre-emptively after other merges: refresh only when the script asks.

**Verdict authority.** A `Review:` line counts only if its author is OWNER, MEMBER or COLLABORATOR on the
repository, it is not quoted (`>`), fenced, indented as code or in inline code, and its SHA (7–40 hex
digits) is a prefix of the head. Among those, the latest line for the head wins. `factory-merge.sh`
enforces this (`scripts/factory-merge.jq`).

## 6. Budget: weekly usage check and pause order

The budget is one Claude Max 20× plan shared by every thread. Every Monday the programme coordinator reads
the weekly usage figure (Claude Code `/usage` or the claude.ai usage page) and records one line in the
programme document's audit log:

`<YYYY-Www> · usage <p>% of weekly limit at <day> · live: <projects and caps> · merged last week: <n> PRs · action: <hold | raise <project> | pause <step>>`

It re-checks mid-week (Thursday). Thresholds, by usage at the check: ≤ 40% on Thursday may raise one
project's cap by one; > 70% on Thursday or > 50% on Monday applies the next pause step; > 90% at any check
applies all three. Pause order, one step at a time, never skipping:

1. model-derived work (Memory G4: hypotheses, anomaly scores, consolidators that call a model);
2. connectors (Deploy D2);
3. adapters (compiler M3–M6, Deploy lifecycle record adapters).

Contracts and gates are never paused. A pause is a Linear project status update on each affected project
beginning `Paused: <classes> until <date or next check>` (the § 4 block makes coordinators read it), plus the
audit-log line. Lifting it is a status update beginning `Resumed:`.

## 7. Versioning and releases

- **Package versions.** Semver in each package's `[project].version` (the compiler's is the root
  `pyproject.toml`). Every package, the compiler included, is `0.<m>.<p>` until the programme gate: passing
  its gate `<m>` (the milestone number) sets `0.<m>.0`, and a fix released between gates bumps `<p>`. The
  compiler adopts this at its next gate tag (`m3-gate` → `neptune` 0.3.0); its current 0.0.1 is not bumped
  retroactively. Every package becomes `1.0.0` at the programme gate (X4). After that, major = breaking change
  to its public API or a major bump of a contract it owns, minor = feature, patch = fix. Versions change only
  in the gate PR (or a release-fix PR), never in feature PRs.
- **Contract versions** are separate and follow ADR 0002; a package release lists the contract versions it
  owns and locks.
- **Tags** (annotated, on the gate PR's merge commit, never moved or deleted): gate tag
  `<milestone code>-gate` with the lowercase Linear milestone code, which is unique across projects (`m3-gate`,
  `l4-gate`, `g4-gate`, `d1-gate`, `f1-gate`, `x3-gate`), and `programme-gate` for X4. These are the names the
  gate issues already use; the existing `m1-gate` and `m2-gate` follow the same scheme. Release tag
  `<package>-v<version>` (`neptune-v0.3.0`, `neptune-ledger-v0.4.0`).
- **Gate release procedure** (project coordinator, after the gate PR merges at `<sha>`):

  ```bash
  git tag -a <code>-gate <sha> -m "<gate issue title> (<MVL-N>)"
  git tag -a <package>-v<version> <sha> -m "<package> <version>"
  git push origin <code>-gate <package>-v<version>
  gh release create <package>-v<version> --verify-tag --title "<package> <version>" --notes-file <notes.md>
  ```

  Then advance `gate_issue` for the package in `contracts/packages.toml` (it may ride in the gate PR).
- **Release notes from conventional commits.** Squash commits on `main` carry PR titles, so the notes come
  from each merged PR's branch commits: for every squash commit in `<previous release tag>..<sha>` that
  touches the package path (for a package's first release, from the commit that created the package), read
  `gh api repos/bennytay/neptune/pulls/<pr>/commits` and file the PR under its highest conventional type
  (`feat` > `fix` > `perf` > `refactor` > `test` > `docs` > `build`/`ci`/`chore`); any `!` or
  `BREAKING CHANGE:` footer puts it under **Breaking**. Each line is `<PR title> (#<pr>, <MVL-N>)`. A final
  **Contracts** section lists versions published in the range and the package's `contracts/lock.toml`
  entries at the tag. A `scripts/release-notes.py` that does this is a follow-up; until then the
  coordinator assembles the notes with the commands above.
- **Release-fix procedure** (a patch release between gates, project coordinator):
  1. The fix lands as an ordinary issue and PR (`fix(<scope>): ...`), reviewed and merged as usual.
  2. A separate release-fix issue titled `Release <package> 0.<m>.<p+1>` and its PR bump `[project].version`
     from `0.<m>.<p>` to `0.<m>.<p+1>` and nothing else (a release-fix PR is the only non-gate PR that changes
     a version). It is the § 4 Mechanical class: one review and a posted verdict.
  3. After it merges at `<sha>`, tag and release as in the gate procedure, without a gate tag:
     `git tag -a <package>-v0.<m>.<p+1> <sha> -m "<package> 0.<m>.<p+1>"`, push the tag, and
     `gh release create` with notes covering `<package>-v0.<m>.<p>..<sha>`.
  4. Several fixes may ride in one patch release; a fix to an owned contract also follows ADR 0002 (`bump`).
- **Compatibility matrix.** `contracts/compatibility.md` shows each contract's owner, status and latest
  versions, and each consumer's locked version. `scripts/contracts.py matrix` generates it from the registry;
  `make contracts-check` (every CI job) and the compiler's tests fail while it is stale, so a PR that edits
  `contracts/lock.toml` or publishes a contract version regenerates it. The matrix at a release tag is the
  compatibility statement for that release.
