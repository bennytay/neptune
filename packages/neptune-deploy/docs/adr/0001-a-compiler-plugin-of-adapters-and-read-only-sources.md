# 0001 — Deploy is a compiler plugin of adapters and read-only Sources, pinned to package schema 3

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-112

## Context

Neptune Deploy (P-MVL-15) turns deployment lifecycle evidence into canonical records: commissioning sheets,
authorisation envelopes, intervention logs, CMMS work orders, incident tickets, change records and risk
registers. The same records cover an arm in a fenced cell and an AMR fleet in a warehouse. The compiler
already owns the parts that make ingestion trustworthy:

- the adapter ABI (root ADRs 0008, 0024): four methods, the laws, the sandbox and the runtime;
- the `Source` interface (root ADR 0009): what bytes exist and how they are opened;
- the record model (root ADR 0051): the eight lifecycle kinds, added at package schema 3, all `stated`.

If Deploy grows its own runtime, its own record kinds or its own write path, the two disagree on
provenance, determinism and identity. If its connectors write back to a CMMS or a ticketing system,
ingestion changes the evidence it reads, which breaks non-negotiable 1.

Root ADR 0024 lists "entry-point plugin discovery" as an alternative it did not take yet: adapters are listed
in `neptune.adapters.builtin`, and discovery is to be added "when a third-party adapter exists". Deploy is
the first. This ADR fixes Deploy's side of that boundary. The compiler side (reading the entry points in
`neptune ingest`) is a compiler issue that supersedes that part of root ADR 0024; until it lands, a job uses
Deploy's adapters by building its own `AdapterRegistry` from them, which root ADR 0024 §9 already allows.

## Decision

1. **Entry points are the only way Deploy reaches the compiler.** `pyproject.toml` declares two groups:
   - `neptune.adapters`: the name is the adapter id; the value is a zero-argument callable returning an
     object with the four-method ABI. Today: `deploy_lifecycle = neptune_deploy.adapters.lifecycle:LifecycleAdapter`.
   - `neptune.sources`: the name is the connector id; the value is a callable returning a `Source`. Reserved
     and empty until the first connector issue lands.
   Deploy's adapter ids start with `deploy_`, so they never take a compiler id; the registry refuses a
   duplicate either way. Tests fail if Deploy declares any other entry-point group.
2. **Deploy adds adapters and Sources, nothing else.**
   - Lifecycle record kinds come from the compiler model (`neptune.model.lifecycle`). A descriptor may
     declare only those kinds, and a test checks it. A field a real form needs and the kinds lack is a
     compiler PR (a companion kind at a later schema version, root ADR 0051 consequences), never a Deploy
     record type.
   - No Deploy change edits `src/neptune`, the runtime, the store, the registry rule or the CLI.
   - Adapters are leaves: they import `neptune.model`, `neptune.identity` and `neptune.adapters.contract`
     only, plus a small standard-library set. A test parses their imports and fails on anything else, which
     keeps out the network, the filesystem and subprocesses (the sandbox, root ADR 0030).
   - Everything an adapter emits is `stated` evidence. No adapter infers a lifecycle state, orders stages,
     checks an intervention against its envelope or decides that a requalification passed.
3. **Deploy pins package schema 3.** `neptune_deploy.PACKAGE_SCHEMA_VERSION = 3`, `docs/contracts.md` and
   `contracts/lock.toml` (`package-schema = "3.0.0"`) name one version. A test fails if they disagree with each
   other or with the compiler's `SCHEMA_VERSION`, or if any lifecycle kind is newer than the pin. The
   dependency is the workspace's `neptune` with no version specifier: the distribution version does not move
   with the schema, so the pin lives in the contracts registry, as the Ledger's does.
4. **Connectors are read-only `Source`s.** A connector (`sources/`) lists and fetches from a CMMS, a ticket
   system or a fleet manager and hands bytes to the compiler. It never writes, acknowledges, transitions or
   comments on anything it reads. It may use the network, only under the compiler's local-only policy
   (root ADR 0026 §6): it calls `require_network(purpose)` before its first request and is refused while the
   workspace is local-only. Credentials and endpoints come from the operator's local configuration, only
   read requests leave the machine, and no adapter ever sees the network.
5. **The compiler's conformance check is the gate for every registered adapter.** CI's `neptune-deploy` job
   (path-filtered by `.github/scripts/ci_plan.py` to `packages/neptune-deploy/**`, `contracts/**`, root
   plumbing and compiler changes) loads every `neptune.adapters` entry point and runs
   `neptune.adapters.conformance.check_conformance` over neutral samples and the hostile inputs it derives.
   The compiler had no reusable suite, so this issue exposed one: a module composing the registry, the
   harness and `neptune.adapters.check`, with no change to compiler behaviour.
6. **The first adapter is registered and reads nothing.** `deploy_lifecycle` declares the eight lifecycle
   kinds, never claims a source (probe confidence 0), and, if a job names it, reports one
   `deploy_lifecycle.not_read` finding citing the whole source. Installing Deploy therefore changes no
   selection and no package until a format lands.

## Alternatives considered

- **Deploy defines its own lifecycle record types.** Faster to iterate, but Memory and Learn would read two
  shapes of one fact, and the records would bypass the schema version, the goldens and the compiler's
  checks. The kinds were added to the compiler (root ADR 0051) precisely so this does not happen. Lost.
- **Add Deploy's adapters to `neptune.adapters.builtin`.** One line per adapter, as root ADR 0024 §9 does for
  formats. It makes the compiler import a workspace member, inverts the dependency and puts Deploy's release
  on the compiler's. Lost.
- **Deploy runs its own ingest loop.** It would re-implement chunking, resume, caching, sandboxing and the
  receipt, and diverge from them. Lost.
- **A version specifier on `neptune` (`neptune>=0.x`).** The distribution version is `0.0.1` and does not track
  the schema, so the specifier would say nothing. The contracts registry is the programme's pin. Lost.
- **Connectors as adapters with network access.** An adapter that fetches breaks purity and the sandbox, and
  makes the chunk id stop describing its input. Fetching belongs to a `Source`. Lost.
- **Keep the conformance suite inside Deploy.** It would copy the compiler's laws into a member and drift
  from them. The suite belongs beside the laws it runs. Lost.

## Consequences

- A new Deploy format is a subpackage under `adapters/` and one entry-point line; CI runs conformance on it
  with no further wiring.
- `neptune ingest` uses Deploy's adapters only after the compiler reads the `neptune.adapters` entry points.
  That is a compiler issue, outside this package.
- Members may not import `neptune.discovery` (root `tests/unit/test_merge_freshness.py`), and a connector
  implements `Source` structurally. The first connector issue decides whether it needs that rule relaxed for
  `neptune.discovery.source`.
- Moving to a newer package schema is a deliberate PR here (contracts.md rules).
- Revisit if a lifecycle source needs more than one source's bytes per adapter call, or if a connector needs
  to write anything back.
