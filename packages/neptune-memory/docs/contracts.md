# neptune-memory contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.
Pins live in `src/neptune_memory/pins.py`; `tests/test_pins_memory.py` keeps them and this file in step.

## Publishes

- Graph schema and claim model: `GRAPH_SCHEMA_VERSION = 1`, published as `contracts/graph-schema/v1.9.0/` (1.0.0 to 1.8.0 stay)
  (JSON Schema, golden graph and vocabulary, generator `contracts/graph-schema/goldens.py`); consumed by Context,
  Deploy and Learn. Surface, version policy and guarantees: [`graph-schema.md`](graph-schema.md) and
  [ADR 0006](adr/0006-graph-schema-v1-contract-surface-and-memory-reader.md).
  - `neptune_memory.schema`: `NodeRef`/`NodeType`/`Tier`, `Claim` and its objects and provenance (with `ModelRef`),
    `Interval`/`CivilClock`/`LedgerTx`, the predicate registry (`CORE_PREDICATES`, `VOCABULARY_VERSION = 10`), the
    superseding resolver (`resolve`, `as_of`, `ResolutionFinding`, `resolver_config`, and from 1.9.0 `Build` for
    withdrawal), `codec` (strict JSON) and `export.graph_schema`. Claim model:
    [ADR 0002](adr/0002-graph-tiers-and-the-bi-temporal-claim-model.md); superseding:
    [ADR 0005](adr/0005-split-closures-bi-temporal-findings-and-the-resolver-config.md); withdrawal:
    [ADR 0016](adr/0016-memory-snapshots-rebuild-cli-and-build-withdrawal.md).
  - Read API: `schema.reader.MemoryReader`, reference `schema.reference.ReferenceReader`; identity traversal
    `schema.traverse.same_as_closure` ([ADR 0008](adr/0008-identity-consolidator-on-compiler-identity-links-and-assertions.md));
    clock conversion `schema.clocks.convert` over `clock_map` claims
    ([ADR 0011](adr/0011-time-domain-registry-clocks-mappings-and-chains-never-estimated.md)).
  - Contract tests: `neptune_memory.contract.suite.CHECKS`, run by the owner in
    `tests/test_reader_contract_memory.py` and `tests/test_golden_graph_memory.py`.
- Memory snapshots and the graph dump (not a registry contract): `memory consolidate` records a
  `MemorySnapshot` per Ledger snapshot and `memory dump` writes claims as canonical JSON Lines; their shapes and
  the determinism they promise are in [`guarantees.md`](guarantees.md) and
  [ADR 0016](adr/0016-memory-snapshots-rebuild-cli-and-build-withdrawal.md).
- Acceptance-corpus snapshot (a test fixture, not a registry contract), for Deploy, Context and the Demo v1
  quickstart (MVL-191). Use it instead of a hand-made graph:
  - Path: `packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json`. It is graph-schema **1.9.0**
    (`graph_schema_version: 1`, with `builds`), head 1, written by Memory's codec. It is what `memory rebuild`
    makes of the MVL-181 acceptance corpus 1.0.0: the corpus is compiled by the SDK into one package, exported as
    the records the Ledger catalogs (no `derived/`), registered at tx 1, and run through the default
    consolidators. Copy it byte for byte; do not edit it.
  - Regenerate it from the repository root with
    `uv run --all-packages python packages/neptune-memory/tests/fixtures/acceptance_corpus_snapshot.py`.
    `--check` compares instead of writing, and `--export FILE` also keeps the Ledger export for
    `memory rebuild`. `tests/test_acceptance_snapshot_memory.py` fails when a corpus, compiler or Memory change
    makes it stale.
  - Check a copy without importing `neptune_memory`: `memory verify FILE`. It exits 0 with a summary line. It
    exits 1 with one line per problem: a claim or finding id that does not match its content, a list out of
    canonical order, a wrong `generation`, a dangling reference. It exits 2 when the file is unreadable.
  - What it holds today: 12 runs from both sites, with `evidenced_by` and `has_member`, and 67 `integrity_finding`
    claims on runs and streams. One of them is the `error` on LEG-01's truncated patrol of 2026-09-14. It holds no
    events, `co_occurs_within`, configuration lineage, `authorisation_undecided`, calibration `drift`, `clock_map`
    or `same_as`. The compiler does not yet emit the records those need for this corpus. Incidents, work orders,
    changes and requalifications are generic CSV and PDF tables. Runs name no machine. The cell PC's clock offsets
    are only `derived/` estimates. Those claims appear when the compiler emits the records; this file is then
    regenerated, not edited.

## Consumes

- Compiler package schema: `SCHEMA_VERSION = 7` (`neptune.model.record`; 2 to 5 add kinds only, root ADRs 0037,
  0050, 0051 and 0062; 6 adds a kind and lifecycle list states, root ADR 0061; 7 adds the task kinds, root ADR
  0063). Alignment records (MVL-82, package-schema 3.0.0), human assertions (MVL-183, package-schema 5.0.0, the
  `neptune.assertions` file of root ADR 0062) and task records (MVL-33, package-schema 7.0.0) are consumed through
  the Ledger. The identity consolidator reads `identity_link`, `assertion` and `timestamp_domain` with the
  compiler's own strict readers (ADR 0008 §1). The configuration lineage consolidator reads
  `commissioning_baseline`, `maintenance_event`, `change_record`, `requalification_record`, `authorisation_envelope`
  (lifecycle records, root ADR 0051), `run`, `snapshot_binding` (root ADR 0050 §8) and the snapshot kinds a binding
  names, the same way ([ADR 0010](adr/0010-configuration-lineage-consolidator.md) §1); the time-domain registry
  reads `run`, `stream` and `clock_mapping` the same way, and `derived/clock_mapping` lines (root ADR 0060) with the
  compiler's `neptune.derived.clocks` reader (ADR 0011 §1, §4). The calibration history consolidator reads
  `calibration`, `hardware_configuration`, `hardware_component`, `frame_transform`, `frame_binding`,
  `maintenance_event` and `requalification_record` the same way
  ([ADR 0014](adr/0014-calibration-history-and-drift-consolidator.md) §1).
- Ledger catalog API: `CATALOG_API_VERSION = "pending: pinned when MVL-85 (Ledger catalog API) lands"`.
  Until then Memory codes against the `LedgerReader` Protocol in `neptune_memory/ledger.py` and tests
  against `StubLedger`.
