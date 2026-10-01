# 0003 — Multi-coordinator playbook and release/versioning policy

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-124

## Context

The programme document (§6, §7b) sets the rules for running seven Linear projects in parallel in one
repository: one Opus coordinator per live project, a Fable programme coordinator, Linear as the only channel
between them, a shared Claude Max budget, and model tiers by kind of work. Those rules are prose. A fresh
coordinator needs them as a procedure: which prompt to paste, which block each package's `AGENTS.md`
carries, how a contract change is requested across projects, how merges go through the queue (ADR 0001)
while the queue may not yet be live, and how packages are versioned, tagged and released. This ADR fixes
those choices; `packages/neptune-platform/docs/playbook.md` is the operating procedure.

## Decision

1. **One playbook.** `packages/neptune-platform/docs/playbook.md` is the single operating procedure. It
   quotes the coordinator prompt from programme document §6 verbatim; only the three placeholders are
   filled. A change to the prompt is a change to the programme document first, then the playbook.
2. **Policy reaches coordinators through package `AGENTS.md`.** The prompt tells each coordinator to read
   `packages/<name>/AGENTS.md`, so the per-path model map, the token rules and the budget-pause check live
   in one marked block (`BEGIN/END model-policy`) whose source is playbook § 4. Packages copy it verbatim;
   copies are never edited in place.
3. **Model map by path and kind of work.** Opus for ADRs, `contracts/`, gates, and code under `store/`,
   `schema/`, `consolidate/`, `query/`, `runtime/`, `model/` or any module exporting a contract; Sonnet for
   adapters, connectors, fixtures, exporters, docs, scaffolds, UI and mechanical work. Mixed issues run at
   the higher tier. Opus-tier work may get two full reviews; Sonnet-tier work gets one review, with REVISE
   blockers re-checked by the same reviewer. A mechanical commit on a PR that already has a reviewer is
   covered by that review; a standalone mechanical PR gets a Sonnet reviewer and a posted verdict, as root
   `AGENTS.md` step 9 and `factory-merge.sh` require. The block also points coordinators to playbook § 2
   and § 5.
4. **Linear as the bus.** Coordinators report up only at a gate or when blocked by another project for more
   than an hour, as one comment on the programme document. The cross-project gate edges of programme document §6
   belong to the programme coordinator; the only cross-project edge a project coordinator adds is a
   contract request's `blocks` edge; a cross-project blocker counts only when Done (no stacking on another project's
   PR). A contract change request is an issue on the owning project titled `Contract <id>: <change>` with a
   `blocks` edge to the consumer's issue; the owner's bump announces on consumers' gate issues (ADR 0002 §7);
   each consumer raises its own lock entry.
5. **Budget.** The programme coordinator records a weekly usage line in the programme document's audit log
   (Monday, re-checked Thursday) with fixed thresholds. Pauses go one step at a time: model-derived work,
   then connectors, then adapters; never contracts or gates. A pause is a Linear project status update
   beginning `Paused:`, which the § 4 block makes coordinators read before each selection.
6. **Merge flow.** Verdict comment `Review: MERGE @ <sha>`, then `scripts/factory-merge.sh <pr> <sha>`,
   which enqueues with `gh pr merge --squash --auto`. A clean merge of `main` into a reviewed branch carries
   the verdict to the new head (`Review: MERGE @ <new> (carried from <old>, …)`); a hand-resolved conflict
   needs a fresh review. Applying the ruleset of ADR 0001 §5 failed (`merge_queue` needs an
   organisation-owned repository; MVL-192 moves it). Until then, the script's REST fallback merges and
   coordinators hand-refresh as in `docs/developer-workflow.md`. Once the queue rule is live, the REST
   fallback is rejected by design (the ruleset has no bypass actors). A verdict counts only from an
   OWNER, MEMBER or COLLABORATOR author, and the latest one wins (MVL-193 enforces this in the script).
7. **Package versions.** Semver in each package's `[project].version`. Every package, the compiler
   included, is `0.<m>.<p>` until the programme gate, where `<m>` is the number of the last gate it passed.
   The compiler adopts this at `m3-gate` (`neptune` 0.3.0); its 0.0.1 is not bumped retroactively. Every
   package becomes `1.0.0` at the programme gate (X4); after that, standard semver, with a major bump of an
   owned contract counting as breaking. Versions change only in gate or release-fix PRs, never in feature
   PRs. Contract versions stay independent (ADR 0002).
8. **Tags.** Annotated, on the gate PR's merge commit, never moved. The gate tag is
   `<lowercase milestone code>-gate` (`m3-gate`, `l4-gate`, `g4-gate`, `x3-gate`). Milestone codes are
   unique across projects, and these are the names the gate issues in Linear already use, so the existing
   `m1-gate` and `m2-gate` fit and nothing is renamed. X4 is tagged `programme-gate`. The release tag is
   `<package>-v<version>`.
9. **Release notes from conventional commits.** Because squash commits on `main` carry PR titles, notes are
   built from each merged PR's branch commits (via the GitHub API), grouped by the highest conventional type
   with breaking changes first, plus a Contracts section. A package's first notes run from the commit that
   created the package. A script is a follow-up.
10. **Compatibility matrix.** `contracts/compatibility.md` lists each contract's owner, status and latest
    versions and each consumer's lock, hand-maintained in any PR that edits `contracts/lock.toml` or
    publishes a version, until `scripts/contracts.py matrix` generates and checks it (follow-up).

## Alternatives considered

- **Policy in the coordinator prompt.** Lost: the prompt is fixed by the programme document and kept
  short; a long prompt is re-sent with every resume, and changes would not reach live coordinators.
- **Policy only in the playbook, linked from `AGENTS.md`.** Lost: a linked file is a file coordinators are
  told not to read; a copied block costs a few lines per package and is always in context.
- **Coordinators messaging each other (shared thread or files).** Lost: unauditable and invisible to a
  resumed session; Linear already holds the dependency graph.
- **Stacking on another project's open PR.** Lost: the other coordinator controls that branch's refreshes and
  REVISE rounds; a cross-project stack couples two loops for a saving of hours.
- **Layer-prefixed gate tags (`ledger-l2-gate`).** Lost: milestone codes are already unique, the gate
  issues in Linear already name the unprefixed tags, and the compiler's `m1-gate`/`m2-gate` exist; a prefix
  would split the scheme or force renames.
- **Release notes from `main`'s commit titles.** Lost: those are PR titles, not conventional commits, so
  types and breaking markers would be lost.
- **One repository-wide version.** Lost: layers ship at different gates; a single number would claim
  compatibility that only the contracts registry can state.
- **Generate the compatibility matrix now.** Lost: `scripts/contracts.py` is outside this issue's paths; the
  matrix is small and its source (`lock.toml`, `version.json`) is already canonical.

## Consequences

- Each package scaffold PR pastes the § 4 block into its `AGENTS.md`; the Platform template should carry it
  so `scripts/new-package.sh` does it automatically (follow-up on `packages/_template/AGENTS.md`).
- While the merge queue is not live, cross-project merges serialise on the up-to-date rule; the playbook
  holds live coordinators at three until it is.
- Until the release-notes script and `contracts.py matrix` exist, gate releases and the matrix are manual and
  can drift; the reviewer of any PR touching `contracts/lock.toml` checks the matrix.
- Root `docs/developer-workflow.md` still describes a single coordinator, hand refresh and a 12-line
  implementer report; the playbook's 15-line limit (programme document §6) supersedes it. Aligning that
  file and pointing it to the playbook is a dedicated PR, outside this issue's paths.
- Revisit when the budget plan changes, when a fourth or later coordinator is routinely live, or when the
  queue is live and the fallback section can be removed.
