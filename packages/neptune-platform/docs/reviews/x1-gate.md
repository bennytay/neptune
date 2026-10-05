# X1 gate: first cross-repo contract test green in the harness

- Date: 2026-10-05 · Issue: MVL-125 · Reviewed: `harness/` (ADR 0004), `scripts/contracts.py check --all`,
  `contracts/lock.toml`, the package-schema and catalog-api contracts, and the Ledger's `PostgresCatalog`
  (MVL-90).
- Method: `make harness` on the branch, and on a throwaway local branch (never pushed) that breaks the
  compiler's package schema. Both runs are recorded below; the break is also an automated test.
- Outcome: the compiler's packages now flow into the **real** Ledger catalog. Every catalog-api response is
  checked against the registry's schema, and the Ledger's package-schema lock is checked against every
  package. A deliberate schema break turns the harness red in two independent places. One defect was fixed:
  the package-schema owner tests did not cover the manifest.
  **Verdict: pass.** X2 may start once this is merged and `main` is tagged `x1-gate` (the coordinator tags).
- Harness: **green at `6af707c`** (and at `f8b16e8`, before the fixes), full run with owner tests:
  `harness green | contracts ok | compiler: real ok | ledger: real ok | memory: stub ok | context: stub ok`.

## Per stage

| Stage | Mode | Contract | Reason (from the report) | What ran |
|---|---|---|---|---|
| contracts | real | all | `scripts/contracts.py check --all` exit 0 | Registry and goldens of every version. Owner tests of package-schema (compiler), catalog-api (Ledger: `TestStubCatalog` against the stub and `TestPostgresCatalog` against the real catalog) and graph-schema (Memory). Locks: `neptune-ledger: package-schema 6.0.0 is current`, `neptune-deploy: package-schema 6.0.0 is current` |
| compiler | **real** | package-schema 6.0.0 | `neptune.model is importable and matches 6.0.0` | Ingests the 4 worked examples (drone, manipulator, mobile robot, quadruped). Each package is committed and verified, and its manifest and receipt validate against the registry's 6.0.0 schema |
| ledger | **real** (was stub) | catalog-api 1.6.0 | `neptune_ledger.api is importable and matches 1.6.0` | `PostgresCatalog` on an embedded PostgreSQL 16 ([ADR 0006](../adr/0006-real-ledger-stage-on-embedded-postgres.md)). For each of the 4 packages: `registered` (tx_seq 1 to 4), `already_registered` on the second call, then `verify` returns `intact` (38, 45, 42 and 44 files). All 12 responses validate against catalog-api 1.6.0. Every package needs package-schema 1 or 2, within the lock (6.0.0) |
| memory | stub | graph-schema 1.0.0 | `the harness has no real driver for neptune-memory yet` | Serves the 6 graph-schema goldens |
| context | stub | query-packet (none) | `neptune_context.contract is not importable` | A canned packet that names the 4 packages; smoke ok |

The issue asked for the compiler's package-schema tests against a Ledger stub. Both halves now run:
`check --all` runs the catalog contract suite against `StubCatalog` and against the real catalog. The harness
stage goes further and puts the compiler's real packages through the real catalog. The stub stays the
fallback whenever `resolve` refuses the real stage: an import failure, a version mismatch or a stale lock
(see `test_harness_stages.py`).

## Contracts lock honoured

- `resolve` refuses the real stage when a package locks a contract a major behind:
  `test_a_lock_a_major_behind_makes_the_stage_a_stub`.
- New: the ledger stage also checks each registered package at run time. Its manifest's `schema_version`
  must be at most the major `neptune-ledger` locks for package-schema. Lowering the lock to 1.0.0 fails the
  manipulator and quadruped, which need version 2 (`test_a_package_newer_than_the_ledger_lock_fails_the_real_ledger`).
- `check --all` warns that two consumers lag by a minor: `neptune-deploy` locks catalog-api 1.4.0 and
  `neptune-memory` locks 1.1.0, against 1.6.0. Warnings, as ADR 0002 allows; each package's coordinator picks
  the bump up.

## The schema break

On a local branch `x1-schema-break-throwaway` (never pushed), `src/neptune/model/package.py` renames the
manifest key `tables` to `table_counts` in both `PackageManifest.to_json` and its reader. This is a compiler
that changes its package format without a contract bump. Command:

```bash
git worktree add .claude/worktrees/x1-break -b x1-schema-break-throwaway f8b16e8
# edit src/neptune/model/package.py: "tables" -> "table_counts" (writer and reader); commit locally
make setup && make harness HARNESS_RUN_DIR=$TMPDIR/red
```

First run, before the fix below (exit 2):

```
harness RED | contracts FAILED | compiler: real failed | ledger: real skipped | memory: stub skipped | context: stub skipped
contracts.problems: catalog-api: owner neptune-ledger's contract tests failed
contracts.notes:    package-schema: owner neptune's contract tests passed      <- the defect
compiler.problems:  drone: manifest.json breaks package-schema: <root>: Additional properties are not allowed ('table_counts' was unexpected)
                    drone: manifest.json breaks package-schema: <root>: 'tables' is a required property
                    (the same for manipulator, mobile_robot and quadruped)
```

After the fix (exit 2):

```
harness RED | contracts FAILED | compiler: real failed | ledger: real skipped | memory: stub skipped | context: stub skipped
contracts.problems: catalog-api: owner neptune-ledger's contract tests failed
                    package-schema: owner neptune's contract tests failed     <- now caught by its owner
compiler.problems:  (as above)
```

The automated form is `test_a_compiler_that_breaks_package_schema_turns_the_harness_red`
(`test_harness_run.py`). It patches `PackageManifest.to_json` in process with the same rename. The harness
exits 1, the compiler stage fails and every later stage is skipped.

## Findings

### Defects found and fixed here

- **X1-1. The package-schema owner tests did not cover the manifest or the receipt.** The contract declared
  `tests/unit/model/test_schema.py` and `tests/integration/test_worked_examples.py`. Both cover record lines
  only, so `check --all` (the harness and every consumer's `contracts.py check`) reported the compiler's
  contract tests as passed for a package the registry's schema rejects. The compiler's own `make check` did
  catch it (`tests/unit/store`). `contracts/package-schema/contract.toml` now also declares
  `tests/unit/store/test_ingest_package.py`, whose
  `test_the_package_documents_validate_against_the_schema` validates both documents. It adds about 1 s to
  the owner run.
- **X1-2. The ledger stage was a stub** ("the harness has no real driver for neptune-ledger yet"), although
  catalog-api 1.6.0 and `PostgresCatalog` were both on `main`. It is now real (ADR 0006).
- **X1-3. Stale docstrings.** The ledger and memory stubs said they served catalog-api 0.0.0 and that
  graph-schema had no version. Both serve their contract's latest goldens.

### Observations (no change)

- `check --all` does not run the owner rule (`check-owner`: exported schema equals the registry). That rule
  runs in each package's `make check` (`contracts-check`), which CI requires, so the harness does not repeat
  it. A broken export is still caught in the harness by the owner tests and by the compiler stage's
  validation.
- A full `make harness` takes about 8 minutes. Almost all of it is the owners' contract tests; the stages take
  about 1 minute. `--no-owner-tests` is the fast loop.

### Follow-ups for the coordinator

- **Package-schema 7.0.0 (PR #92).** It already raises `neptune-ledger`'s lock to 7.0.0, which keeps the
  ledger stage real. It must also move the compiler stage's pinned `"6.0.0"` in
  `test_harness_stages.py` (`test_today_the_compiler_and_the_ledger_resolve_to_real`, which predates this
  gate). The new ledger tests read the lock from the registry, so they do not pin a version.
- Make the harness workflow a required status once Memory is real (ADR 0004, "Make it a required status
  now", deferred).
