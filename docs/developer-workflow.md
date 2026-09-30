# Developer workflow

Status: agreed 2026-09-30. `AGENTS.md` carries the short form; this is the reference.

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

- Start only issues whose `blockedBy` are all Done. Check for issues already In Progress first. The selection
  rule for "implement the next MVL issue" is in `AGENTS.md` under *Picking the next issue*.
- Branch name = the issue's `gitBranchName` (`benjamintay07/mvl-N-slug`). Linear's GitHub integration links
  PRs by this name and transitions status on merge; enable it once in Linear → Settings → Integrations.
- Comment on the issue when moving to In Review: PR link + acceptance checklist. On Done: merge SHA.

## Branching and merging

- Trunk-based. `main` is protected: PR required, CI (`check`) green, linear history, no force-push, no deletion.
- One branch per issue; rebase on `main` before opening the PR. PR title = issue title.
- **Only the maintainer merges** (squash-merge). Agents open the PR, move the issue to In Review, and stop —
  no merging, approving, or auto-merge, even on green CI.
- Tags: `m1-gate`, `m2-gate`, … at review gates; semver `v0.x.y` from M3 onward.

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

Use the template. The maintainer reads the visible part in about 30 seconds and skips the rest, so write
bottom line up front (BLUF):

| Section | Limit | Contents |
|---|---|---|
| `Closes MVL-N` | 1 line | links the issue |
| **TL;DR** | 1 line | what now exists that didn't before |
| **Decided** | ≤5 bullets, ≤15 words each | decisions and trade-offs, not files |
| **Your call** | ≤3 bullets | where the maintainer's judgment is needed; "none" if none |
| **Progress** | 1 line | milestone count + next issue, e.g. `M1 3/9 · next: MVL-40` |
| Details (collapsed) | — | acceptance checklist, `make check` result, docs/ADRs touched, follow-ups |

No paragraphs in the visible part. Design depth belongs in ADRs and docs, linked rather than pasted. An
unticked acceptance box means the PR stays a draft. Linear comments on status changes follow the same shape.

**Architecture change.** After each PR, ask whether it changes `ARCHITECTURE.md`: a box, an arrow, which box
owns a responsibility, or a box's built/partial/not-built state. If it does, update the file in the same PR and
add the template's *Architecture change* section. Phrase it conceptually ("added this box", "moved
responsibility X → Y"), with a tiny before → after Mermaid diagram if that helps. If it does not, touch neither.

## Review checklist (self-review before requesting review)

- Non-negotiables in `AGENTS.md` hold (provenance, explicit unknowns, no silent conversions, determinism).
- Parsing separated from normalisation; no inferred values in `model/`.
- Malformed-input, boundary and determinism tests present. Fixtures real, small, committed.
- Docs updated where a contract changed; ADR written where a decision was made.
- No changes to `model/` after the M1 gate without an ADR.

## Parallel agents

The Linear dependency graph bounds parallelism. M1 is effectively serial (contracts compose); M2 admits 2–3
concurrent issues; M3–M6 admit ~15 independent issues (adapters are leaf packages).

Mechanics, host-agnostic (Zed threads, Claude Code, Codex):

1. One git worktree per agent: `git worktree add ../neptune-mvl-17 -b <gitBranchName>`.
2. One issue per agent. Prompt: "Implement MVL-N. Follow AGENTS.md." Everything else is in the repo.
3. Agents never touch `main`, never edit `AGENTS.md`/`architecture.md`/ADRs in feature PRs.
4. A coordinator picks unblocked issues and assigns them; the maintainer reviews and merges in dependency order.
5. Practical ceiling is review bandwidth: ~4 concurrent agents per reviewer.

## Gates

At `MVL-56` (M1) and `MVL-57` (M2) feature work stops; the gate issue's stress test is run on paper against
the current contract, findings are recorded in `docs/reviews/`, foundational problems are fixed, and `main`
is tagged. No M(n+1) issue starts before the M(n) gate is Done.
