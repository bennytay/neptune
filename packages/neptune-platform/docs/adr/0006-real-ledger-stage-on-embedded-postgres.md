# 0006 — The real Ledger stage runs on an embedded PostgreSQL, not the compose stack

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-125
- Amends: [0004](0004-integration-harness.md) §3 (the ledger stage is real) and §6 (a real stage that needs
  PostgreSQL need not need the compose stack)

## Context

The X1 gate needs the Ledger's catalog-api checked against the real registry, not its goldens. The Ledger's
catalog (`PostgresCatalog`, MVL-90) needs PostgreSQL 16 with a C-collated database and nothing else: no AGE,
no pgvector, no MinIO. ADR 0004 §6 assumed a real stage that needs PostgreSQL would use
`harness/compose.yaml`, which means Docker in CI and a several-minute image build (AGE from source) for a
stage that uses none of that image's extensions. The Ledger's own contract tests already run the catalog on
the `pgserver` wheel (PostgreSQL 16 binaries in a wheel; a dev dependency of `neptune-ledger`), and
`make harness` installs every member's dev group.

## Decision

1. The ledger stage's real driver (`harness/stages.py` `ledger_real`) starts an embedded PostgreSQL 16 from
   `pgserver` with its data directory in the run's scratch (`work/ledger-pgdata`), creates one C-collated
   database, applies the Ledger's migrations for the tenant `harness`, and deletes the server on exit.
   The stage declares `needs_services=False`; CI stays Docker-free.
2. For every package the compiler stage committed, in case order, it calls the real catalog:
   `register` (must be `registered`, with the compiler's package id), `register` again (must be
   `already_registered`) and `verify` (must be `intact`). Every response is validated against the
   registry's latest stable catalog-api schema at the pointer of its golden (`Registration`,
   `VerifyReport`).
3. The lock is honoured at run time as well as in `resolve`: each package's manifest `schema_version` (the
   lowest package-schema version whose readers read it, compiler ADR 0037) must be at most the major
   `neptune-ledger` locks for package-schema in `contracts/lock.toml`.
4. The report keeps the catalog's answers that are deterministic (outcome, finding codes, `tx_seq`,
   schema version, record and file counts, verdict, schema validity) and drops `tx_time` (a clock) and
   `root_locator` (an absolute path), so the report stays byte-identical across runs and checkouts.

## Alternatives considered

- **The compose stack's PostgreSQL.** Lost: Docker plus an AGE build in CI for a stage that needs plain
  PostgreSQL. It returns when a real stage needs AGE, pgvector or MinIO (Memory, Context).
- **Use the compose stack when `HARNESS_POSTGRES` answers, else `pgserver`.** Lost: the same commit would
  run against two servers depending on the host, and the report could not say which without a new key.
- **Leave the ledger stage a stub and rely on `check --all`.** Lost: the owner's contract tests prove the
  catalog against its own fixtures, not that it accepts what the compiler emits today. The gate exists to
  prove that seam.

## Consequences

- The harness depends on `neptune-ledger`'s dev group being installed (`make setup` does it). Without
  `pgserver` the stage is an `error` that names the missing module.
- A run starts and stops one PostgreSQL (a few seconds); the scratch directory holds its data until the
  stage ends.
- Memory and Context, when real, still follow ADR 0004 §6 (compose stack, `needs_services=True`). Revisit this
  ADR if the Ledger starts to need an extension `pgserver` lacks, or a second real stage needs the same
  database.
