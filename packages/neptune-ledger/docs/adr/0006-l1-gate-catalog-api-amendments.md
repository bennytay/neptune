# 0006 — L1 gate: catalog API amendments for hostile packages, moved evidence, tenant roots and paging; catalog-api 1.1.0 stable

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-89
- Amends: ADR 0004 §1 (`verify` verdicts, `query` cursor), §2 (finding codes), §4 (what
  registration checks, and in what order), §5 (the stable version); ADR 0003 §3.5 (what a clock
  merge may reorder). Review: [l1-stress-test.md](../reviews/l1-stress-test.md).

## Context

The L1 gate walked the catalog contract through hostile cases
([review](../reviews/l1-stress-test.md)). Five gaps in ADR 0004 broke a case:

- A **symlink inside a package** has no defined outcome. `package_unreadable` covers only a
  symlinked root, so an implementation could follow `records/run.jsonl` to another tenant's file.
- A **missing table** and a **manifest that omits a table** have no defined finding.
- A **package that moved** cannot be told from a damaged one. `verify` re-hashes at the stored
  root, which no longer exists, and the only failing verdict is `damaged`. ADR 0002 §6 forbids
  recording a second root, so the catalog never learns the new one.
- A **source that moved** is listed at its old location even by a package that records it gone.
  The compiler records a move as a new revision plus a `SourceAbsence` for the old location (root
  ADRs 0009, 0010). `resolve` returned every stated location, absent ones included.
- **`register(package_root)` takes any path.** On a host that can read several tenants'
  packages, one tenant could register another's package by path and then read its evidence. That
  sidesteps the schema-per-tenant isolation of ADR 0002 §2.

The 10⁵-package measurement ([ADR 0005](0005-l1-gate-catalog-data-model-amendments.md) §5) found
one more: `query` has a `limit` but no way to ask for the next page. The review also carried a
caveat deferred from ADR 0003: say what a clock merge may reorder.

## Decision

1. **Register verifies the whole package first, and never follows a link.** Before it opens a
   transaction, `register` walks the root without following symlinks (`lstat`, then `O_NOFOLLOW`
   opens). It checks, in this order:
   - Every entry under the root is a regular file or a directory. A symlink (to a file or a
     directory), FIFO, socket or device at any depth is `unsafe_entry`, with the package-relative
     path as the subject. It is never followed or opened.
   - The manifest parses and counts a table for every record kind of the schema version.
     Otherwise it is `manifest_invalid`.
   - The listed files equal the present ones (`file_missing`, `unexpected_file`), and each has
     its listed size and sha256 (`file_digest_mismatch`). A deleted record table is
     `file_missing`, because an empty table is an empty file (root ADR 0022 §1). A listed path
     that is absolute or escapes the root can never be present, so it is `file_missing` too, and
     nothing outside the root is read.
   - The records pass the package-schema readers (`record_invalid`).

   Each failure is `refused`, and nothing is written. Only then does it lock `tx_clock` and look
   up the id. The rows it indexes come from the bytes it verified, read once. So
   `already_registered` also certifies that the root it was given holds the registered package's
   bytes. A damaged copy of a registered package is `refused`, not `already_registered`. `verify`
   applies the same entry checks to the stored root.
2. **A moved package is `unreachable`, not `damaged`.** `root_locator` records where a package
   was registered from: a historical fact, not an address. `verify` gains the verdict
   `unreachable`: the stored root is missing, not a directory, or a symlink. Then nothing is
   compared, `files_checked` is 0, and one `package_unreadable` finding names the root. To check a
   package at its new place, call `register(new_root)`: `already_registered` means it is intact
   there (§1). The catalog still records no second root (ADR 0002 §6 stands), so the registration
   log and rebuilds are unchanged.
3. **Tenant package roots.** A catalog object serves one tenant (ADR 0004 §1) and is configured
   with that tenant's package roots: absolute directories.
   - **Containment is decided on fully resolved paths.** `register` computes the realpath of
     `package_root`. That is ADR 0004 §1's `root_locator`, with every symlink and every `..`
     resolved. It also computes the realpath of each tenant root at that moment. The package is
     inside a root only if its resolved path is that root's resolved path or below it, compared
     by path components (`Path.is_relative_to` on resolved paths). It is never compared by string
     prefix, so `/srv/b-evil` is not inside `/srv/b`. A `package_root` that, once resolved, is
     inside no tenant root is refused. That covers `B_root/link/pkg` with `link → A's tree`, and
     `B_root/../A_root/pkg`.
   - **The check comes before the §1 walk.** The walk then starts from the resolved path and
     follows nothing, so no link swapped in after the check is followed.
   - `register` refuses a root outside every tenant root with `package_unreadable`, worded as for
     a root that does not exist and with `package_id` `Unknown`, so the answer reveals nothing
     about another tenant's files.
   - Contract test: `test_register_stays_inside_the_tenants_package_roots`, with the symlink and
     `..` escapes. Implementations provide `CatalogContract.make_tenant_catalog(workdir, roots)`.
     The stub runs it as a strict expected failure.
   `access/` (MVL-99) owns the configuration, alongside the per-tenant database role it already
   needs (ADR 0002 §2). Every other cross-tenant probe already answers as for an unknown id:
   `resolve` gives `unresolvable` with `unresolvable_evidence`, `verify` gives
   `unknown_package`, `lineage` and `threads_of` give `unknown_record`, and `thread` gives an
   empty thread. Content ids, record ids and thread ids are equal across tenants because they
   are hashes. None of them is a capability: each lookup runs inside the caller's tenant schema.
4. **A moved source is another package, with the same records.** Re-ingesting after a source
   moved gives a new manifest, so a new package. Its evidence records keep their ids and bodies,
   so registration accepts them (ADR 0005 §2), and a current view lists the record once with both
   packages. `source_location` gains the new location, and `location_absence` (ADR 0005 §3)
   records the old one as gone.
5. **`resolve` routes each package to the locations it says hold the bytes.** For a referenced
   source, a package's `SourceLocation.locations` are the locations of its revisions of that
   content that no other revision or absence *in the same package* supersedes, in table order.
   `fetch` keeps registration order. The Ledger does not decide which package's statement is
   fresher; it never checks a location, and registration order is not observation order.
6. **`query` pages by keyset.** `QuerySpec.after: QueryCursor(kind, record_id, package_id) |
   None` keeps the rows whose sort key is strictly greater than the cursor, as UTF-8 bytes.
   `limit` keeps the first rows. The last row of a page is the next page's cursor, so paging is
   stable while packages are added if `as_of` is fixed. The database serves it from the record
   primary key (ADR 0005 §4, §5).
7. **Threads are returned whole in 1.x.** A thread is not paged: its current view resolves every
   lineage set over the whole thread (ADR 0003 §4), and its partitions are ordered by their
   smallest registration key. The measured workhorse thread, one machine with 4 000 of 10⁵
   packages and 20 000 entries, takes 195 ms at p95, inside its 500 ms budget (ADR 0005 §5). Revisit if a consumer needs threads of
   more than about 10⁵ entries. A thread windowed by clock range would then be a minor addition.
8. **What a clock merge may reorder** (amends ADR 0003 §3.5; the caveat deferred at its review).
   Entries of one clock mapped through the same path keep that clock's order, because each hop is
   monotone increasing. Two entries of one clock mapped through different paths can come out in
   the opposite of their native order only when (a) their mapped intervals overlap, so the
   evidence cannot order them on the reference clock, or (b) the mappings contradict each other:
   some declared bound does not hold. Under consistent mappings, each mapped interval contains
   the entry's true reference instant. If `s_x < s_y` and `hi_y < lo_x`, the true instants would
   have to satisfy `t_y < t_x`, which a monotone true relation forbids. Every merged entry carries
   its path and interval, so a caller can see which case applies. The unmerged order is always
   available: the same call at the same `as_of` without `merge`.
9. **catalog-api 1.1.0 is published as `stable`.** It adds the `unsafe_entry` finding code, the
   `unreachable` verdict, `QueryCursor` and `QuerySpec.after`. Each addition is an enum member or
   an optional field, so every 1.0.0 document validates against 1.1.0. 1.0.0 stays the pre-gate
   draft; the registry cannot promote a draft in place. It was published with `scripts/contracts.py
   bump catalog-api 1.1.0 --status stable` in the gate PR. As the first stable version, the tool
   also locks `neptune-memory`, the only consumer with a lock section, at 1.1.0. The coordinator
   posts the printed announcements to MVL-106, MVL-111, MVL-116 and MVL-120. The registry tool
   needs `LINEAR_API_KEY` for that, and implementers do not hold it.

## Alternatives considered

- **Follow symlinks that stay inside the root.** Rejected. The compiler never writes one (root
  ADR 0022 §1), so a link means the directory is not a compiler package. "Inside the root" is
  also a race: the link can be swapped between the check and the open.
- **Record every root a package is presented from.** Rejected. A second root is a registration
  fact that no package states, so the log would need a row per sighting and re-registration would
  allocate ticks. That gives up ADR 0002 §6's no-op re-registration for a convenience that
  `register(new_root)` already gives.
- **`verify(package_id, package_root=…)` to check at another root.** Rejected. It duplicates what
  `already_registered` certifies once registration verifies first.
- **Report a moved package as `damaged` with a `package_unreadable` finding.** Rejected. A
  consumer reading the verdict would treat a move as tampering.
- **List every stated location, with a separate list of absences.** Rejected. `fetch` is a route
  to the bytes, and a location its own package says is gone is not a route. The absences stay in
  the catalog (`location_absence`) and in the package.
- **Page `query` by offset.** Rejected. Offsets shift as rows are added and cost O(offset) to
  skip. A keyset cursor on the existing sort key is stable and index-served.
- **Page threads now.** Rejected. Per-lineage-set resolution and partition order need the whole
  thread, and the measured largest thread is inside the budget.
- **Publish 1.0.1 or leave 1.0.0 as the only version.** Rejected. The additions are minor by ADR
  0004's own rule, and stable needs a new version.

## Consequences

- MVL-90 implements §1 to §5 and passes the new contract tests: symlinks, a missing table, a
  manifest omitting a table, a damaged copy, a record body conflict, a moved source, a moved
  package, and query paging. Registration of a large materialised package now hashes it on every
  call, including identical re-registrations; that is the price of `already_registered` meaning
  "intact here".
- MVL-99 must configure tenant package roots as well as per-tenant roles before any
  multi-tenant deployment.
- Consumers can lock catalog-api 1.1.0. Later additions are minor versions that must accept
  1.1.0's goldens.
- Revisit §7 when a thread exceeds about 10⁵ entries, and §5 if a consumer needs the Ledger to
  pick between packages' location statements.
