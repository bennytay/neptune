# Catalog API

The contract that Memory, Context, Deploy and Learn build against. This page is the reference for
each call. The decisions behind it are in [ADR 0002](adr/0002-catalog-data-model.md) (tables,
registration), [ADR 0003](adr/0003-entity-threads-and-the-lineage-current-view.md) (threads),
[ADR 0004](adr/0004-catalog-api-error-model-and-versioning.md) (names, errors, versioning) and
the L1 gate's amendments, [ADR 0005](adr/0005-l1-gate-catalog-data-model-amendments.md) (record
bodies, clock checks, collation, scale) and
[ADR 0006](adr/0006-l1-gate-catalog-api-amendments.md) (hostile packages, moved packages and
sources, tenant roots, paging, merge order). The gate's review is
[reviews/l1-stress-test.md](reviews/l1-stress-test.md).

- **Version:** `1.4.0`, `neptune_ledger.api.CATALOG_API_VERSION`, **stable**, in
  `contracts/catalog-api/v1.4.0/`. 1.0.0 was the pre-gate draft; 1.1.0 adds the `unsafe_entry`
  finding, the `unreachable` verdict and `QuerySpec.after`, and accepts every 1.0.0 document;
  1.2.0 adds package schema 2's `configuration_snapshot` and `configuration_value` record kinds
  (root ADR 0037) and accepts every 1.1.0 document; 1.3.0 adds package schema 3's alignment
  record kinds (root ADR 0050) and accepts every 1.2.0 document; 1.4.0 stops listing record
  kinds and references the package-schema contract for them instead (below, [ADR
  0011](adr/0011-schema-version-registry-and-record-kinds-by-package-schema-version.md)), and
  accepts every 1.3.0 document. A package-schema version that adds kinds changes no catalog-api
  version.
- **Code:** `neptune_ledger.api`. It holds the `CatalogApi` protocol, the request and response
  records, `catalog_schema()` (JSON Schema 2020-12, one `$defs` entry per record),
  `QUERY_RESULT_SCHEMA` (Arrow), `to_json` / `from_json` / `dumps` / `loads`, and `StubCatalog`.
- **Contract tests:** `neptune_ledger.contract_tests.CatalogContract`.

## Rules every call follows

- **Errors are findings.** Every call returns its typed response. A problem with the evidence or
  the arguments is a `CatalogFinding(code, subject, detail)` in `findings`, never an exception. A
  rejected call returns an empty payload, and its status field names the failure. The only
  exception is `CatalogUnavailable`, raised when the store cannot answer; a call that raised it
  wrote nothing.
- **Missingness is explicit.** `Knowledge[T]` is used where the catalog may not know. It takes the
  compiler's JSON shape. The Ledger's own determinations carry no provenance and are never
  `known_absent`. A field that restates a package field (`WorldTime.end`) is the package's state
  verbatim, provenance included, with timestamps as `TimePoint`s that carry their own clock. A
  field that does not apply to a shape is omitted from JSON. There is no `null`, except in
  `query` results (below).
- **Replayable reads.** Every read call is evaluated at a catalog point. Pass `as_of`, a `tx_seq`;
  without it, the latest committed point is used. Either way the response returns the point as a
  `TransactionKey`. The same call at the same point gives byte-identical canonical JSON, or
  byte-identical Arrow IPC for `query`.
- **One tenant per catalog object.** Nothing crosses tenants (ADR 0002 §2).
- **Record kinds come from the package-schema contract.** A `RecordKind` is any table name
  (`^[a-z][a-z0-9_]*$`) in the JSON Schema; which kinds exist is the package-schema contract's
  business (`contracts/package-schema/v<N>.x.y/`), at the schema version a package declares
  (`Registration.schema_version`). The Ledger checks kinds against that version where they
  enter the catalog: a package must hold exactly its version's tables (`manifest_invalid`), and
  its version must be one the Ledger's schema-version registry holds
  (`unsupported_schema_version`, so a package from a future version is refused). Readers should
  accept a kind they do not know: it is a kind of a newer package-schema version.

## Calls

| Call | Returns | Guarantees | Never |
|---|---|---|---|
| `register(package_root)` | `Registration` | First verifies the whole package at `package_root`, outside any transaction: no symlink or special file anywhere under it, every listed file present with its size and sha256, a table for every record kind, every record readable. Only an intact package goes on. One transaction then writes the log row, the package row and every index row, or nothing. It locks `tx_clock` before the lookup, so READ COMMITTED suffices; at stricter isolation it retries on 40001/40P01. An identical re-registration returns `already_registered` with the stored ids and locator and allocates no tick; because the bytes were verified first, it also certifies that `package_root` holds the registered package. `refused` writes nothing. `package_root` must lie under the tenant's own package roots, decided on the fully resolved path by path components (ADR 0006 §3); any other root is `package_unreadable`, exactly as if it did not exist. | Edits, normalises or copies package bytes. Follows a symlink. Keeps the first value of a conflicting id, including a record id that arrives with another body (except a `source_artifact`, whose chunking may differ; its conflict is another size) (that is `refused` + `conflicting_id`). Records a second root locator. |
| `verify(package_id)` | `VerifyReport` | Re-hashes `manifest.json` against the id and every listed file against the manifest, at the stored root locator, with the same entry checks as `register`. Verdict `intact`, `damaged` (every mismatch listed), `unreachable` (the stored root cannot be read: nothing compared, one `package_unreadable` finding naming it) or `unknown_package` (also for a package registered after `as_of`). Returns the `as_of` it used. A package that moved is checked at its new root with `register(new_root)`. | Repairs, re-registers or changes the catalog. Reports a moved package as `damaged`. |
| `resolve(evidence_ref)` | `Resolution` | For an `EvidenceAnchor(source, locator)`: the source's size, the innermost locator step (`region`), every registered package's route to the bytes (`fetch`, in registration order: the package-relative blob path when materialised; when referenced, the locations that package says hold the bytes, leaving out a revision that another revision or an absence in the same package supersedes), and every record whose record-level anchor equals it exactly (`cited_by`). | Fetches bytes, follows a location, or matches locators other than by exact canonical JSON. |
| `thread(key, order, preference)` | `Thread` | ADR 0003. `History()` returns every entry and leaves lineage sets `NotApplicable`. `LatestTransform()`, `Pinned(t)` or `AsRegisteredBy(p)` returns the current view: one transform per lineage set, resolved to `Known`, `Ambiguous` or `NotCovered`; only `Known` sets contribute entries. An entry's `world` has a `TimePoint` start and its end exactly as the package states it (open unless `Known` on the start's clock). `world` order gives per-clock partitions with the untimed partition last. `transaction` order gives one partition. Optional `merge` takes a reference clock and `ClockMapping` ids. | Defaults the preference (a missing one gives `preference_required`). Unions threads. Relates clocks without named mappings. Converts ticks. Orders by wall clock. |
| `threads_of(record_id)` | `ThreadsOf` | Every thread the record is a member of, per registering package, with its roles, plus the threads an `Ambiguous` field names (`unresolved`). | Merges co-declared keys into one thread. |
| `lineage(record_id)` | `LineageGraph` | The record's kind and transform, every package holding it, the transform DAG upstream of it (an unregistered upstream is a node with `NotCovered` info), and lineage siblings (same kind and anchor, other transforms). | Picks a "current" transform; that is `thread` with a preference. |
| `query(spec)` | `pyarrow.Table` | Columns are exactly `QUERY_RESULT_SCHEMA` (`QueryRow`): the catalog's nullable index columns (ADR 0002 §5), where NULL means "not Known in the record" (`world_last`: the end is open), never "absent". Metadata `neptune.catalog_api` holds `QueryMeta` (`as_of`, findings). Filters on kinds (required; a kind no package-schema version the Ledger reads declares is `invalid_request`), a `TimeWindow` on one clock (inclusive; records without world time never match), a `thread_id` (history entries) and packages, combined with AND. Rows are sorted by `(kind, record_id, package_id)` as UTF-8 bytes; `after` (a `QueryCursor`, 1.1.0) keeps the rows strictly after it and `limit` the first rows, so the last row of a page is the next page's cursor. | Accepts SQL. Applies a window across clocks. Returns record bodies (read the package). |

ADR 0003's `history(thread, order)` is `thread(key, order, History())`, and its
`current(thread, preference, order)` is `thread(key, order, preference)`.

## What the API never does

- **Merge.** It never merges identities, threads, clocks or packages. Two identical URDFs are two
  packages, and co-declared keys are two threads. An `IdentityLink` is reported as an edge.
- **Mutate.** It never edits a package or a catalog row. Registration only appends, and
  supersession is computed when the catalog is read (ADR 0003 §5).
- **Infer.** It never stores or returns an inferred meaning. Membership comes only from `Known`
  values with stated or observed provenance. Latest transform means SemVer precedence on
  comparable chains, never the order of registration.

## Finding codes

| Code | Raised by | Meaning |
|---|---|---|
| `package_unreadable` | register, verify | The root is missing, not a directory, a symlink, outside the tenant's package roots, or its manifest cannot be read. From verify: the stored root, with verdict `unreachable` |
| `manifest_invalid` | register | `manifest.json` is not a package manifest, including one that does not count a table for every record kind |
| `unsafe_entry` | register, verify | An entry under the root is a symlink, FIFO, socket or device (subject: package-relative path). It is never followed or opened (1.1.0) |
| `file_missing`, `unexpected_file` | register, verify | A listed file is absent (a deleted record table is `file_missing`), or a file the manifest does not list is present |
| `file_digest_mismatch` | register, verify | A file's size or sha256 differs from the manifest (subject: package-relative path) |
| `manifest_digest_mismatch` | verify | `manifest.json` at the stored root no longer hashes to the package id |
| `unsupported_schema_version` | register | The manifest declares a package-schema version the Ledger's schema-version registry does not hold (a future version is refused, never guessed at), or the catalog indexed that version with another projection mapping and must be rebuilt (ADR 0011) |
| `record_invalid` | register | A record fails the package-schema readers, or states a schema version newer than its package's |
| `conflicting_id` | register | An existing source or transform id arrives with different fields (ADR 0002 §6), or an existing record id (clocks included) with another body; for `source_artifact`, another size only, since chunking is not identity (ADR 0005 §2) |
| `unknown_package` | verify | The tenant never registered the id |
| `unknown_record` | lineage, threads_of | No registered package holds the record id |
| `unresolvable_evidence` | resolve | No registered package holds the source |
| `preference_required` | thread | The preference was missing |
| `unknown_clock`, `unknown_mapping` | thread | The merge names a clock or mapping that the catalog does not hold |
| `unsupported_mapping`, `mapping_out_of_range` | thread | ADR 0003 §3: a mapping that is not usable, or an entry that no usable path covers; `mapping_out_of_range` lists the paths it tried in `paths_tried` |
| `as_of_out_of_range` | read calls | `as_of` is beyond the latest committed point |
| `invalid_request` | any | An argument outside the contract, for example a window with `first > last` |

## Running the contract tests against an implementation

```python
from pathlib import Path
from neptune_ledger.api import CatalogApi
from neptune_ledger.contract_tests import CatalogContract

class TestMyCatalog(CatalogContract):
    def make_catalog(self, workdir: Path) -> CatalogApi:
        return MyCatalog(workdir)  # fresh and empty for every test
```

The suite contains:

- golden calls over the compiler's four worked-example packages (drone, manipulator, mobile
  robot, quadruped);
- current-view resolution over synthetic lineage siblings that the suite builds from the drone:
  `latest_transform` dominance ({v1 cfgA, v1 cfgB, v2} gives `Known(v2)`), `Ambiguous`
  ({v1 cfgA, v1 cfgB}), `pinned` and `as_registered_by`, including their `NotCovered` cases;
- the error cases: unknown package, tampered manifest and record table, refused tampered package,
  `conflicting_id` refusal, unresolvable evidence, missing preference, `as_of_out_of_range` and
  an inverted window;
- the L1 gate's hostile cases (ADR 0006): a symlinked file or directory in a package (refused,
  never followed), a deleted table, a manifest that omits a table, a damaged copy of a
  registered package, a record id with another body, a source that moved (another package, and
  `resolve` routes each package to its own locations), a package that moved (`unreachable`, then
  `already_registered` at the new root), the same bytes at two chunk sizes, tenant-root escapes
  by symlink and `..` (through `make_tenant_catalog`), two timed clocks in one thread with no
  mapping (across packages and within one), and paging `query` by cursor;
- determinism checks: the same call twice gives identical bytes, and `as_of` replays an earlier
  point.

Clock-merge and `mapping_out_of_range` tests are deferred to MVL-92. They need MVL-82
`ClockMapping` records, which no package carries yet. It needs pytest and jsonschema (the `contract-tests` extra). Outside this repository, set
`NEPTUNE_WORKED_EXAMPLES` to the compiler's `tests/fixtures/model`.

This package runs the suite twice (`tests/contract/test_ledger_catalog_contract.py`): against
`StubCatalog` as strict expected failures, where each test must fail with `NotImplementedError`,
and against the real `neptune_ledger.catalog.registry.PostgresCatalog`. There, `register`,
`verify` and `resolve` pass. Each test that reaches a call not implemented yet is listed by name
with its owning issue (`thread`, `threads_of`, `lineage`: MVL-92; `query`: MVL-98)
and is a strict expected failure until that call lands. `make_catalog` must return a catalog
with no package-root limit, because the tests register from `tmp_path`.

Referenced sources are re-hashed on request by `PostgresCatalog.verify_sources`, outside this
contract, because catalog-api has no finding code for a source location
([ADR 0007](adr/0007-registration-implementation-boundaries-and-source-checks.md)). The `ledger`
command (`ledger register <root>`, `ledger verify <id> [--source-root DIR]`) is a thin front end
over it. The registry goldens in
`contracts/catalog-api/v1.4.0/golden/` come from `contract_tests/goldens.py`. They are example
documents valued from the worked examples, with fixed illustrative transaction keys.
