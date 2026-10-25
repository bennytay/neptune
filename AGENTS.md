# AGENTS.md — how to work in this repository

Single source of truth for coding agents (Claude Code, Codex) and humans. `CLAUDE.md` imports this file.
Keep it short; detail lives in `docs/`. Package `AGENTS.md` files predate the reframe: where they ask for
edge-case, security, versioning or ADR work beyond what is below, this file wins.

## What Neptune is

A **live, cited model of a whole site**: every robot, machine, person (by role and shift), task, zone,
document and rule, built from the evidence a site already has (robot logs and bags, fleet-manager and PLC
feeds, CMMS work orders, rosters, manuals, SOPs, whiteboard photos, regulations) and kept current as new
evidence arrives. AI agents query it over MCP and run analysis on top; Neptune does not build the LLM, the
chat UI, dashboards or robot control.

Neptune serves **every type of robot** (arms, mobile bases, legged, humanoids, aerial, marine, vehicles) and
the machines and people around them. Never assume one morphology or a single vehicle.

**This is a demo project.** No customers are onboarded. Build what makes the demo convincing, fast.
Start with `docs/site-model/README.md` for the current milestone (M1, the site model).

## Rules (what the demo must visibly show)

1. **Cited.** Every fact and every answer names the file, row, page or byte it came from.
2. **Labelled.** Each fact is `observed`, `stated` or `inferred`. Inferred facts never pose as stated.
3. **No guessing.** A blank stays a blank; answers say `unknown` or `stale` instead of a value.
4. **Same input, same model.** The model is a pure function of the fact log; one replay test checks it.
5. **Raw sources are read-only.**

## Not worth time (demo rules)

- Hostile-input, malformed, boundary, property or scale tests for new code; security or sandbox work.
- Contract versions, schema bumps, migrations, compatibility shims. v3 replaces v2 at the M1 gate.
- An ADR per decision. The spec decides; write an ADR only for a real fork in the road.
- Existing robustness code stays as it is; don't spend time removing it unless it blocks the demo.

## Repository map

```
src/neptune/            compiler: discovery, adapters (MCAP, ROS bags, PDF, CSV/XLSX, images, URDF, ...),
                        runtime, canonical records, ingest package
packages/neptune-memory memory: v2 claim graph (frozen) and v3 site model in neptune_memory/site/ (M1)
packages/neptune-context query layer and the `neptune` MCP server
packages/neptune-deploy  connectors, lifecycle mappers, evidence packs
packages/neptune-ledger  catalog over ingest packages (off the M1 critical path)
packages/neptune-platform harness, acceptance corpus, demo
harness/  docs/  tests/  contracts/  scripts/
```

## Commands

```
make setup      install everything into .venv (uv) and the pre-commit hooks
make check      lint + types + fast tests, in parallel; what PR CI runs
make check-all  everything, including slow tests; what the nightly runs
make test-fast  fast tests only (PKG=<name> for one package)
make fmt        format and auto-fix
```

## Workflow: one GitHub issue → one branch → one PR

Work is tracked in **GitHub Issues** on `bennytay/neptune`, grouped by milestone (M1 Site model, ...).
Order comes from each issue's "blocked by" links. Status is never set by hand:
open with no linked PR = todo · draft PR = in progress · ready PR = in review · merged = done.

1. Read the issue, `docs/site-model/README.md` and the spec. Nothing else unless the issue names it.
2. Branch from `origin/main`: `gh issue develop N --name N-short-slug --base main`. No stacked branches.
3. Implement. Happy-path tests plus the issue's acceptance tests; gold demo tests flip from xfail as they pass.
4. `make check PKG=<package>` green locally, then push. Open a **draft** PR early; mark it ready when done.
5. PR title = issue title; body starts `Closes #N`, then a one-line **TL;DR**, ≤5 **Decided** bullets and a
   **Progress** line, all within 12 visible lines. State decisions; never hand one back to the maintainer.
   If the PR changes a box, arrow or status in `ARCHITECTURE.md`, update it and fill **Architecture change**.
6. Implementers never merge. The coordinator merges with `scripts/factory-merge.sh <pr> <head-sha>` when CI is
   green and, for core PRs (fact log, live updates), after one quick review (`Review: MERGE @ <sha>`).

The coordinator runs at most 3 implementers at once, one worktree each, and picks the lowest-milestone open
issue that is not blocked.

## Git

- `main` is protected: PR required, CI green, linear history, no force-push. Never commit to `main`.
- Conventional commits with package scope (`feat(memory): fact log`), trailer `Refs: #N`.
- Refresh with `git merge origin/main` only when `scripts/factory-merge.sh` says so; never rebase a pushed branch.
- Never commit secrets, fixtures > 512 KB, or ingest output.

## Docs

- Docs describe the system as it is. `docs/site-model/spec.md` is the design for M1.
- `ARCHITECTURE.md` is one evolving Mermaid diagram and nothing else, in the soft palette.
- Tests have no `__init__.py`: test and fixture basenames must be unique across the repository.
- Golden files are fine to regenerate; say so in the PR.
