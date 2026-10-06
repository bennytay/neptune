# 0017 — Configuration snapshots open their anchored configuration thread; every other unthreaded kind decided

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-92 (follow-up)
- Supersedes: [ADR 0003](0003-entity-threads-and-the-lineage-current-view.md) §2, the `configuration` row only

## Context

Package schema 2 added `configuration_snapshot`: one configuration document as its bytes declare it (root
ADR 0037). The compiler binds a run sheet's pinned parameter files to these snapshots with stated
`SnapshotBinding`s (root ADR 0072, PR #90). ADR 0003 §2's thread table predates schema 2 and has no row for
the kind, so the catalog puts snapshots in no thread: `threads_of(snapshot)` answers `found` with no
memberships.

Memory names a bound snapshot's configuration by the thread its evidence keys (Memory ADRs 0010 §1, 0018
§2). With no row, all 10 configuration-snapshot pins of acceptance corpus 2.1.0 became
`configuration_unknown`, which blocks the Demo v1 question "what changed since the last good run". Memory
PR #154 (Memory ADR 0022) works around it. It computes `thread:<id>` itself from the snapshot's
record-level evidence with ADR 0003 §1.3's published rule, and marks that as transitional until this row
exists.

ADR 0003 §2 says a new row is a superseding ADR, never an implementation choice. ADR 0010's Consequences
add that the thread index then needs a membership change and a catalog rebuild.

## Decision

### 1. The row

ADR 0003 §2's `configuration` row becomes:

| Thread kind | Key | `subject` | `cites` / `part_of` |
|---|---|---|---|
| `configuration` | anchored | `HardwareConfiguration`, `SoftwareConfiguration`, `Calibration`, `ConfigurationSnapshot` | part_of: `HardwareComponent.configuration` |

A `configuration_snapshot` with `stated` or `observed` provenance and a record-level evidence anchor is the
`subject` of the anchored `configuration` thread its **own** evidence keys:
`ThreadKey("configuration", EvidenceAnchor(source content id, locator))`. Its id is ADR 0003 §1.3's rule:
`"sha256:" + hex(sha256(canonical JSON of {"key": {"locator": …, "source": …}, "kind": "configuration"}))`.
That is byte for byte the node Memory ADR 0022 §1 computes, so Memory can drop its workaround with no id
churn. Tests check this on the demo corpus's 12 snapshots and on the compiler's manifest goldens.

Every other rule of ADR 0003 and ADR 0010 holds unchanged. A snapshot has no world time (ADR 0003 §3), so it
sits in the untimed partition. Its lineage set is `(thread, configuration_snapshot, source)`. An inferred
snapshot, or one with no record-level anchor, opens nothing. The package schema and registration refuse a
snapshot line without evidence with `record_invalid` before membership is computed (ADR 0010 §1), so a
malformed snapshot is a structured refusal, never a crash and never a partial index.

### 2. Why its own key, not its configuration's

- **The rule identifies a snapshot stably across re-ingests.** The anchor is tier 1 plus a locator. The
  compiler's config adapter cites a document's root as `[config:document index, json_pointer ""]` (root ADR
  0037 §2). Every adapter version that reads the same bytes the same way gives the same anchor, so
  re-ingests are lineage siblings in one thread (ADR 0003 §4.1). New bytes at the same path are a new
  source revision and a new thread, related by `revises` (ADR 0003 §5).
- **Its own evidence is the only stated key.** A snapshot names no machine and no other configuration
  record. "Its configuration's thread" would need a link the record does not state, which would be an
  identity join. The anchored `configuration` thread already means "what this evidence declares" (ADR 0003
  §1.2), and that is exactly a configuration document.
- **No identity merge.** Two snapshots with the same `digest` (equal values) but different evidence are two
  threads. Comparing digests is a reader's question, not a Ledger key. Two byte-identical files are one
  source and so one thread, as ADR 0003 §1.2 already says of all anchored threads.

### 3. Every other kind with no row

Each record kind of package schema 9 that is not in ADR 0003 §2's table now has an explicit decision.
"Unthreaded" means the kind is reached by record id, by `query` on its kind and projected columns, or by
`lineage`, and never as a thread entry. "Follow-up" means a row is a plausible later ADR. It is not built
now because no caller needs it (root AGENTS.md: no abstraction without a present caller).

| Kinds | Decision | Why |
|---|---|---|
| `source_artifact`, `source_revision`, `source_absence`, `transform_record`, `ingest_finding` | Unthreaded | Catalog bookkeeping, not evidence about an entity. They are served by `resolve`, `verify`, `lineage` and revision edges (ADRs 0002, 0003 §5). |
| `timestamp_domain`, `civil_time_zone`, `frame`, `frame_graph`, `frame_transform` | Unthreaded | Reference structure. Clocks are partition keys (ADR 0003 §3), not entries, and frames are the spatial index's references (ADR 0015). |
| `clock_mapping`, `identity_link` | Unthreaded (already decided) | ADR 0010 §8–§9: a thread reads them as merges and links, never as entries. |
| `structured_table`, `structured_record`, `spatial_artifact` | Unthreaded | They declare no identifier. A row per table row would swamp threads (ADR 0005's 20 000-entry budget), and `query` serves them by kind. |
| `configuration_value` | Follow-up: `part_of` via `ConfigurationValue.snapshot` | It is the natural sibling of `DocumentBlock.document`. A parameter dump can hold thousands of values, and Memory reads values by their snapshot id. Add the row when a caller needs a snapshot's values in thread order. |
| `snapshot_binding`, `run_assembly`, `frame_binding` | Follow-up: `part_of` the run via `.run` | They relate records already threaded. Memory reads them by kind (Memory ADRs 0009, 0010). |
| `run_declaration` | Follow-up: `part_of` the run, `cites` machine and site | Memory reads it by kind (Memory ADR 0020). |
| The eight lifecycle kinds of schema 4 (`authorisation_envelope` … `risk_assessment`) | Follow-up: `cites` machine via `machines` and site via `site` | ADR 0010's Consequences already noted they join no thread. Memory reads them by kind. A row changes every machine thread, so it gets its own ADR. |
| `task_brief`, `work_order`, `requirement`, `sop_section` | Follow-up: making the reserved `task` kind live | ADR 0003 §2 reserves `task` until a record kind declares one, and schema 7 now does. That needs its own ADR on the key (`identifiers`) and on `part_of` for requirements and sections. |
| `assertion` | Unthreaded | A stated claim about records, scoped by its own fields. Memory owns claims. |
| `description_expansion`, `description_extension`, `hardware_specification` | Follow-up: `part_of` the configuration via `.configuration` / `.subject` | Robot-description detail under a `HardwareConfiguration`. No reader needs it in thread order yet. |

Only `configuration_snapshot` is built: it is the one the demo needs.

### 4. Existing catalogs: migration 0012

The thread tables are append-only and are filled at registration from the verified lines (ADR 0010 §1). A
catalog that registered snapshots before this ADR has none of their thread rows, and SQL cannot recompute
canonical-JSON thread ids from stored bodies byte for byte. Migration 0012 therefore adds no table. Like
0007 and the generated projection guards, it refuses to apply when `record` holds any
`configuration_snapshot` row, and names the rebuild (ADR 0012). Rebuilding replays registration, so it
writes the new rows (tested). A catalog with no snapshot needs no rebuild: its index already equals what
a rebuild would give.

### 5. The contract

catalog-api does not change. The thread kind `configuration`, `ThreadKey`, `Membership` and `ThreadEntry`
already carry a snapshot entry. The membership table is not in `contracts/catalog-api/`: no schema
enumerates which record kinds join which thread, and no golden holds a snapshot. Answers do change:
`threads_of(snapshot)` now lists one membership where it listed none, and those threads exist. Consumers
that resolve a snapshot through the catalog (Memory ADR 0018) get the same node they compute today.

## Alternatives considered

- **Put the snapshot under its configuration's thread.** Rejected. No record states which configuration a
  snapshot belongs to, so choosing one would be an identity join (§2). Override only if the §1.3 rule could
  not identify a snapshot stably across re-ingests, and it can.
- **A new thread kind (`configuration_snapshot`).** Rejected. The id would hash another `kind`, so it would
  differ from Memory's node, and every reader would need a second kind for the same "what this document
  declares" thread.
- **Key by the value digest.** Rejected. It joins every value-equal document into one thread, which is an
  identity no record states (Memory ADR 0022 rejected the same thing).
- **Backfill the rows in migration 0012.** Rejected. It would mean reimplementing canonical JSON and sha256
  thread ids in SQL over `record.body`, a second implementation that could drift from
  `threads.membership`. A rebuild uses the one implementation.
- **Thread every unthreaded kind now.** Rejected. Each row changes existing threads' contents and forces a
  rebuild, and no present caller needs them (§3).

## Consequences

- Memory can resolve the 10 demo pins (5 distinct snapshots) through `threads_of` and retire its
  transitional rule. The ids stay the same, so no claim churns.
- A catalog holding snapshots must be rebuilt to apply 0012. The harness and the tests build catalogs fresh.
- Each follow-up in §3 is its own superseding ADR plus a migration guard like 0012's.
- Revisit if the config adapter changes how it cites a document's root, which would split threads across
  versions as ADR 0003's Consequences describe, or if the thread-id rule changes (catalog-api 2.0.0).
