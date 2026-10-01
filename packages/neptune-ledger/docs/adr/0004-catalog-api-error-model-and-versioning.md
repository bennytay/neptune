# 0004 — Catalog API: call names, error model, absence, versioning and contract tests

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-88

## Context

Memory, Context, Deploy and Learn start building against the catalog API before it is implemented
(MVL-90, MVL-92, MVL-98). ADR 0002 fixes the catalog's tables and requires conflicting ids to be
refused as findings. ADR 0003 fixes thread semantics but leaves the names of `history`, `current`
and `threads_of` to this issue. Neither says how a call reports a problem, how missingness reaches
the wire, how the API is versioned in the Platform registry (platform ADR 0002), or how the
contract tests run before an implementation exists. If these are wrong, consumers write code
against exceptions that never come, read a `null` as a fact, or pin a version that changes under
them.

## Decision

1. **Calls.** `neptune_ledger.api.CatalogApi` is a synchronous protocol for one tenant with seven
   calls: `register(package_root)`, `verify(package_id)`, `resolve(evidence_ref)`,
   `thread(key, order, preference)`, `threads_of(record_id)`, `lineage(record_id)` and
   `query(spec)`. ADR 0003's `history(thread, order)` is `thread(key, order, History())`, and its
   `current(thread, preference, order)` is `thread(key, order, preference)` with a
   `LatestTransform`, `Pinned` or `AsRegisteredBy` preference. `preference` and `order` have no
   default. Every read call (`verify`, `resolve`, `thread`, `threads_of`, `lineage`, `query`) takes
   an optional `as_of` (`tx_seq`). When it is omitted they use the latest committed point, and
   every read response returns the point it used. An `as_of` beyond that point is refused with
   `as_of_out_of_range`. In a current view, a lineage set that resolves to `Ambiguous` or
   `NotCovered` selects no records; only `Known` sets contribute entries. Each call has a request
   record, and `call(api, request)` dispatches a request to its call.
2. **Error model.** No call raises because of evidence or arguments. Each call returns its typed
   response, and problems are `CatalogFinding(code, subject, detail)` entries in its `findings`:
   an unknown package, a tampered manifest or file, unresolvable evidence, a conflicting id, a
   missing preference, a mapping out of range, an invalid window. A rejected call returns its
   response with the payload empty: every collection empty, and the Knowledge fields that the
   failure makes unavailable set to `Unknown`, `NotCovered` or `NotApplicable`. Its status field
   (`outcome`, `verdict`, `status`) names the failure. An unknown thread key is not an error; the
   thread is empty. A `mapping_out_of_range` finding lists the paths it tried in `paths_tried`
   (`MappingPath`s of `ClockMapping` ids, ADR 0003 §3.4); `detail` is never parsed. `query` carries
   its findings in the result's Arrow schema metadata
   (`QueryMeta`). The only exception a conforming implementation raises is `CatalogUnavailable`:
   the store is unreachable, or a transaction kept failing after its retries. A call that raised
   it wrote nothing.
3. **Absence.** `Knowledge[T]`, in the compiler's JSON shape, is used where the catalog or the
   evidence may not know. Two kinds:
   - The Ledger's own determinations (a lineage set's resolution, an entry's `world`, a
     registration key) carry no provenance, and `KnownAbsent` never appears.
   - A field that restates a package field (`WorldTime.end`, marked `as_stated`) is the package's
     state verbatim: `Unknown`, `NotCovered` and `KnownAbsent` stay distinct, the package's
     provenance is kept (`StatedProvenance`), and a timestamp is a `TimePoint` carrying its own
     `domain_id`, so an end on another clock is returned `Known` as stated, never dropped or
     converted. Whether the end is open is derived from it (`WorldTime.closed_end`).

   `X | None` means only that the field does not apply to this request or response shape, for
   example no merge was asked for or an entry is outside a merged partition. Such a key is omitted
   from JSON. There is no `null`, so every document is canonical JSON (root ADR 0002).
   **Exception:** a `query` result's Arrow columns are the catalog's nullable index columns
   (ADR 0002 §5), so `QueryRow`'s optional fields are NULL in Arrow and omitted in JSON. There a
   NULL means "not Known in the record" (for `world_last`: the end is open), never "absent in the
   world"; the package keeps the state, and `thread` returns it. Each record's JSON
   Schema and its strict decoder are generated from its type hints, so they cannot drift from the
   types. Unknown keys are errors.
4. **Registration transaction.** One transaction writes the registration-log row, the package row
   and every index row, or writes nothing. It locks `tx_clock` before looking the package up and
   holds the lock until commit, so READ COMMITTED is enough. An implementation that runs at
   REPEATABLE READ or SERIALIZABLE retries the whole transaction on SQLSTATE 40001 or 40P01. That is
   safe, because a retry either registers the package or finds it already registered. An identical
   re-registration returns `already_registered` with the stored ids, locator and Ledger version,
   allocates no tick and writes nothing. A conflicting id gives `refused` with a `conflicting_id`
   finding (ADR 0002 §6). `root_locator` is the absolute, resolved path of the package root.
5. **Versioning.** `CATALOG_API_VERSION` equals the registry version exactly (platform ADR 0002
   §3). The first export is **1.0.0** and not 0.1.0. The registry treats a version as a minor or
   patch only if it accepts every earlier golden of its major. 0.1.0 would have to accept the
   Platform draft's 0.0.0 goldens, which use different shapes (`CatalogPackage`, `CurrentView`).
   It is published with `status = "draft"` while the L1 gate (MVL-89) is open, so no consumer is
   required to lock it. Once the gate passes, the next version is published as `stable`, because
   the tool cannot promote a version in place. A reader-incompatible change raises the major.
   Every response's `api_version` accepts any `1.x.y`, so a minor bump keeps earlier goldens valid.
6. **Contract tests.** `neptune_ledger.contract_tests.CatalogContract` is a pytest base class. An
   implementation subclasses it as `Test…` and returns a fresh catalog from `make_catalog`. Its
   tests are golden calls over the compiler's four worked-example packages, the error cases and
   determinism checks. They take their expectations from the package bytes by ADR 0002 and ADR 0003
   rules, never from an implementation. The Ledger runs the suite against `StubCatalog` with
   `expected_failure = NotImplementedError`. Each test calling the API is marked
   `xfail(strict=True, raises=NotImplementedError)`, so another exception or a pass turns CI red.
   Tests that need no implementation (schema, codec, thread ids, goldens) are ordinary tests.
   Current-view, conflict and `as_of` cases use synthetic packages that the suite builds
   deterministically from the drone example with the compiler's own id rules: the same sources
   re-identified under ulog 1.0.0 with another config and under 2.0.0 (lineage siblings), and a
   copy whose source artifact states another size (a conflicting id). Clock-merge and
   `mapping_out_of_range` tests are deferred to MVL-92: they need MVL-82 `ClockMapping` records,
   which no package can carry yet.

## Alternatives considered

- **Exceptions per error (`UnknownPackage`, `TamperedManifest`).** Rejected: they are invisible in
  the return type, they cannot travel over JSON without a second schema, and partial results
  (a damaged package with four findings) would need an exception that carries a report anyway.
- **A `Result[T, Error]` union per call.** Rejected: every caller must unwrap even when the call
  succeeds with warnings (`mapping_out_of_range` alongside a valid thread), and a failure would
  still need a typed payload.
- **Separate `history` and `current` calls.** Rejected: the issue fixes `thread(key, order,
  preference)`, and one call keeps the arguments, response and ordering contract identical. The
  view is chosen explicitly, and `History()` is a required argument, not a default.
- **`null` for optional fields.** Rejected: the compiler's canonical JSON has no `null`. A null
  invites reading a blank as a fact. Omitting a key is unambiguous, because only `X | None` fields
  may be omitted.
- **Publish as 0.1.0 and keep the draft's shapes in the schema so its goldens still validate.**
  Rejected: it would ship a `CurrentView` with one `current` record, which contradicts ADR 0003's
  per-lineage-set resolution.
- **Mark the stub's contract tests skipped.** Rejected: skips prove nothing. Strict expected
  failures prove that each test calls the API and that nothing else is broken.

## Consequences

- Consumers code against the response types and `findings`, and handle one exception type. Adding
  a finding code or an optional field is a minor version. Removing or renaming one is a major.
- `query` results are byte-stable only if implementations build them with `query_table`. The
  contract test compares Arrow IPC bytes.
- The contract tests need the compiler's worked examples, found in this repository or at
  `$NEPTUNE_WORKED_EXAMPLES`, plus pytest and jsonschema (the `contract-tests` extra).
- Revisit if a consumer needs an asynchronous or multi-tenant surface (MVL-99, `access/`), or if
  external-object sources (`ExternalObjectRef`) must be resolvable. v1 resolves content-id
  sources only.
