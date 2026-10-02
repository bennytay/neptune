# 0004 — Integration harness: real-or-stub stages, one deterministic report, no Docker until needed

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-123

## Context

Six layers are built in parallel and meet only at the contracts of ADR 0002. `check --all` proves the
registry is well formed and that each owner's own tests pass, but nothing runs a corpus through the layers
in order, so a seam that every contract test accepts can still be broken end to end. The layers arrive one
at a time: today only the compiler exists, so a harness that needs all of them is useless until the last one
lands. It must also be safe to run on a pull request, since it executes that pull request's code.

## Decision

1. **Where.** `harness/` at the repository root, owned by Platform. `uv run python -m harness` (also
   `make harness`) runs it. Its unit tests live in `packages/neptune-platform/tests/` (`test_harness_*`),
   because only a workspace member has a test job; the member's pytest and mypy add the repository root to
   their path to import it.
2. **Run.** In order: `scripts/contracts.py check --all` (the registry and every owner's contract tests);
   the stages compiler, ledger, memory, context, each fed the previous one's output; one smoke query
   against the context stage; the report. Exit is non-zero when the contracts check fails, any stage is
   not `ok`, or the smoke query returns no packet. The report is written either way. A stage error is a
   finding in the report, never a crash, and a stage after a failed one is `skipped`.
3. **Real or stub, per stage.** A stage runs the real package only when its contract entry point
   (`contract.toml` `[owner].module`) is importable, its version constant matches the registry's latest
   version of the contract it owns (an integer constant is the registry major, a string constant equals the
   version), every contract the package locks is within its latest major, and the harness has a real driver
   for it. Otherwise it runs a contract stub, which never runs package code: it serves the registry's goldens
   for that contract (`contracts/<id>/v*/golden`), or a canned marker when the contract has no published
   version. The report states `real` or `stub` and the reason for each stage. Today the compiler is real
   and ledger (catalog-api 0.0.0 goldens), memory (graph-schema: canned) and context (query-packet: canned)
   are stubs.
4. **Smoke query.** The context stage's output carries `smoke: {query, packet_source, packet}`. The stub
   serves a golden packet when query-packet has a published version, and otherwise a minimal canned packet
   that names the packages that flowed in. The report says which (`packet_source`).
5. **Corpus.** The compiler's four worked examples (`tests/fixtures/model/*/sources`: drone, manipulator,
   mobile robot, quadruped) until the Deploy D1 archetype fixtures exist. `harness/corpus.py` has the one
   hook (`ARCHETYPES`); `--corpus <dir>` runs any folder of case folders. No fixture is added.
6. **Services.** `harness/compose.yaml` holds PostgreSQL 16 with Apache AGE and pgvector (built from
   `harness/postgres/Dockerfile` on a digest-pinned `apache/age` base) and MinIO (exact release tag), bound to
   loopback on non-default ports. Each stage declares `needs_services`; it matters only when that stage runs
   for real, and then a missing stack is a stage error that prints the start command (`--compose` starts and
   stops it). Nothing starts Docker by itself, so CI needs none today. A pull request that makes a
   `needs_services` stage real must add the compose step to the workflow; a unit test fails until it does.
7. **Report.** `<run dir>/report.json` (default `harness/.run/`, git-ignored) and a Markdown form: sorted keys,
   no wall-clock, host or absolute path, so two runs of one checkout are byte-equal. Only the tool's own
   output lines are kept from `check --all`, never pytest timings. The run's scratch directory (`work/`) is
   cleared at the start of each run; nothing else under the run dir is touched.
8. **CI.** `.github/workflows/harness.yml` runs on `merge_group` and a nightly schedule (always: a merge
   group has no path filter), on `workflow_dispatch`, and on `pull_request` for `contracts/**`, `harness/**`,
   `scripts/contracts.py`, the compiler's `src/neptune/model/**` and each member's `api`, `schema`,
   `contract` and `manifest` modules (a unit test checks that every contract's exported modules are
   covered). It is not a required status and `ci.yml`'s `check` does not wait for it.
   - The job that runs the harness has a read-only token. The PR comment is a separate job with
     `pull-requests: write` that checks out nothing and posts the finished report, for same-repository pull
     requests only. `pull_request_target` is never used.
   - Linear: on schedule and dispatch only, when `LINEAR_API_KEY` is set, `python -m harness.publish`
     comments on the current gate issue (`contracts/packages.toml`) of every package that owns a stage and of
     `neptune-platform`, through `post_comment` in `scripts/contracts.py`. The nightly run posts only when red
     (`--only-failed`); a manual run always posts.

## Alternatives considered

- **Compose-only: run every stage in containers.** Lost: nothing needs a service today, and requiring
  Docker in the merge queue for four in-process stages adds minutes and a failure mode for no coverage.
- **Skip a stage until its package exists.** Lost: the downstream stages and the smoke query would have
  nothing to run on. A golden stub keeps every seam exercised against the registry's own examples.
- **Stub a stage as soon as any piece is missing, without a version check.** Lost: a real package built
  against an older contract would be trusted silently. The version and lock checks make a stale package fall
  back to its stub, with the reason on record.
- **Post a green nightly result on every gate.** Lost: seven comments a day that say nothing. A red result
  is the signal; a person can dispatch the run when they want a green record.
- **A `harness` workspace member with its own test job.** Lost: one more package for a script and its tests,
  and a second place to add contract paths. Platform already owns `contracts/` and `harness/`.
- **Make it a required status now.** Lost: the stubs make a green run mean less than it will; it becomes
  required when ledger and memory are real (revisit then).

## Consequences

- Every gate issue after this one references the harness: "harness green at <sha>" is part of the gate.
- Swapping a stub for a real stage is one driver in `harness/stages.py` plus, if it needs services, a compose
  step in the workflow. `docs/harness.md` is the runbook.
- The platform member's `pyproject.toml` adds the repository root to pytest's and mypy's path. `ci.yml`'s plan
  maps a change under `harness/` only to the compiler job (it is outside `packages/` and `contracts/`), so the
  harness workflow also runs `make check PKG=neptune-platform` (the harness's unit tests) before the harness.
- The MinIO tag could not be checked against the registry from the authoring environment and Docker is not
  run in CI; the first run of `docker compose up` is the check. Revisit this ADR when a stage first needs
  services, when the harness becomes required, or when the Deploy archetype fixtures land.
