# Catalog API

The contract that Memory, Context, Deploy and Learn build against. This page is the reference for
each call. The decisions behind it are in [ADR 0002](adr/0002-catalog-data-model.md) (tables,
registration), [ADR 0003](adr/0003-entity-threads-and-the-lineage-current-view.md) (threads) and
[ADR 0004](adr/0004-catalog-api-error-model-and-versioning.md) (names, errors, versioning).

- **Version:** `1.0.0`, `neptune_ledger.api.CATALOG_API_VERSION`, published as a draft in
  `contracts/catalog-api/v1.0.0/` until the L1 gate (MVL-89) passes.
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
  compiler's JSON shape, carries no provenance, and is never `known_absent`. A field that does
  not apply to a shape is omitted from JSON. There is no `null`.
- **Replayable reads.** Every read call is evaluated at a catalog point. Pass `as_of`, a `tx_seq`;
  without it, the latest committed point is used. Either way the response returns the point as a
  `TransactionKey`. The same call at the same point gives byte-identical canonical JSON, or
  byte-identical Arrow IPC for `query`.
- **One tenant per catalog object.** Nothing crosses tenants (ADR 0002 §2).

## Calls

| Call | Returns | Guarantees | Never |
|---|---|---|---|
| `register(package_root)` | `Registration` | One transaction writes the log row, the package row and every index row, or nothing. It locks `tx_clock` before the lookup, so READ COMMITTED suffices; at stricter isolation it retries on 40001/40P01. An identical re-registration returns `already_registered` with the stored ids and locator and allocates no tick. `refused` writes nothing. | Edits, normalises or copies package bytes. Keeps the first value of a conflicting id (that is `refused` + `conflicting_id`). Records a second root locator. |
| `verify(package_id)` | `VerifyReport` | Re-hashes `manifest.json` against the id and every listed file against the manifest, at the stored root locator. Verdict `intact`, `damaged` (every mismatch listed) or `unknown_package`. | Repairs, re-registers or changes the catalog. |
| `resolve(evidence_ref)` | `Resolution` | For an `EvidenceAnchor(source, locator)`: the source's size, the innermost locator step (`region`), every registered package's route to the bytes (`fetch`: package-relative blob path when materialised, stated locations when referenced, in registration order), and every record whose record-level anchor equals it exactly (`cited_by`). | Fetches bytes, follows a location, or matches locators other than by exact canonical JSON. |
| `thread(key, order, preference)` | `Thread` | ADR 0003. `History()` returns every entry and leaves lineage sets `NotApplicable`. `LatestTransform()`, `Pinned(t)` or `AsRegisteredBy(p)` returns the current view: one transform per lineage set, resolved to `Known`, `Ambiguous` or `NotCovered`. `world` order gives per-clock partitions with the untimed partition last. `transaction` order gives one partition. Optional `merge` takes a reference clock and `ClockMapping` ids. | Defaults the preference (a missing one gives `preference_required`). Unions threads. Relates clocks without named mappings. Converts ticks. Orders by wall clock. |
| `threads_of(record_id)` | `ThreadsOf` | Every thread the record is a member of, per registering package, with its roles, plus the threads an `Ambiguous` field names (`unresolved`). | Merges co-declared keys into one thread. |
| `lineage(record_id)` | `LineageGraph` | The record's kind and transform, every package holding it, the transform DAG upstream of it (an unregistered upstream is a node with `NotCovered` info), and lineage siblings (same kind and anchor, other transforms). | Picks a "current" transform; that is `thread` with a preference. |
| `query(spec)` | `pyarrow.Table` | Columns are exactly `QUERY_RESULT_SCHEMA` (`QueryRow`). Metadata `neptune.catalog_api` holds `QueryMeta` (`as_of`, findings). Filters on kinds (required), a `TimeWindow` on one clock (inclusive; records without world time never match), a `thread_id` (history entries) and packages, combined with AND. Rows are sorted by `(kind, record_id, package_id)` as UTF-8 bytes. | Accepts SQL. Applies a window across clocks. Returns record bodies (read the package). |

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
| `package_unreadable` | register | The root is missing, not a directory, or a symlink, or its manifest cannot be read |
| `manifest_invalid` | register | `manifest.json` is not a package manifest |
| `file_missing`, `unexpected_file` | register, verify | A listed file is absent, or a file the manifest does not list is present |
| `file_digest_mismatch` | register, verify | A file's size or sha256 differs from the manifest (subject: package-relative path) |
| `manifest_digest_mismatch` | verify | `manifest.json` at the stored root no longer hashes to the package id |
| `unsupported_schema_version`, `record_invalid` | register | The package uses a schema version the Ledger does not read, or a record fails the package-schema readers |
| `conflicting_id` | register | An existing source, transform or clock id arrives with different fields (ADR 0002 §6) |
| `unknown_package` | verify | The tenant never registered the id |
| `unknown_record` | lineage, threads_of | No registered package holds the record id |
| `unresolvable_evidence` | resolve | No registered package holds the source |
| `preference_required` | thread | The preference was missing |
| `unknown_clock`, `unknown_mapping` | thread | The merge names a clock or mapping that the catalog does not hold |
| `unsupported_mapping`, `mapping_out_of_range` | thread | ADR 0003 §3: a mapping that is not usable, or an entry that no usable path covers |
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

The suite contains golden calls over the compiler's four worked-example packages (drone,
manipulator, mobile robot, quadruped), the error cases (unknown package, tampered manifest and
record table, refused tampered package, unresolvable evidence, missing preference, inverted window)
and determinism checks: the same call twice gives identical bytes, and `as_of` replays an earlier
point. It needs pytest and jsonschema (the `contract-tests` extra). Outside this repository, set
`NEPTUNE_WORKED_EXAMPLES` to the compiler's `tests/fixtures/model`.

This package runs the suite against `StubCatalog` as strict expected failures
(`tests/contract/`): each test must fail with `NotImplementedError`. The registry goldens in
`contracts/catalog-api/v1.0.0/golden/` come from `contract_tests/goldens.py`. They are example
documents valued from the worked examples, with fixed illustrative transaction keys.
