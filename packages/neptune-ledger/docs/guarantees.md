# Ledger guarantees

What the Ledger promises its operators and the layers above, each with the test that holds it in
CI. A guarantee changes only by an ADR, and the same PR changes its test.

## 1. The catalog can be rebuilt from packages alone

**Statement.** Given the tenant's **registry manifest**, the packages it lists, and the same Ledger
version, `ledger rebuild` produces the catalog the Ledger held, byte for byte: every table and
every row, transaction times included (ADR 0002 §4,
[ADR 0012](adr/0012-rebuild-from-packages-registry-manifest-dump-and-replay.md)). Without the
manifest, the packages alone determine every row of `ledger dump`. The dump is the catalog
without the columns that record when and in which order packages were registered, so the
Ledger is never the only source of a fact.

**Inputs.**

| Input | What it is | Written by |
|---|---|---|
| Registry manifest | The registration log as one canonical JSON file: per registration, `tx_seq`, `tx_time`, `package_id`, `root_locator`, `ledger_version`, in `tx_seq` order | `ledger register --manifest <path>` after every registration that adds a package; `ledger manifest` on demand |
| Packages | The compiler packages at the manifest's roots, unchanged | the compiler; the Ledger never writes them |
| Ledger version | The `neptune-ledger` release, which fixes migrations, projections and thread membership | the deployment |

**What is compared.** `ledger dump` writes canonical JSON Lines from one snapshot: a header, then
per table its kept columns and one object per row (column → PostgreSQL text form; a NULL column is
absent), rows in byte order. It leaves out `tenant_id` and the transaction columns `tx_seq`,
`tx_time`, `registration_key`, `first_registration_key`, `last_seq` and `last_time`. Every
left-out value follows from the manifest, so the dump plus the manifest determine the catalog.

**How to rebuild.**

```
ledger --dsn "$DSN" --tenant acme --manifest /srv/ledger/acme.manifest.json \
    rebuild --from /srv/ledger/acme.manifest.json --package-root /srv/packages/acme
```

- One transaction: the tenant's schema is dropped, migrated afresh, and every entry is
  re-registered in `tx_seq` order at its logged tick (`replay_tx`), from its logged root, inside
  the tenant's package roots. Any refusal rolls everything back and names the entry; the old
  catalog stands. Readers wait until commit and never see a partial catalog.
- A registered package the manifest does not list refuses the rebuild unless `--prune` is
  given, so a stale manifest cannot drop packages silently. The rebuild locks the clock first,
  so no registration slips in between that check and the drop.
- Live registration continues after the last replayed tick.

**Limits.**

- *Another Ledger version* re-indexes the same packages over the same transaction keys. That is
  a new catalog lineage, by design (ADR 0002 §4, ADR 0009 §6): its log records the new version,
  and its rows follow the new version's migrations and projections. Upgrading the Ledger is
  exactly this rebuild, and it needs `--new-lineage`. Keep the old manifest: it is the old
  lineage's only record.
- *A moved or damaged package* refuses the rebuild with that registration's findings
  (`package_unreadable`, `file_digest_mismatch`, …), as does a logged root that now resolves
  through a link, or that now holds another package, however intact
  (`manifest_digest_mismatch`). Restore the package, or edit its manifest root to its real new location. The rebuilt log then records the new root, and nothing else differs.
- *Conflicting packages* (ADR 0005 §2): whichever registered first stands and the other was
  refused, so it is not in the log. Order independence holds for sets of packages that register
  without conflict.
- A rebuild drops the schema's grants; `access/` (MVL-99) reapplies them.

**Held by** `tests/test_ledger_rebuild.py`, run by the `neptune-ledger` CI job (`make check
PKG=neptune-ledger`). The fixtures are the four contract examples (drone, manipulator, mobile
robot, quadruped), the schema-4 `manipulator_cell` with its lifecycle kinds, and three drone
variants: adapter v2 lineage siblings, a moved source with its absence, and another chunk size.

- `test_a_rebuild_from_the_manifest_and_packages_is_byte_identical` (seeds 94 and 2026):
  registers in a seeded shuffle through the CLI and dumps; rebuilds from the manifest, in place
  and into a fresh, empty database, and dumps each. All three dumps are equal bytes, the manifest
  is rewritten unchanged, and every table, transaction columns included, is unchanged.
- `test_the_dump_does_not_depend_on_registration_order_or_tenant`: the shuffle and package-id
  order, in two databases and two tenants, dump to equal bytes.
- `test_every_transaction_column_is_left_out_of_the_dump`: a migration that adds a transaction
  column under a new name fails CI until the dump leaves it out.
- The refusal (a damaged package, another package at a logged root), prune, root, clock,
  lineage and malformed-manifest cases in the same file.

## 2. Package series are read in place, never copied

**Statement.** The lakehouse reads a package's `series/*.parquet` where the package lies (its
registered root, or a byte-for-byte mirror an `ObjectStore` names) and writes nothing: no copy,
cache or rewrite of a package byte
([ADR 0013](adr/0013-lakehouse-layout-and-in-place-series-reads.md)). A read checks, and reports
as a finding rather than reading, a series file that is missing, reached through a link, of
another size than its manifest states, whose footer is not Parquet holding int64 `seq` and the
scanned `time/<i>`, or whose pages an engine cannot decode; the other files' rows still come
back. It checks size, not hash: a same-size rewrite that is still such Parquet reads silently.
Byte equality is `verify`'s job ([ADR 0006](adr/0006-l1-gate-catalog-api-amendments.md)).

**Held by.** `tests/test_ledger_lake.py`:
`test_reads_copy_no_byte_and_scan_the_packages_own_files` snapshots every file's size, mtime and
inode under the test root before and after reads with both engines, and checks that the scan names
the package's own file. `test_a_package_changed_since_registration_is_a_finding` covers truncation,
links, removal, a changed manifest, a moved root and a linked root;
`test_a_file_whose_pages_do_not_decode_costs_only_its_own_rows` covers same-size page damage
with both engines.

**Budget.** A 10⁶-row window reads in under 200 ms locally, end to end (catalog, manifest, footer,
scan into Arrow): `test_a_million_row_window_reads_under_200_ms` (`slow`). Measured: DuckDB about
55 ms, DataFusion about 75 ms (ADR 0013 §6).

## 3. Time and space are indexed per declared clock and per declared frame or CRS

**Statement.** A window on clock C lists only intervals on C: records' world time and series
files' known ticks. An interval on another clock is compared with it only when the request names
that clock and the `ClockMapping` records that join it to C, and then it is carried through a
usable mapping path and keeps its own ticks. A box names its frame (`FrameRef`) or CRS and its
unit. Extents are compared only within that reference and unit; nothing is converted, reprojected
or given a default frame. Both indexes are written at registration and reproduced by a rebuild
([ADR 0015](adr/0015-time-and-spatial-indexes-per-clock-and-per-reference.md)).

**Held by.** `tests/test_ledger_time_space_index.py`:
`test_naming_the_gps_clock_without_a_mapping_is_refused`,
`test_each_clock_is_its_own_index_and_gps_time_is_never_compared_with_boot_time`,
`test_a_named_mapping_carries_gps_intervals_onto_the_boot_clock`,
`test_a_frame_of_another_graph_is_another_frame`, `test_crs_codes_are_compared_verbatim` and
`test_a_rebuild_reproduces_both_indexes`. `tests/test_ledger_index_scale.py` holds that building
them is linear and that four times the index costs a lookup at most about twice the pages.

## 4. Cited bytes are served verified, and media are derivatives that replace nothing

**Statement.** An evidence reference resolves to the source's bytes through a registered
package: its blob, or a stated location under an ingest root the deployment names. Every byte
returned is hashed, chunk by chunk, against the chunk ids the package's `source_artifact`
states, so a moved, removed, resized or same-size edited source is a finding (`file_missing`,
`file_digest_mismatch`), never served. A read touches only the chunks it overlaps. Frames,
image regions, pages, rows and values are extracted on request into the tenant's Lance media
table, each row naming its evidence reference and its transform (decoder, decoder version,
library versions). Two hydrations of one reference give equal rows and byte-identical
artefacts, and a pinned media snapshot returns what it returned before. Hostile input (links,
escaping paths, truncation, decompression and pixel bombs, aliases, forged Parquet footers) is a
finding; a Parquet row is decoded only in a child process under an OS memory cap
([ADR 0014](adr/0014-lance-media-store-and-evidence-resolution-to-bytes.md)).

**Held by.** `tests/test_ledger_media.py`:
`test_every_worked_example_citation_resolves_to_its_bytes` resolves every citation of the four
worked examples to its exact bytes and hydrates their pointers and rows;
`test_a_frame_from_a_referenced_mcap_and_from_a_materialised_one`,
`test_frames_hydrate_from_the_compilers_own_message_citations` (a package `neptune ingest` wrote),
`test_documents_past_the_limit_are_refused_and_parsing_costs_no_tree`,
`test_deep_documents_and_long_yaml_walks_are_refused`,
`test_parquet_guards_hold_before_any_page_is_decoded`,
`test_parquet_dictionary_and_run_length_values_decode_one_row_not_one_batch`,
`test_parquet_delta_pages_decode_and_their_shared_prefixes_stay_bounded`,
`test_parquet_forged_footers_are_bounded_by_the_decoding_process_cap`,
`test_parquet_rows_resolve_only_in_a_capped_child_process`,
`test_two_hydrations_are_byte_identical`, `test_a_snapshot_pins_what_a_hydration_returns`,
`test_hydration_is_lazy_and_video_bytes_slice_by_range`,
`test_a_moved_source_resolves_to_a_finding_not_a_crash`, `test_a_changed_source_is_never_served`
and the hostile-input cases in the same file.

## Scale

The catalog's query budget (ADR 0005 §5) re-measured with every record's stored body (ADR 0009 §1)
and the derived thread index (ADR 0010), as of MVL-94. `tests/ledger_catalog_scale.py` at the full
scale, on the same 20-core, 30 GB workstation shared with other jobs, stock `pgserver` PostgreSQL
16.2 settings. Each body is sized to the worked examples' mean line for its kind and built from
hex digests.

- **Catalog:** 100 000 packages, 11 316 720 records, every one with a body (mean 1 738 bytes of
  `jsonb` text), 500 000 thread members. 42.5 GB on disk. The record partitions and their indexes
  take 40.7 GB, against 16.5 GB without bodies at the L1 gate: 2.5×, more than the RAM, so most
  reads are cold. Loading took 974 s for records and 210 s for indexes.
- **Host:** load average 6 to 7 from other agents' test runs during the measurement; the gate's
  run had a quieter host.

| Query (what serves it) | Rows | p50 ms | p95 ms | max ms | L1 p95 | Budget | |
|---|---|---|---|---|---|---|---|
| `thread`, typical machine, **thread index** (what `thread` runs, ADR 0010 §4) | 235–250 | 7.7 | **44.1** | 218.7 | — | p95 < 50 | holds |
| `thread`, workhorse machine, thread index | 20 000 | 89.1 | **97.6** | 105.1 | — | < 500 | holds |
| declared machine key through `record_logical_id` → `record` (the L1 path) | 235–250 | 20.1 | **56.0** | 90.8 | 15.3 | p95 < 50 | **over** |
| declared sensor key, same path | 47–50 | 13.8 | **21.6** | 32.3 | 11.2 | p95 < 50 | holds |
| anchored key | 1 | 0.70 | **1.8** | 5.0 | 0.58 | p95 < 50 | holds |
| workhorse through `record` (the L1 path) | 20 000 | 189 | **207** | 223 | 195 | < 500 | holds |
| lineage set | 40–120 | 0.84 | 2.3 | 3.6 | 0.81 | — | |
| window, typical clock | 41–121 | 0.99 | **2.1** | 4.2 | 0.56 | p95 < 200 | holds |
| window, long recording | 3 001 | 9.7 | **11.9** | 11.9 | 9.1 | p95 < 200 | holds |
| `query` page, keyset cursor | 1 000 | 0.67 | 1.7 | 2.4 | 1.9 | — | |
| package lookup by id | 1 | 0.12 | 0.73 | 1.2 | 0.09 | — | |

| Registration writes at 10⁵ packages (median of 5, rolled back) | ms | L1 |
|---|---|---|
| log row + package row (0002's triggers) | 0.31 | 0.58 |
| 108 record rows of one package, with bodies | 103 | 56.5 |

**Reading.** Every query `thread`, `query` and registration run today meets its budget at the
full scale with bodies. The workhorse thread now takes 98 ms through the thread index, against
195 ms through `record` at the gate. The one miss is the L1 gate's declared-key join through
`record`. It now reads a heap 2.5 times larger, mostly from disk. No API call runs it since
`thread` reads the thread index (ADR 0010 §4), but MVL-98's `query` must not use it as a thread
path. Bodies double a package's record-insert time, which is still independent of catalog size.
Revisit under ADR 0009's trigger (bodies as a storage problem at 10⁶ packages), or if a read
path has to join `record` per thread entry.

The harness's `slow` test runs the same build at 10³ packages, bodies included. It asserts that
every measured query uses its index.
