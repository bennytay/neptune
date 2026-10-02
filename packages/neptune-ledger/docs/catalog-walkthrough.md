# Catalog DDL walkthrough: the four worked example packages

What registering the compiler's four example packages (`drone`, `quadruped`, `manipulator`,
`mobile_robot`; root `tests/fixtures/model/README.md`) writes into one tenant's catalog under
[ADR 0002](adr/0002-catalog-data-model.md), as amended at the L1 gate by
[ADR 0005](adr/0005-l1-gate-catalog-data-model-amendments.md). The schema is
[`0001_catalog.sql`](../src/neptune_ledger/catalog/migrations/0001_catalog.sql) plus
[`0002_gate_amendments.sql`](../src/neptune_ledger/catalog/migrations/0002_gate_amendments.sql),
in a database with the byte-order `C` collation.

`tests/test_catalog_walkthrough.py` registers the packages, in the order below, into a real
PostgreSQL 16 and checks every table in this document against the database, so the numbers cannot
drift from the DDL. Tenant: `acme`, schema `tenant_acme`.

## Registration, step by step

Registration first verifies the whole package at its root, outside any transaction, and never
follows a symlink ([ADR 0006](adr/0006-l1-gate-catalog-api-amendments.md) §1). Then one
transaction in the tenant's schema does the rest:

1. Lock the transaction clock (`SELECT … FROM tx_clock FOR UPDATE`). Registrations in a tenant
   are serialised from here on.
2. Look up `package_id` = sha256 of `manifest.json`'s bytes. If it is there, stop: return the stored
   id and `tx_seq`, write nothing (ADR 0002 §6).
3. `next_tx()` gives the Ledger's next tick: `tx_seq` 1, 2, 3, 4 for the four packages, `tx_time`
   the host's UTC time as RFC 3339 with microseconds, never earlier than the previous tick's. A
   rebuild uses `replay_tx` with the logged tick instead.
4. `registration_log`: the tick, the package id, the root locator it was registered from and the
   Ledger's version. Then `package`, with `schema_version` and `receipt_id` from the manifest; the
   rest is copied from the log row. Then `schema_version` and `schema_version_projection` for each
   package-schema version the package states that the tenant has not seen: the drone brings
   version 1, with the projection mapping it is indexed by
   ([ADR 0011](adr/0011-schema-version-registry-and-record-kinds-by-package-schema-version.md)).
5. `source` (once per content id; an existing one with another size is refused) and
   `package_source` (per package) from `manifest.sources`; every example references its sources in
   place (`referenced`).
6. `source_location` from each `source_revision` record: the location object as stated
   (`{"kind": "local", "path": "flight.ulg"}`) and the revisions it supersedes; `location_absence`
   likewise from each `source_absence` record (the examples have none).
7. `transform` (once per transform id; an existing one with other fields is refused) and
   `transform_upstream` from each `transform_record`.
   The examples have nine transforms, one per adapter (`ulog`, `rosbag2`, `mcap`, `urdf`, `stl`,
   `handeye`, `rosbag1`, `csv`, `png`, all `1.0.0`), and no upstream edges.
8. `clock` from each `timestamp_domain` record: its field and scope.
9. `record`: one row per line of every `records/<kind>.jsonl` the manifest counts, into the kind's
   partition, or `record_default` for a kind without one (ADR 0008), with
   the package's `tx_seq` as `registration_key`, the provenance summary (source, locator,
   transform, assertion kind), the world time where ADR 0003 §3 gives the kind one, the
   pointers of its `Ambiguous` fields and `body_digest`, the sha256 of the record's line. A record
   id already catalogued with another digest is refused (ADR 0005 §2); then `record_logical_id` for every Known logical id the
   record states.
   [ADR 0009](adr/0009-record-index-bodies-pointers-and-generated-projections.md) adds, per row,
   the body as `jsonb` (a projection copy; the package line stays authoritative), the pointers of
   its `Unknown` fields, and the hot-filter columns generated from the package schema
   (`machine_*`, `site_*`, `run_ids`, `clock_ids`). Rows are written in `(kind, record_id)` order.

## Packages

| example | tx_seq | package id | package_source | source_location | clock | record |
|---|---|---|---|---|---|---|
| drone | 1 | `sha256:757f33c0c11d…` | 1 | 1 | 3 | 17 |
| quadruped | 2 | `sha256:44b4309877e2…` | 4 | 4 | 5 | 34 |
| manipulator | 3 | `sha256:74f076160632…` | 2 | 2 | 4 | 21 |
| mobile_robot | 4 | `sha256:97f31808983a…` | 3 | 3 | 3 | 21 |

Ten `source` rows in all: no source is shared between the examples. The quadruped's four sources
are its bag's `metadata.yaml` and `walk_0.mcap`, `robot.urdf` and `meshes/body.stl`, each with
one location.

## Record rows by partition

Columns are `tx_seq`. Partitions not listed hold no rows for these packages.

| partition | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| record_source_artifact | 1 | 4 | 2 | 3 |
| record_source_revision | 1 | 4 | 2 | 3 |
| record_transform_record | 1 | 3 | 2 | 3 |
| record_ingest_finding | 2 | 0 | 1 | 0 |
| record_timestamp_domain | 3 | 5 | 4 | 3 |
| record_frame_graph | 0 | 1 | 1 | 0 |
| record_frame | 0 | 3 | 4 | 0 |
| record_frame_transform | 0 | 2 | 1 | 0 |
| record_run | 1 | 1 | 1 | 1 |
| record_stream | 2 | 2 | 2 | 2 |
| record_machine | 1 | 0 | 0 | 0 |
| record_hardware_configuration | 1 | 1 | 0 | 0 |
| record_hardware_component | 2 | 6 | 0 | 0 |
| record_software_configuration | 1 | 1 | 0 | 0 |
| record_calibration | 1 | 0 | 1 | 0 |
| record_site | 0 | 0 | 0 | 2 |
| record_spatial_artifact | 0 | 1 | 0 | 0 |
| record_image | 0 | 0 | 0 | 1 |
| record_structured_table | 0 | 0 | 0 | 1 |
| record_structured_record | 0 | 0 | 0 | 2 |

Provenance summary, by shape:

- **Evidence records** (`run`, `frame`, `calibration`, …) take `source_content_id`,
  `transform_id` and `assertion_kind` from their record-level provenance: the manipulator's run
  cites `session.mcap` (`sha256:d6bebd2e…`) through the `mcap` transform, `observed`.
- **Findings** take `transform_id` from the finding and `source_content_id` from an `evidence`
  subject; `assertion_kind` is NULL. The manipulator's `handeye.unit_undeclared` cites
  `handeye.yaml` through the `handeye` transform.
- **Ledger records** (`source_artifact`, `source_revision`, `source_absence`, `transform_record`)
  carry no provenance, so all three columns are NULL; `source`, `source_location` and `transform`
  index them. A `source_artifact` row's `record_id` is its content id.

`line` is the record's line in its table, so the drone's two streams are lines 1 and 2 of
`records/stream.jsonl` in package `sha256:757f33c0c11d…`. Nothing else of the record is copied:
the package is the record.

## World-time index

Rows with a world time, keyed `tx_seq·kind·first 8 hex of the record id`. The columns hold ADR 0003
§3's start `s`, end `e` and the clock of `s`, named by its `timestamp_domain` record
(`world_clock`); its field is shown for reading. Ticks are as the source states them, never
converted. A NULL `world_last` is an open end: not Known, or on another clock than the start.

| row | clock field | world_first | world_last |
|---|---|---|---|
| 1·run·bbe0a997 | timestamp | 12000000 | NULL |
| 2·run·5bf3bccb | starting_time | 1790762400000000000 | 1790762400045000000 |
| 3·run·0cf852d4 | log_time | 1790762401000000000 | 1790762401020000000 |
| 4·run·83e76d50 | time | 1790766000000000000 | 1790766000200000000 |

- The drone's run starts at tick 12 000 000 of the ULog `timestamp` clock (microsecond ticks,
  epoch `Unknown` in the package) and states no end, so `world_last` is NULL. It is on a different
  clock from every other row and is never compared with them.
- The streams' and calibrations' times are `Unknown` in these packages (series rows are not
  written; no calibration states a validity or when it was performed), so they have no row here.
- The photo's EXIF capture time stays in the package: ADR 0003 §3 gives `image` no world time, and
  the catalog indexes exactly that definition.

A time-window query names one clock and compares ticks only within it:

```sql
SELECT kind, record_id, package_id FROM tenant_acme.record
WHERE world_clock = 'rec:sha256:64c7daee…'          -- the manipulator's MCAP log_time
  AND world_first <= 1790762401010000000 AND world_last >= 1790762401000000000;
```

## Logical-id index

Every Known logical id a record states, keyed `tx_seq·kind·pointer·namespace·value`, with the number
of rows.

| row | rows |
|---|---|
| 1·calibration·/machine·px4.sys_uuid·000200000000343233345117003a0027 | 1 |
| 1·hardware_component·/identifiers/0·px4.device_id·1310988 | 1 |
| 1·hardware_configuration·/machine·px4.sys_uuid·000200000000343233345117003a0027 | 1 |
| 1·machine·/identifiers/0·px4.sys_uuid·000200000000343233345117003a0027 | 1 |
| 1·run·/machine·px4.sys_uuid·000200000000343233345117003a0027 | 1 |
| 1·software_configuration·/machine·px4.sys_uuid·000200000000343233345117003a0027 | 1 |
| 4·image·/capture/device_identifiers/0·exif.body_serial·22061345 | 1 |
| 4·site·/identifiers/0·register·S-007 | 1 |
| 4·site·/identifiers/0·register·S-008 | 1 |

The drone's machine declares `px4.sys_uuid` at `/identifiers/0`, and five records name it at
`/machine`: `record_logical_id_by_value` finds all six in one lookup. Whether those rows form one
entity thread is the thread resolver's decision (ADR 0003), not the catalog's. The quadruped's URDF
names no machine and the manipulator's run states no `machine`, so they have no rows: a blank
never becomes a link.

## Ambiguous fields

One record has an `Ambiguous` field: the manipulator's hand-eye `frame_transform`
(`rec:sha256:e2c144c5…`), whose `ambiguous_pointers` is `{/direction}`. The receipt's `ambiguous`
list agrees, which the test checks for all four packages.

## Re-registration

Registering all four packages again returns the same four `(package_id, tx_seq)` pairs, allocates
no tick (`tx_clock.last_seq` stays 4) and changes no row. Replaying the registration log into a
fresh tenant reproduces every column of every table byte for byte, transaction times included.
