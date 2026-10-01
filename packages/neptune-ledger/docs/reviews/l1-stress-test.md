# L1 architecture review gate: stress-testing the catalog contract

- Issue: MVL-89 (gate of milestone L1 — Contract & Scaffold). Date: 2026-10-02.
- Under review: Ledger ADRs [0001](../adr/0001-ledger-place-in-the-programme.md),
  [0002](../adr/0002-catalog-data-model.md) (data model),
  [0003](../adr/0003-entity-threads-and-the-lineage-current-view.md) (threads),
  [0004](../adr/0004-catalog-api-error-model-and-versioning.md) (API);
  [catalog-api.md](../catalog-api.md), [catalog-walkthrough.md](../catalog-walkthrough.md),
  migration `0001_catalog.sql`, catalog-api 1.0.0 (draft).
- Outcome: **the gate passes after amendments.** The walk broke 5 of 12 cases. Each break is
  fixed by [ADR 0005](../adr/0005-l1-gate-catalog-data-model-amendments.md) (data model),
  [ADR 0006](../adr/0006-l1-gate-catalog-api-amendments.md) (API), migration
  `0002_gate_amendments.sql` and catalog-api **1.1.0, published stable**. All six L1 ADRs are
  Accepted. L2 may start once this merges and `l1-gate` is tagged.

## Verdict

| # | Case | Outcome | Resolution |
|---|---|---|---|
| 1 | Worked example: drone (aerial) | holds | — |
| 2 | Worked example: quadruped (legged) | holds | — |
| 3 | Worked example: manipulator | holds | — |
| 4 | Worked example: mobile robot | holds | — |
| 5 | Re-registration after a referenced source moved | **breaks** (absent location still routed; moved package reads as damaged) | ADR 0006 §2, §4, §5; `location_absence` (ADR 0005 §3); `unreachable` verdict |
| 6 | Same source under two adapter versions | holds | — (already in the contract tests) |
| 7 | Tampered `manifest.json` | **breaks** (a self-consistent tampered package can bring an existing record id with another body) | ADR 0005 §2: `body_digest`, `conflicting_id` for records, DB trigger |
| 8 | Package missing a table | **breaks** (no defined finding) | ADR 0006 §1: `file_missing` / `manifest_invalid` |
| 9 | Symlink in a package | **breaks** (only a symlinked root was defined) | ADR 0006 §1: `unsafe_entry`, never followed |
| 10 | Thread on two clocks, no mapping | holds (two contract tests) | Carried caveat on merges stated (ADR 0006 §8) |
| 11 | Tenant boundary probe | **breaks** (`register` takes any path) | ADR 0006 §3: tenant package roots |
| 12 | 10⁵-package registry | holds, with the conditions in ADR 0005 §4–§5 | Measured below; `query` paging added (ADR 0006 §6) |

Case 5 had two independent defects (a source that moved, a package directory that moved); both
are fixed.

## Method

Each case is walked through the three layers in turn: the **data model** (which rows ADR 0002
writes, which constraints fire), **threads** (ADR 0003 membership, order, current view) and the
**API** (ADR 0004 call, response, findings). A case **holds** when every layer gives a defined,
deterministic, provenance-preserving answer, with no blank turned into a fact. It **breaks**
otherwise. Facts about the worked examples come from the packages themselves (the compiler's
committed examples, `tests/fixtures/model/`), as the contract tests derive them. Case 12 is a
measurement on a real PostgreSQL 16.

## 1–4. The four worked examples

All four register in the order drone, quadruped, manipulator, mobile robot (tx_seq 1–4), and
every row lands as [catalog-walkthrough.md](../catalog-walkthrough.md) lists. That document is
checked against the database by `test_catalog_walkthrough.py`. No source, key or clock is shared
between examples, so every thread below belongs to one package, and each run sits on a clock of
its own.

| | drone (aerial) | quadruped (legged) | manipulator (fixed arm) | mobile robot (wheeled) |
|---|---|---|---|---|
| records / sources / clocks | 17 / 1 / 3 | 34 / 4 / 5 | 21 / 2 / 4 | 21 / 3 / 3 |
| declared threads | machine `px4.sys_uuid:0002…0027`: `Machine` subject + run, hw config, sw config and calibration cite (5 entries); sensor `px4.device_id:1310988` (1 subject) | none: the URDF's `machine` is `NotCovered`, and its sensor component states no identifier | none: run and calibration `machine` are `Unknown` | sites `register:S-007`, `S-008` (subjects); sensor `exif.body_serial:22061345` with one `cites` (the image) and no subject |
| anchored threads | run (logical id `NotCovered`), 2 streams `part_of`; configurations for hw/sw config and calibration | configuration on the URDF's `<robot>` range, 6 components `part_of`; run, 2 streams `part_of` | run on `session.mcap`; configuration on `handeye.yaml` | run on its bag; streams |
| world time | run `s` = 12 000 000 on `timestamp`, end `NotCovered` → open; streams untimed | run [1790762400000000000, …045000000] on `starting_time` | run [1790762401000000000, …020000000] on `log_time` | run [1790766000000000000, …200000000] on `time`; the photo's EXIF time is not world time |
| current view | every lineage set has one transform → `Known`; current = history | same | same | same |

**Data model.** It holds. Every record has its row and line, and provenance summary is filled
exactly where the record has record-level provenance. Ledger records (`source_artifact`, …) carry
no provenance columns. The one `Ambiguous` field (the manipulator's hand-eye `/direction`) is
indexed by pointer.

**Threads.** It holds. Blanks never become links. The quadruped's `<robot name>` and the
manipulator's missing machine create no machine thread. The image's camera serial keys a sensor
thread with only a `cites` entry, which is legal: membership comes from a field, not from
finding a subject. The drone's machine thread orders as partition `timestamp` = [run], then the
untimed partition [machine, hw config, sw config, calibration] by registration key and record id
bytes. The hand-eye `/direction` ambiguity is not a membership field, so it appears in no
`unresolved` list.

**API.** It holds. `register` → `registered` ×4, and again → `already_registered`. `verify` →
`intact`. `resolve` of every cited anchor → `resolved`, with referenced routes in registration
order. A window on the manipulator's `log_time` returns its run and nothing on another clock.
These are the 1.0.0 contract tests, unchanged.

## 5. Re-registration after a referenced source moved — breaks, fixed

**Walk.** The compiler records a move as a new `SourceRevision` at the new location plus a
`SourceAbsence` superseding the old location's revision (root ADRs 0009, 0010). The evidence
records keep their ids, because ids cover source content, locator and transform, not location.
Re-ingesting therefore gives a new manifest and a new package P′.

- *Data model.* P′ registers. `source` exists with the same size, so there is no conflict.
  `package_source` and `source_location` gain P′'s rows. The evidence records arrive with
  existing ids and identical bodies: new rows keyed by P′. The absence went only to the
  `record_source_absence` partition. No table held its location, so the catalog could not tell
  that L was gone.
- *Threads.* Holds. History has each record twice, once per package. The current view lists each
  once with both packages (ADR 0003 §4.3). Clocks are per transform, and the transform is
  unchanged, so P′'s entries share P's partitions.
- *API.* **Breaks.** `resolve` listed L as a route for P′, although P′ itself states L no longer
  holds the bytes.

Variant: the **package directory** moved instead. Re-registering from the new root is a no-op
that keeps the stored root (ADR 0002 §6). `verify` re-hashes at the stored root, finds nothing,
and the only failing verdict, `damaged`, reports a move as tampering. **Breaks.**

**Resolution.** ADR 0005 §3 adds `location_absence`. ADR 0006 §5: a package's route lists only
locations that no revision or absence in the same package supersedes. ADR 0006 §2: verdict
`unreachable` for an unreadable stored root, and `register(new_root)` → `already_registered`
certifies the bytes there, because registration now verifies before the lookup (§1). Contract
tests: `test_a_moved_source_registers_as_another_package`,
`test_verify_reports_a_moved_package_unreachable`.

## 6. Same source under two adapter versions — holds

**Walk.** P1 = ulog 1.0.0 and P2 = ulog 2.0.0 over the same bytes. In the *data model*, `source`
is shared, `transform` has two rows, and record ids and clock ids differ by transform. The
Ledger records (`source_artifact`, `source_revision`) have equal ids in both packages, and
`source_revision` has equal bodies. A `source_artifact` body may differ only in chunking (a
compiler hashing at another chunk size), which is accepted. Its conflict rule is content id plus
size (ADR 0005 §2). In *threads*, declared keys put both machines in one machine thread, and equal locators
put both in one anchored thread. Records and locators that differ are siblings or members of one
lineage set (ADR 0003 §4.1). Each transform's clocks are separate partitions. `latest_transform`
picks 2.0.0 per lineage set whatever the registration order, so a backfilled 1.0.0 never becomes
"latest". `pinned` and `as_registered_by` reach either version. In the *API*, the contract tests
`test_latest_transform_*`, `test_pinned_*` and `test_as_registered_by_*` cover {1.0.0 cfgA,
1.0.0 cfgB, 2.0.0}. Nothing to change.

## 7. A tampered `manifest.json` — breaks, fixed

**Walk.**
- *Tampered after registration:* `verify` → `damaged` + `manifest_digest_mismatch`. Holds.
- *Tampered and inconsistent at registration:* listed digests fail → `refused` +
  `file_digest_mismatch`. Holds.
- *Tampered and self-consistent:* the attacker edits a record and rewrites the manifest to match.
  That is a valid package with a new id. ADR 0002 §6 refuses an existing source or transform id
  with other fields. A tier-2 record id, however, covers only evidence and transform (root ADR
  0003), so the package can carry an existing record id with **another body**, for example
  another run start or another `machine`. Both rows would be catalogued, and a current view that
  shows "the record once, with both packages" would merge two different statements under one
  entry. **Breaks.** The same gap covers a clock (`timestamp_domain`) id arriving with another
  field or scope.

**Resolution.** ADR 0005 §2 adds `record.body_digest`, the sha256 of the record's canonical line.
One (kind, record id) has one digest per tenant. Registration refuses another digest with
`conflicting_id` (subject: the record id), and a trigger enforces the rule in the database.
Honest packages never trip it, because the compiler is deterministic. The one kind whose body
legitimately varies, `source_artifact` (its `chunk_size` and `chunks` are verification metadata,
not identity), is exempt: there a conflict is the same content id with another size, already the
`source` table's rule, and the same bytes at two chunk sizes register as two packages
(`test_the_same_bytes_at_two_chunk_sizes_register_as_two_packages`,
`test_a_source_artifact_may_differ_in_chunking_only`). Contract test
`test_an_existing_record_id_with_another_body_is_refused`; migration tests
`test_a_record_id_keeps_one_body` and
`test_an_existing_record_id_with_another_body_is_refused`.

## 8. A package missing a table — breaks, fixed

**Walk.** Root ADR 0022 says every kind has a table, an empty one being an empty file. A deleted
`records/video.jsonl` that the manifest still lists, or a manifest rewritten to omit the kind,
had no finding defined in ADR 0004. An implementation could have indexed the package as "no
videos", turning a missing table into a fact. **Breaks** (contract, not model).

**Resolution.** ADR 0006 §1. A listed but absent table is `file_missing`. A manifest that does not
count every kind is `manifest_invalid`. Both are `refused`, and `verify` reports a deleted table as
`file_missing` → `damaged`. Contract tests `test_a_package_missing_a_table_is_refused` and
`test_a_manifest_that_omits_a_table_is_refused`.

## 9. A symlink in a package — breaks, fixed

**Walk.** ADR 0004 defined only a symlinked *root* (`package_unreadable`). For
`records/run.jsonl → /elsewhere/run.jsonl`, or `records → /elsewhere/records`, with byte-identical
targets, a digest check that follows the link passes. The catalog would then index bytes from
outside the package, possibly another tenant's. **Breaks.**

**Resolution.** ADR 0006 §1. The walk never follows a link. A symlink, FIFO, socket or device at
any depth is `unsafe_entry` with its package-relative path, and the package is refused. The
contract test uses byte-identical link targets, so only a non-following implementation passes
(`test_a_symlink_in_a_package_is_refused_and_never_followed`, file and directory). Listed paths
that escape the root can never equal a present entry, so they are `file_missing`, and nothing
outside the root is opened.

## 10. A thread with records on two clocks and no mapping — holds (contract-tested)

**Walk.** The run of example C in ADR 0003 spans two MCAP splits, with `log_time` of c0 (L0) and
of c1 (L1). In the *data model*, each record's `world_clock` names its own domain, and the window
index is per clock. In *threads*, the world order is partition L0, then partition L1, ordered by
smallest registration key. Adjacency means nothing across the boundary, and the response marks
it. A calibration whose `valid_until` sits on another clock than `valid_from` has an open end,
returned as stated with its own `domain_id`. A merge naming mappings that do not reach L1 leaves
L1 as its own partition. In the *API*, `query` windows never cross a clock
(`test_query_time_window_stays_on_one_clock`).

**Evidence.** Two strict-xfail contract tests build such threads deterministically from the
drone, without MVL-82. Each asserts two separate clock partitions, the cross-partition order, no
merged partition and no mapped interval. They fail only with the stub's `NotImplementedError` and
bind MVL-90/L2.
- `test_two_clocks_across_packages_stay_apart_in_registration_order`: the drone and its 2.0.0
  re-parse put two runs on two clocks in one machine thread. The package whose clock sorts later
  as bytes is registered first, so the order must follow the smallest registration key, not the
  clock bytes.
- `test_two_clocks_in_one_package_order_by_clock_key_bytes`: one package. The run sits on
  `timestamp`, and one stream is given a Known start on another clock it carries. Both
  partitions share the registration key, so they fall back to clock key bytes, and the untimed
  stream comes last.

**Carried caveat** (deferred at ADR 0003's review). When mappings *are* named, two entries of one
clock that took different paths reorder only if their mapped intervals overlap or the mappings
contradict each other, and the unmerged order stays available. Stated with its proof in ADR 0006
§8.

## 11. A tenant boundary probe — breaks, fixed

**Walk.** Tenant B tries to resolve tenant A's evidence ref.
- *Data model:* B's schema has no row of A (every `tenant_id` references B's one tenant row), so
  every lookup returns nothing. Holds. The owner role can still join across schemas; MVL-99's
  per-tenant role is already the precondition for multi-tenant deployment (ADR 0002 §2).
- *API:* `resolve` gives `unresolvable` + `unresolvable_evidence`, which is identical to an
  anchor no one holds, so there is no oracle. `verify`, `lineage`, `threads_of` and `thread` answer
  as for unknown ids. Hash ids are equal across tenants and grant nothing. Holds.
- *The path:* `register(package_root)` accepted any directory the Ledger process can read. B could
  register A's package by path and then read A's evidence in its own catalog. **Breaks.**

**Resolution.** ADR 0006 §3. Each tenant's catalog is configured with its package roots. A root
outside them is `package_unreadable`, worded exactly as a missing root. Containment is decided
on the fully resolved `package_root` and fully resolved tenant roots, compared by path
components, never by string prefix. `access/` (MVL-99) owns the configuration with the
per-tenant role. Contract test `test_register_stays_inside_the_tenants_package_roots` (strict
xfail) probes `B_root/link → A's tree` and a `..` escape. Implementations configure roots through
`CatalogContract.make_tenant_catalog`.

## 12. A 10⁵-package registry — holds, under conditions

**Question.** Do the 0001/0002 indexes keep the thread and time-window queries under budget at
10⁵ packages and about 10⁷ records?

**Budget** (ADR 0005 §5): client wall-clock time for one call, all rows fetched, p95 over distinct
keys.
- thread (declared or anchored key) **p95 < 50 ms** for threads of up to 10³ entries;
- time window on one clock **p95 < 200 ms**;
- the workhorse thread (20 000 entries) **< 500 ms**;
- one registration's catalog writes independent of catalog size.

**Set-up.** `tests/ledger_catalog_scale.py`, run once at the full scale:

```
uv run --all-packages --all-groups python packages/neptune-ledger/tests/ledger_catalog_scale.py \
    --packages 100000 --data-dir ~/.cache/mvl89-catalog-scale/pg --out report-100000.json
```

- **Scale used: the full one.** 100 000 packages, **11 316 720** records (7 516 720 with world
  time), 1 500 000 logical-id rows, 99 000 sources, 300 000 clocks; 17.7 GB on disk (16.5 GB of
  it the record partitions and their indexes). Generation took 450 s for records, 96 s for
  indexes; no scale-down was needed.
- **Fleet.** 2 000 machines across seven embodiments (manipulator, mobile base, legged, humanoid,
  aerial, marine, road vehicle; 40–120 streams per recording), about 49 packages each. Machine 0,
  a mobile base, is a workhorse with 4 000 packages (one in twenty). One package in a hundred
  re-parses its predecessor's source under adapter 2.0.0 (lineage siblings, new record ids and
  clocks); one in a thousand is a long recording with 5 000 streams on one clock. Rows derive in
  SQL from the package sequence number: no randomness, same rows every run.
- **Schema.** The shipped migrations 0001 + 0002 in a `C`-collated database. The harness drops the
  record keys and indexes for the bulk load, recreates them from their own definitions and
  asserts the result equals the migrations' schema index for index (`schema_matches_migrations`).
- **Server.** The `pgserver` PostgreSQL 16.2 with stock settings (`shared_buffers` 128 MB,
  `work_mem` 4 MB, `random_page_cost` 4) on a 20-core workstation with 30 GB RAM shared with
  other jobs, so most of the 17.7 GB is not cached. Times are client wall-clock per call,
  fetching every row through psycopg, after one warm-up call; samples are distinct keys picked by
  a fixed stride (100 per typical case, 20 for the workhorse and long recordings).

**Results.**

| Query (what serves it) | Rows per call | p50 ms | p95 ms | max ms | Budget | |
|---|---|---|---|---|---|---|
| thread, declared machine key, typical (`thread`) | 235–250 | 4.3 | **15.3** | 20.8 | p95 < 50 | holds |
| thread, declared sensor key | 47–50 | 4.9 | **11.2** | 12.4 | p95 < 50 | holds |
| thread, anchored key (`thread` on an `EvidenceAnchor`) | 1 | 0.44 | **0.58** | 2.2 | p95 < 50 | holds |
| lineage set (kind, source), all transforms | 40–120 | 0.33 | 0.81 | 1.6 | — | |
| thread, workhorse machine (4 000 packages) | 20 000 | 179 | **195** | 289 | < 500 | holds |
| window, typical clock (`query` with `TimeWindow`) | 41–121 | 0.24 | **0.56** | 2.5 | p95 < 200 | holds |
| window, long recording (5 000 streams on one clock) | 3 001 | 5.9 | **9.1** | 10.2 | p95 < 200 | holds |
| `query` page, keyset cursor, 1 000 rows | 1 000 | 0.58 | 1.9 | 2.6 | — | |
| same page **without** `tenant_id` in the predicate | 1 000 | 588 | 717 | 717 | — | seq scan |
| package lookup by id (`register`'s first step) | 1 | 0.02 | 0.09 | 0.22 | — | |
| same lookup **without** `tenant_id` | 1 | 4.7 | 5.0 | 5.0 | — | seq scan |

| Registration writes at 10⁵ packages (median of 5, rolled back) | ms |
|---|---|
| log row + package row, **0001's** trigger functions | **17.0** |
| log row + package row, **0002's** trigger functions | **0.58** |
| 108 record rows of one package (every trigger and FK, incl. `record_body_agrees`) | 56.5 |

At 2 000 packages the same two log + package inserts took 0.55 ms (0001) and 0.31 ms (0002): the
0001 cost grows with the catalog, the 0002 cost does not.

**Plans** (EXPLAIN ANALYZE of the median sample, abridged):

- *Thread, declared key* (1.5 ms execution, 4 168 buffers):
  `Index Scan record_logical_id_by_value (namespace, value)` → per row `Index Scan
  package_tenant_id_package_id_tx_seq_key` → `Append` over the **five** pruned record partitions
  (`calibration`, `hardware_configuration`, `machine`, `run`, `software_configuration`), each an
  `Index Scan …_package_id_kind_idx` → `Sort` (quicksort, 127 kB). Before the kind filter was
  repeated on `record`, the same plan probed all 25 partitions per row.
- *Thread, anchored key* (0.008 ms): one `Index Scan record_stream_source_content_id_kind_md5_idx`
  on `(source_content_id, kind, md5(source_locator))`, then the locator compared exactly.
- *Window, typical* (0.041 ms): `Append` of `Index Scan …_world_clock_world_first_world_last_…`
  on `record_run` and `record_stream` with `Index Cond (world_clock = …, world_first <= last,
  registration_key <= as_of)`, `Filter COALESCE(world_last, world_first) >= first`, then `Sort`.
  Long recording (2.9 ms): the same, as a bitmap scan over 3 000 index entries.
- *Query page* (0.28 ms): `Limit` over `Index Scan record_stream_pkey` with `Index Cond (tenant_id
  = …, ROW(kind, record_id, package_id) > ROW(cursor))`: no sort, because `C` collation makes the
  key order the API order. Without `tenant_id`: `Parallel Seq Scan record_stream` over 3.66 M
  rows, 520 167 buffers read, top-N heapsort, 647 ms.

Full plans and every number are in the JSON report the script prints; the slow test asserts the
index each query uses.

**Findings from the measurement.**

1. **The budget holds at the full scale.** Typical thread p95 is 15 ms against 50, window p95
   0.6 ms (9 ms on a dense clock) against 200, and the largest thread, 20 000 entries, 195 ms.
   Window cost is bounded by records per clock, and a clock belongs to one source and one
   transform (ADR 0003 §3), so it does not grow with the catalog.
2. **Every keyed lookup must name `tenant_id`.** Keys lead with it (ADR 0002 §2). Without it a
   package lookup is a 100 000-row scan and a query page a 3.7 M-row scan: 30× and 1 000× slower,
   and growing. Recorded as a rule (ADR 0005 §5); the slow test keeps the two contrasts.
3. **A thread query must repeat its kind filter on `record`.** Otherwise the planner cannot prune
   partitions through the join and probes all 25 per entry (ADR 0005 §5).
4. **Byte order needs `C` collation.** The keyset page is served by the primary key only because
   the database's text order is the API's order (ADR 0005 §4).
5. **0001's monotonicity scans are O(n):** 17 ms per registration at 10⁵ packages, growing
   linearly; 0002's clock-row checks take 0.58 ms at any size. Removed (carried item 2).
6. **`query` needed a cursor.** `limit` alone could never reach row 1 001 of a 3.7 M-row kind.
   `QuerySpec.after` (ADR 0006 §6) pages in 0.6 ms per 1 000 rows.
7. **Not measured here:** the derived thread index (L2) and application-side ordering and
   serialisation, which do not exist yet. The budget transfers to them (What L2 inherits).

## Carried items

1. **ADR 0003 merge caveat.** Stated in ADR 0006 §8: same path keeps native order; different
   paths reorder only on overlapping intervals or contradictory mappings; unmerged order always
   available.
2. **0001's full-table "never goes backwards" scans.** Measured (case 12): 17.0 ms per
   registration at 10⁵ packages against 0.55 ms at 2 000, i.e. linear in the catalog. Decided:
   **removed** by migration 0002, which replaces both trigger functions with checks against the
   one `tx_clock` row, proven equivalent in ADR 0005 §1. 0001 is not edited.
3. **Repo map.** `api/` and `contract_tests/` added to `packages/neptune-ledger/AGENTS.md`.
4. **Is catalog-api ready for stable?** Yes. The gate leaves no open contract question for L2.
   Clock-merge contract tests wait for MVL-82 records (MVL-92), and those tests exercise behaviour
   1.1.0 already specifies. **Plan, executed in this PR:** 1.1.0 published `stable` with
   `scripts/contracts.py bump catalog-api 1.1.0 --status stable`. That locks `neptune-memory` at
   1.1.0 and regenerates `contracts/compatibility.md`. **Coordinator, after merge:** post the four
   announcements the tool printed (MVL-106, MVL-111, MVL-116, MVL-120; needs `LINEAR_API_KEY`) and
   tag `l1-gate`.

## ADR status after the gate

| ADR | Status | Changed by the gate |
|---|---|---|
| 0001 | Accepted | — |
| 0002 | Accepted | amended by 0005 (§4 mechanism, §5 tables, §6 records, §7 lookups) |
| 0003 | Accepted | amended by 0005 (§4.3 one body per id) and 0006 (§3.5 merge order) |
| 0004 | Accepted | amended by 0006 (§1, §2, §4, §5) |
| 0005 | Accepted | new: data model amendments, collation, scale budget |
| 0006 | Accepted | new: API amendments, catalog-api 1.1.0 stable |

## What L2 inherits

- **MVL-90 (register/verify):** ADR 0006 §1–§5 and the fifteen new contract test cases. Index from the
  verified bytes, read once. Include `tenant_id` in every keyed lookup (ADR 0005 §4).
- **The derived thread index (L2):** the catalog alone cannot resolve `part_of` (tier-2
  references inside record bodies), the sensor `category`, `software_version` keys or `unresolved`
  candidates. The derived index ADR 0002 §7 already planned must hold them, and it inherits the
  same budget. Rerun the scale harness against it.
- **MVL-92:** clock-merge and `mapping_out_of_range` contract tests (unchanged deferral).
- **MVL-99:** tenant package roots and per-tenant roles before any multi-tenant deployment.
