# 0012 — Rebuild from packages: the registry manifest, a canonical dump, and a one-transaction replay

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-94
- Amends: ADR 0002 §4 (the registration log also lives in a file, and how a rebuild runs);
  ADR 0005 §5 and ADR 0009's consequences (the scale budget re-measured with stored bodies and
  the thread index). The guarantee itself is [guarantees.md](../guarantees.md).

## Context

ADR 0002 §4 promises that the same Ledger version, the packages and the registration log give a
byte-identical catalog, transaction times included. ADRs 0004, 0007, 0008, 0009, 0010 and 0011
each lean on that promise: a migration that cannot backfill refuses a populated catalog and says
"rebuild", and a projection-mapping change says the same. Nothing implemented the rebuild, and
nothing checked the promise. Three gaps:

- **The log lives only in the catalog it is meant to rebuild.** A dropped or damaged schema takes
  the registration log with it, so "packages plus log" needs the log outside the database.
- **Nothing compares two catalogs.** ADR 0009 §4 claims registration order changes only the
  registration keys and transaction columns. The tests checked it with a private dump helper on
  the four worked examples, so no command could show that a rebuilt catalog equals its original.
- **A rebuild that fails halfway loses the catalog.** Dropping the schema and re-registering one
  package at a time leaves a partial catalog when one package has moved or been damaged since.

MVL-94's own wording leaves two points open. It asks for "a separate `audit` table" holding
transaction timestamps, and for a rebuild "in sorted order" after a registration in random
order. The guarantee must say what is compared, and in which order a rebuild registers.

## Decision

1. **The registry manifest is the registration log as a file.** It is one canonical JSON
   document (root ADR 0002) plus a newline:
   `{format: "neptune-ledger/registry-manifest", format_version: 1, tenant_id, registrations}`.
   `registrations` lists every `registration_log` row in `tx_seq` order: `tx_seq`, `tx_time`,
   `package_id`, `root_locator` and `ledger_version`. It is a function of the log alone, so a
   catalog always writes the same bytes.
   - `PostgresCatalog(manifest=path)` rewrites it after every registration that is not
     refused. `already_registered` rewrites it too, so registering a package again repairs a
     manifest that an earlier failure left stale. The writer takes a per-tenant transaction
     advisory lock and reads the log after taking it. While still holding the lock, it replaces
     the file atomically and durably: a new `O_EXCL` sibling, fsync, rename, then fsync of the
     directory. So after concurrent registrations, the last writer has read every committed row.
     A failed write raises `ManifestNotWritten`, which carries the committed registration; the
     CLI prints that registration and exits 1. `ledger manifest` also rewrites the file from the
     catalog.
   - `Manifest.from_bytes` treats the file as hostile. It accepts only canonical bytes of exactly
     that shape: `tx_seq` rising, `tx_time` never falling, each package once, roots absolute and
     free of NUL, and every value in the shape its log column requires. Anything else is
     `ManifestError`.
2. **A rebuild replays the manifest in one transaction.** `rebuild(conninfo, manifest,
   package_roots=…)`, or `ledger rebuild --from <manifest>`, opens one transaction. It takes the
   tenant's migration lock, drops `tenant_<id>` with `CASCADE`, applies every migration, and then
   replays each entry in `tx_seq` order through registration from its logged root.
   `PostgresCatalog.replay` advances the clock with `replay_tx(tx_seq, tx_time)` instead of
   `next_tx`, so the log, the package rows, every registration key and the thread index carry
   the original keys. It also checks the tenant's package roots (ADR 0006 §3), because a manifest
   is input. A logged root that now resolves to another path (a link put on one of its
   directories) is refused as `package_unreadable`, because the rebuilt log would record another
   root. A logged root that now holds another package, however intact, is refused as
   `manifest_digest_mismatch`: replay passes the entry's `package_id` and compares it with the
   hash of the `manifest.json` it verified, so no other package takes the logged tick. The catalog borrows the rebuild's connection and writes each registration in a
   savepoint. If any entry is not `registered`, the whole transaction rolls back and the old
   catalog stands. The report names the entry and its registration's findings. Readers block on
   the dropped schema's locks until commit, so they never see a partial catalog.
   - **Same Ledger version, same catalog.** If every entry names the running Ledger version, the
     result is the original catalog byte for byte, transaction times included: ADR 0002 §4's
     guarantee. If an entry names another version, the rebuild would be a new catalog lineage
     over the same transaction keys (ADR 0002 §4, ADR 0009 §6). Its log would record the running
     version, while `as_of` points held by consumers keep their meaning. Such a rebuild is
     refused, with the other versions listed, unless `new_lineage` (`--new-lineage`) is given.
     The old manifest is the only record of the old lineage, so keep it.
   - **A stale manifest drops nothing by accident.** Before dropping, the rebuild locks the clock
     row, as every registration does. A registration already in flight commits first, and no new
     one starts until the rebuild ends. The rebuild then reads the existing log. A registered
     package that the manifest leaves out refuses the rebuild and is listed in the report, unless
     `prune` (`--prune`) is given. With `prune`, this check is skipped, so a catalog too damaged
     to read can still be replaced.
3. **The dump is canonical and leaves out registration order.** `dump(conninfo, tenant, out)`,
   or `ledger dump`, first takes the tenant's migration lock in shared mode. A rebuild or a
   migration in progress therefore finishes before the dump takes its snapshot; otherwise an old
   snapshot could read a rebuilt schema as empty. It then reads one `REPEATABLE READ READ ONLY`
   snapshot. It sets `bytea_output`, `extra_float_digits` and `TimeZone`, so text forms do not
   depend on session defaults. It writes JSON Lines, and `--out` replaces its file only with a
   complete dump:
   - a header `{format: "neptune-ledger/catalog-dump", format_version: 1, left_out}`;
   - per table, in byte order of name (partitioned `record` as one table): `{columns, table}`
     with the kept columns in byte order, then one object per row mapping each column to its
     PostgreSQL text form. A NULL column is absent, because canonical JSON has no null. Rows are
     ordered by their column texts under `COLLATE "C"`.
   - **Left out:** `tenant_id`, constant within a schema, and every transaction column:
     `tx_seq`, `tx_time`, `registration_key`, `first_registration_key`, `last_seq` and
     `last_time`. The set is held by name. A schema test fails when a migration adds a column of
     the `tx_time` domain, or one named for a sequence or registration key, that is not in it.
     These are the values that say when, and in which order, the Ledger learned of each package.
     Each one follows from the manifest, because a row's registration key is its package's
     `tx_seq` (ADR 0002 §4's foreign key). So the dump and the manifest together determine the
     catalog. Two catalogs of the same packages under the same Ledger version dump to the same
     bytes, whatever order and tenant they were registered in.
4. **No `audit` table.** The issue's "transaction timestamps in a separate `audit` table" is met
   by §3's column set, not by a migration. ADR 0002 §4 already confines transaction time to
   `registration_log`, `package` and `tx_clock`. Moving those columns out would be a migration
   that cannot apply to a populated catalog (`package_from_log`'s foreign key and trigger, and
   append-only tables), and it would change no read.
5. **The guarantee is tested in the package's CI job.** `tests/test_ledger_rebuild.py` holds the
   tests. CI's `neptune-ledger` job runs them with `make check` (no workflow change; a
   `packages/neptune-ledger/**` change selects the job). The fixture set is the four contract
   examples, the schema-4 `manipulator_cell` (lifecycle kinds), and three drone variants: adapter
   v2 lineage siblings, a moved source with its absence, and another chunk size.
   - *Acceptance* (two fixed seeds): register through `ledger register --manifest` in a seeded
     shuffle and `ledger dump`. Then `ledger rebuild --from` the manifest (its order is the
     shuffle; "sorted" means sorted by `tx_seq`) and `ledger dump` again. The dumps are
     byte-identical, the rewritten manifest equals the original, and every table, transaction
     columns included, is unchanged.
   - *Order independence:* the shuffle in one database and tenant, and package-id order in
     another, give byte-identical dumps. This is the issue's "random order versus sorted order"
     in the strong form.
   - Refusal rolls back, roots outside the tenant's roots, stale manifests with and without
     prune, live ticks continuing after a replay, a new-version lineage, a rebuild into a new
     tenant, the CLI's tenant and usage checks, and twenty-one malformed manifests.
6. **Scale, re-measured with bodies** (ADR 0005 §5, ADR 0009's and ADR 0010's consequences).
   `tests/ledger_catalog_scale.py` now writes `record.body` for every row, as registration does.
   Each body is sized to the worked examples' mean canonical line for its kind (stream 2 260
   bytes, calibration 2 140, run 1 340, and so on) and built from hex digests, so it compresses
   about as little as real bodies. `--no-bodies` reproduces the L1 gate's catalog. Results at
   10⁵ packages and 11.3 M records, full tables in [guarantees.md](../guarantees.md#scale):
   - The record partitions take 40.7 GB, against 16.5 GB without bodies (2.5×, more than RAM).
   - Every path an API call runs today holds its budget. `thread` through the thread index has
     p95 44.1 ms for a typical machine and 97.6 ms for the 20 000-entry workhorse. Windows are
     2.1 ms (11.9 ms on a dense clock), and a keyset page is 1.7 ms.
   - Registration writes stay independent of catalog size: 0.31 ms for log and package. A
     package's 108 record rows now take 103 ms with bodies, against 56.5 ms without.
   - The L1 gate's declared-key join through `record` now has p95 56.0 ms, over its 50 ms
     budget, because it reads the larger heap from disk. Since ADR 0010, `thread` no longer runs
     it. **Rule:** no read path joins `record` once per thread entry. MVL-98's `query` serves
     threads from the thread index, and `record` only for row pages and windows.

## Alternatives considered

- **A manifest of package ids and roots sorted by package id, with a fresh clock on rebuild.**
  Rejected. It makes the manifest independent of registration order, but a rebuild would renumber
  every registration key. Consumers' `as_of` points would then name other catalog states, and ADR
  0002 §4's guarantee, transaction times included, could never be shown. §3's dump gives order
  independence without discarding the order.
- **Keep the manifest only in the database.** Rejected. A rebuild exists for when the schema is
  gone or wrong.
- **Rebuild one package per transaction, or into a staging schema and rename it.** Rejected. Per
  package leaves a partial catalog on the first refusal. A staging schema cannot become the
  tenant's: every row's `tenant_id` and the schema name are bound to the tenant (ADR 0002 §2),
  and a rename would leave the old schema's grants behind.
- **Compare catalogs with `pg_dump`.** Rejected. Its output depends on server version, OIDs and
  object order, it includes transaction columns, and it cannot leave out `tenant_id`.
- **Dump rows as arrays with `null` for NULL.** Rejected. Canonical JSON (root ADR 0002) has no
  null. Objects without the NULL column are unambiguous, because every present value is text.
- **A new CI workflow job for the rebuild test.** Rejected. The package job already runs it on
  every change that can break it, and a separate job would duplicate the server start and setup.

## Consequences

- An operator can always recover a catalog from the manifest and the packages. Upgrading the
  Ledger is a rebuild with the new version, and a migration that refuses a populated catalog now
  has a supported path.
- Registration with a manifest writes one file per added package. The file grows with the log:
  about 250 bytes per package, or 25 MB at 10⁵ packages, rewritten whole. Revisit, with an
  append-only form, when registration rates make that rewrite visible.
- A rebuild holds every table's lock for its duration and re-verifies every package; it is an
  offline operation for one tenant. A rebuild drops grants on the schema, which `access/` (MVL-99)
  must reapply.
- Which of two conflicting packages is refused depends on registration order (ADR 0005 §2), so
  the order-independence of §3 holds for sets of packages that register without conflict.
  Refused packages are never in the log, so a rebuild replays the same outcome.
- `ledger dump` sorts every table in the database and writes every body, so it is a test and
  audit tool, not a backup format.
