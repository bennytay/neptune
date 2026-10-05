# neptune-memory contracts

What this package publishes to, and consumes from, other workspace members. Each entry names the schema or
interface under `contracts/`, its version, and the ADR that fixed it. Nothing else is a public surface.
Pins live in `src/neptune_memory/pins.py`; `tests/test_pins_memory.py` keeps them and this file in step.

## Publishes

- Graph schema and claim model: `GRAPH_SCHEMA_VERSION = 1`, published as `contracts/graph-schema/v1.7.0/` (1.0.0 to 1.6.0 stay)
  (JSON Schema, golden graph and vocabulary, generator `contracts/graph-schema/goldens.py`); consumed by Context,
  Deploy and Learn. Surface, version policy and guarantees: [`graph-schema.md`](graph-schema.md) and
  [ADR 0006](adr/0006-graph-schema-v1-contract-surface-and-memory-reader.md).
  - `neptune_memory.schema`: `NodeRef`/`NodeType`/`Tier`, `Claim` and its objects and provenance (with `ModelRef`),
    `Interval`/`CivilClock`/`LedgerTx`, the predicate registry (`CORE_PREDICATES`, `VOCABULARY_VERSION = 9`), the
    superseding resolver (`resolve`, `as_of`, `ResolutionFinding`, `resolver_config`), `codec` (strict JSON) and
    `export.graph_schema`. Claim model: [ADR 0002](adr/0002-graph-tiers-and-the-bi-temporal-claim-model.md);
    superseding: [ADR 0005](adr/0005-split-closures-bi-temporal-findings-and-the-resolver-config.md).
  - Read API: `schema.reader.MemoryReader`, reference `schema.reference.ReferenceReader`; identity traversal
    `schema.traverse.same_as_closure` ([ADR 0008](adr/0008-identity-consolidator-on-compiler-identity-links-and-assertions.md));
    clock conversion `schema.clocks.convert` over `clock_map` claims
    ([ADR 0011](adr/0011-time-domain-registry-clocks-mappings-and-chains-never-estimated.md)).
  - Contract tests: `neptune_memory.contract.suite.CHECKS`, run by the owner in
    `tests/test_reader_contract_memory.py` and `tests/test_golden_graph_memory.py`.

## Consumes

- Compiler package schema: `SCHEMA_VERSION = 6` (`neptune.model.record`; 2 to 5 add kinds only, root ADRs 0037,
  0050, 0051 and 0062; 6 adds a kind and lifecycle list states, root ADR 0061). Alignment records (MVL-82,
  package-schema 3.0.0) and human assertions (MVL-183, package-schema 5.0.0, the `neptune.assertions` file of
  root ADR 0062) are consumed through the Ledger. The identity consolidator reads `identity_link`, `assertion`
  and `timestamp_domain` with the compiler's own strict readers (ADR 0008 §1). The configuration lineage
  consolidator reads `commissioning_baseline`, `maintenance_event`, `change_record`, `requalification_record`,
  `authorisation_envelope` (lifecycle records, root ADR 0051), `run`, `snapshot_binding` (root ADR 0050 §8) and the
  snapshot kinds a binding names, the same way ([ADR 0010](adr/0010-configuration-lineage-consolidator.md) §1); the time-domain registry reads
  `run`, `stream` and `clock_mapping` the same way, and `derived/clock_mapping` lines (root ADR 0060) with the
  compiler's `neptune.derived.clocks` reader (ADR 0011 §1, §4). The
  calibration history consolidator reads `calibration`, `hardware_configuration`, `hardware_component`,
  `frame_transform`, `frame_binding`, `maintenance_event` and `requalification_record` the same way
  ([ADR 0014](adr/0014-calibration-history-and-drift-consolidator.md) §1).
- Ledger catalog API: `CATALOG_API_VERSION = "pending: pinned when MVL-85 (Ledger catalog API) lands"`.
  Until then Memory codes against the `LedgerReader` Protocol in `neptune_memory/ledger.py` and tests
  against `StubLedger`.
