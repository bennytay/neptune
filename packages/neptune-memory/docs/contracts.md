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
    (`graph_schema_version: 1`, with `builds`), head 2, written by Memory's codec. It is what
    `memory rebuild --with-estimates` makes of the MVL-181 acceptance corpus 1.0.0. The pipeline:
    1. The SDK compiles the corpus into one package, registered at tx 1.
    2. `python -m neptune_deploy map` maps that package with the `cmms_generic`, `jira_json`, `register_zone` and
       `servicenow_csv` presets into a lifecycle package, registered at tx 2.
    3. Both are exported as the records the Ledger catalogs, plus the compiler's `derived/clock_mapping` fits.
    4. The deterministic consolidators run, with `memory.time_estimates` alongside
       ([ADR 0017](adr/0017-estimated-clock-mappings-in-a-tenant-graph.md)).

    Copy the file byte for byte; do not edit it.
  - Regenerate it from the repository root with
    `uv run --all-packages python packages/neptune-memory/tests/fixtures/acceptance_corpus_snapshot.py`.
    `--check` compares instead of writing, and `--export FILE` also keeps the Ledger export for
    `memory rebuild`. `tests/test_acceptance_snapshot_memory.py` fails when a corpus, compiler or Memory change
    makes it stale.
  - Record ids in it depend on the libraries the compiler's transforms record. The calibration adapter records
    the expat bundled with CPython, so a different 3.12 patch release gives other finding record ids.
    `acceptance_corpus.environment.json` lists those libraries. The committed files are made under the Python CI
    installs, and CI requires a byte-identical regeneration. The byte check skips only on a local host whose
    `expat` or `python` alone differ. It still checks the same facts with every record id masked. Any
    other change fails: the corpus, an adapter or its version, or a library `uv.lock` pins. Cite corpus evidence
    by source path and locator, not by record id.
  - Check a copy without importing `neptune_memory`: `memory verify FILE`. It exits 0 with a summary line. It
    exits 1 with one line per problem: a claim or finding id that does not match its content, a list out of
    canonical order, a wrong `generation`, a dangling reference. It exits 2 when the file is unreadable.
  - What it holds today:
    - 12 runs from both sites, with `evidenced_by` and `has_member`.
    - 67 `integrity_finding` claims on runs and streams. One is the `error` on LEG-01's truncated patrol of
      2026-09-14.
    - One event: Deploy's `incident_record` for the near-miss INC-C3-0004. Its claims are `event_kind`,
      `stated_severity`, `has_description` and `evidenced_by`.
    - 36 `maps_to` and 36 `clock_map` claims. These are the compiler's estimated fits, all `inferred`, and readers
      drop them with `include_inferred=False`. One is the cell PC's ≈ −96.7 s on 2026-09-14.
  - What it lacks:
    - No event for INC-C3-0011, and no `co_occurs_within`. The arm-cell incident is a PDF, and Deploy's
      incident template for it has not shipped. Bag e-stops are MVL-204. No event is ever aligned through an
      inferred mapping.
    - No configuration lineage and no `authorisation_undecided`. Deploy's 5 `change_record`, 15
      `maintenance_event` (WO-26-0911 among them) and 2 `authorisation_envelope` records are in the Ledger
      export. Memory places them on Ledger thread nodes, which it reads today only from the `ledger_thread`
      stand-in (ADR 0003 §1); no real Ledger export carries those. Runs also name no machine (MVL-205).
    - No calibration `drift` (MVL-207, then #129) and no `same_as` for events.

    This file is regenerated as those land, never edited.
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
- Ledger catalog API: `CATALOG_API_VERSION = "1.7.0"` (`contracts/catalog-api/v1.7.0/`, locked in
  `contracts/lock.toml`). Memory reads thread membership from the catalog's `threads_of` answers (`ThreadsOf`,
  `Membership`, `UnresolvedMembership`, `ThreadKey`), parsed from the published wire form by
  `neptune_memory.ledger.threads_of_from_json` without the bookkeeping `api_version`, `as_of` and `findings`;
  it imports no Ledger code ([ADR 0018](adr/0018-thread-membership-from-the-catalog-api.md)). It reads through
  the `LedgerReader` Protocol in `neptune_memory/ledger.py`; the `memory` CLI's Ledger export carries the
  answers under `threads`. The `ledger_thread` stand-in (ADR 0003 §1) stays for the archetype goldens and
  unit tests; a reader that answers no thread queries is read through it alone.
