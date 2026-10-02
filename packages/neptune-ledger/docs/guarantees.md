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
  given, so a stale manifest cannot drop packages silently.
- Live registration continues after the last replayed tick.

**Limits.**

- *Another Ledger version* re-indexes the same packages over the same transaction keys. That is
  a new catalog lineage, by design (ADR 0002 §4, ADR 0009 §6): its log records the new version,
  and its rows follow the new version's migrations and projections. Upgrading the Ledger is
  exactly this rebuild.
- *A moved or damaged package* refuses the rebuild with that registration's findings
  (`package_unreadable`, `file_digest_mismatch`, …). Restore it, or edit its manifest root to
  the new location. The rebuilt log then records the new root, and nothing else differs.
- *Conflicting packages* (ADR 0005 §2): whichever registered first stands and the other was
  refused, so it is not in the log. Order independence holds for sets of packages that register
  without conflict.
- A rebuild drops the schema's grants; `access/` (MVL-99) reapplies them.

**Held by** `tests/test_ledger_rebuild.py`, run by the `neptune-ledger` CI job (`make check
PKG=neptune-ledger`). The fixtures are the four contract examples (drone, manipulator, mobile
robot, quadruped), the schema-4 `manipulator_cell` with its lifecycle kinds, and three drone
variants: adapter v2 lineage siblings, a moved source with its absence, and another chunk size.

- `test_a_rebuild_from_the_manifest_and_packages_is_byte_identical` (seeds 94 and 2026):
  registers in a seeded shuffle through the CLI and dumps; rebuilds from the manifest and dumps.
  The two dumps are equal bytes, the manifest is rewritten unchanged, and every table, transaction
  columns included, is unchanged.
- `test_the_dump_does_not_depend_on_registration_order_or_tenant`: the shuffle and package-id
  order, in two databases and two tenants, dump to equal bytes.
- `test_every_transaction_column_is_left_out_of_the_dump`: a migration that adds a transaction
  column under a new name fails CI until the dump leaves it out.
- The refusal, prune, root, clock, lineage and malformed-manifest cases in the same file.

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
