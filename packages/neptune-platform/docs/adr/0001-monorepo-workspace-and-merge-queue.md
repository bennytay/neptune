# 0001 — One uv workspace, path-selected CI jobs behind one `check`, and a merge queue on main

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-122

## Context

Seven projects (the compiler and the layers above it) live in one repository and several coordinators
merge into `main` at once. Today every PR runs the whole compiler suite, `main` requires branches to be
up to date, and the coordinator refreshes each PR by hand before `scripts/factory-merge.sh` will merge
it. With N PRs in flight that is O(N²) CI runs and constant refresh churn. Shared hand-edited files (the
root ADR index, `ci.yml`, `ARCHITECTURE.md`) conflict between parallel PRs.

## Decision

1. **Workspace.** The root `pyproject.toml` is a uv workspace: the compiler (`neptune`) is the root
   project; every `packages/<name>/` is a member (`members = ["packages/*"]`); `packages/_template` is
   excluded. `make setup` is `uv sync --all-packages --all-groups`. Dev tools stay in the root `dev`
   group; every tool runs as `uv run --all-packages --all-groups` from the package's own directory so
   its own `[tool.ruff]` (extending the root), `[tool.mypy]` and `[tool.pytest]` apply.
2. **Make.** `make check` runs lint + ADR-index check + mypy + pytest for the compiler and every member;
   `make check PKG=<name>` runs one (`PKG=neptune` is the compiler; an unknown name is an error). The
   compiler's lint excludes `packages/*`; each member lints itself.
3. **Template.** `packages/_template/` holds `pyproject.toml`, `src/__PKG_MODULE__/`, `tests/`,
   `docs/adr/0000-template.md` + generated index, `docs/contracts.md`, an `ARCHITECTURE.md` seed and an
   `AGENTS.md` with the shared non-negotiables and a marked layer-specific section.
   `scripts/new-package.sh <name>` copies it with `__PKG_NAME__` / `__PKG_MODULE__` substituted and
   runs `uv lock`; the `packages/*` glob is the registration. Names are lowercase-hyphenated; the import
   package swaps hyphens for underscores. This package (`neptune-platform`) is the first member.
4. **CI.** `.github/workflows/ci.yml` has a `plan` job (`.github/scripts/ci_plan.py`, stdlib only) that
   selects jobs from `git diff base...head` on `pull_request`; `merge_group` and `push` run everything.
   - root plumbing (`pyproject.toml`, `uv.lock`, `Makefile`, `.python-version`, `.github/**`): all jobs;
   - compiler job `neptune`: any path outside `packages/` and `contracts/` (so root `docs/`, `scripts/`
     count; this is wider than `src/neptune/**` + `tests/**` because compiler tests read `docs/`);
   - member job `<name>` (a matrix over discovered members, so adding a package edits no workflow):
     `packages/<name>/**`, `contracts/**`, or a run of any workspace project it lists in
     `[project].dependencies` (transitively; depending on `neptune` follows the compiler);
   - `template` job: `packages/_template/**` or `scripts/new-package.sh`; it generates a throwaway
     package and runs `make check` on it.
   The `check` job needs all of them, runs `if: always()`, and fails only on a `failure` or `cancelled`
   result; skipped jobs count as success. `check` remains the single required status.
5. **Merge queue as code.** `.github/rulesets/main.json` targets the default branch: no deletion, no
   force-push, linear history, PRs required with squash only, required status `check` from GitHub
   Actions (integration 15368) without the strict up-to-date policy, and a squash merge queue
   (ALLGREEN, up to 5 built and merged together, 60-minute check timeout). The queue tests each PR on
   top of `main` plus the PRs ahead of it, which replaces the up-to-date rule. Implementers never
   change repository settings; the coordinator applies it after this ADR's PR merges:

   ```sh
   gh api --method POST repos/bennytay/neptune/rulesets --input .github/rulesets/main.json
   gh api --method PATCH repos/bennytay/neptune -F allow_auto_merge=true \
     -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY
   # once one PR has merged through the queue, retire the classic protection (strict up-to-date):
   gh api --method DELETE repos/bennytay/neptune/branches/main/protection
   # later edits: gh api --method PUT repos/bennytay/neptune/rulesets/<id> --input .github/rulesets/main.json
   ```

   GitHub offers merge queues only on organisation-owned repositories. If the POST rejects the
   `merge_queue` rule because `bennytay/neptune` is user-owned, the repository moves to a free
   organisation (public repos keep merge queues free there) and the commands above run against the new
   owner; until then the classic protection stays and the fallback in §6 keeps PRs merging.
6. **`scripts/factory-merge.sh`.** Keeps every refusal (open, not draft, base `main`, reviewed head,
   `check` green) except being up to date; it refuses only on conflicts (`dirty`). It additionally
   requires the latest `Review: <VERDICT> @ <sha-prefix>` line (PR comment or review body) whose SHA
   prefixes the head to say `MERGE`, and, when the PR edits any `ARCHITECTURE.md`, a filled (uncommented)
   **Architecture change** section in the body. It then runs `gh pr merge --squash --auto
   --match-head-commit <head>` with the PR title `(#N)` and body as the commit message, waits for the
   merge and prints its SHA (`WAIT=0` returns once queued; `MERGE_TIMEOUT` bounds the wait). If GitHub
   rejects auto-merge (no queue yet, auto-merge disabled, or the PR is already mergeable), it falls back
   to the previous REST squash merge pinned to the head, which still needs `mergeable_state` `clean`.
   `DRY_RUN=1` runs every check and merges nothing.
7. **Generated ADR indexes.** Each member's `docs/adr/README.md` is generated by `make adr-index`
   (`.github/scripts/adr_index.py`) from the ADRs' headings and `Status:` lines; `make check` fails on
   a stale index. ADR numbers are local to each package, so parallel PRs in different packages never
   collide; within one package a conflict is resolved by regenerating. The root `docs/adr/README.md`
   stays as MVL-84 defines it.

## Alternatives considered

- **`dorny/paths-filter` with one hand-written job per package.** Every new package would edit
  `ci.yml`, recreating the hotspot this removes, and adds a third-party action to the trust boundary.
- **Workflow-level `paths:` filters.** A workflow skipped by `paths` never reports its status, so a
  required status would stay pending forever; an always-running aggregator is the documented fix.
- **Keep the up-to-date rule and auto-update branches.** Still one CI run per PR per merge to `main`
  and still serial; the queue batches and needs no pushes to PR branches.
- **Separate repositories per layer.** Loses atomic cross-layer changes and the shared contracts
  directory; the programme plan chose one monorepo.
- **Members carrying their own dev tools.** Version drift between packages; one root `dev` group keeps
  ruff and mypy identical everywhere.

## Consequences

- A PR touching only `packages/<name>/` runs that member's job (and its dependents'), not the compiler
  suite; a merge-queue group runs everything once for the batch.
- Two PRs in different packages no longer touch a common file; adding a package edits only
  `packages/<name>/` and `uv.lock` (regenerated by `uv lock` after merging `main`).
- Every member must keep a working `make check PKG=<name>`; the template job guards the template.
- `factory-merge.sh` now depends on reviewers posting `Review: MERGE @ <sha>`; a verdict for an older
  head does not count.
- Revisit if the queue's 5-entry batches become the bottleneck, if a member needs a tool version the
  root `dev` group cannot provide, or if `ci_plan.py`'s dependency rule misses a real coupling (for
  example a member reading compiler data without declaring the dependency).
