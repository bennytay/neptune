# 0010 — Entity thread index: membership at registration, whole-thread reads, identity links and clock mappings

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-92
- Completes: ADR 0002 §7 and ADR 0005 §6 (the derived thread index). Amends: ADR 0002 §5
  (`transform_upstream` gains `position`); ADR 0003 §3.1 (a bound of 0 only when the absence is
  stated) and §3.4 (validity windows are half-open, as root ADR 0050 §3 states them).

## Context

MVL-92 implements ADR 0003's `thread` (history and current view) and `lineage` over the catalog.
ADR 0003 fixes the semantics; ADR 0002 §7 and ADR 0005 §6 leave the derived thread index to this
issue. Four facts shape it:

- Membership needs record bodies: `Stream.run`, a component's `category`, a software item's
  `commit` or `digest`, the candidates of an `Ambiguous` field. None is a catalog column, and
  `part_of` resolves a tier-2 reference only inside its package (ADR 0003 §2).
- `latest_transform` compares chains flattened from `upstream` *in consumed order* (ADR 0003
  §4.4), and `lineage` reports each edge's position. `transform_upstream` (ADR 0002 §5) kept the
  edges but not their order.
- Package schema 3 (root ADR 0050, MVL-82) adds `IdentityLink` and `ClockMapping`, and the
  Ledger reads schema 3. ADR 0003 names both (a thread's `links`, a merge's mappings) but was
  written before their shape: how a link's `Ambiguous` side, a mapping's anchor-and-rate form,
  its `Knowledge` fields and its half-open window reach the merge is open.
- ADR 0003 leaves a few membership details open: whose provenance grounds a field, what a run
  with an `Ambiguous` logical id joins, how a software version's key is written.

## Decision

1. **The thread index is written at registration, in the registration transaction.**
   Migration 0007 adds five append-only tables. `thread` holds each key once: `thread_id` and
   the key's canonical JSON. `thread_member` holds one row per thread entry: `(thread, package,
   record)`, the record kind, roles, registration key, transform, lineage source, the record's
   world-time columns and `ThreadEntry.world` as canonical JSON. `thread_unresolved` holds `(thread,
   package, record, pointer)` for each `Ambiguous` candidate. `thread_identity_link` and
   `thread_clock_mapping` hold the alignment records a thread reads (§8, §9).
   `threads.membership.thread_rows` is a
   pure function of the package's verified record lines and the Ledger version. Registration
   writes its rows after the record rows (ADR 0007 §1's boundary, extended). Every column except
   `registration_key` is independent of registration order. A replay of the registration log
   rebuilds the index byte for byte (ADR 0003 §8). 0007 refuses to apply to a catalog that already
   holds packages, as 0004 and 0005 do: append-only tables cannot be backfilled, so such a
   catalog is rebuilt. A package whose membership cannot be expressed in the catalog API (a key
   the API's schema refuses) is refused with `record_invalid` naming the record and the pointer,
   and writes nothing.
2. **Membership details** (ADR 0003 §2, which stays the only field table):
   - A field's grounding is its own provenance, or the record's when the field carries none. Only
     `observed` or `stated` counts. The same holds for each `Ambiguous` candidate, so an inferred
     candidate names no thread.
   - A record joins a thread only if it has a record-level evidence anchor (content id plus a
     non-empty locator). Every registered evidence record has one.
   - A run whose `logical_id` is not `Known` (including `Ambiguous`) opens its anchored run thread.
     An `Ambiguous` logical id also lists the run in each candidate's declared run thread as
     unresolved.
   - `part_of` joins every thread of the named kind that the referenced record is the `subject`
     of in the same package: the declared or anchored run thread of `Stream.run`, the
     configuration thread of `HardwareComponent.configuration`, and the document thread of
     `DocumentBlock.document`. It never follows a reference to another package.
   - A `software_version` key is `(version kind token, the stated sha or digest)`, verbatim. A
     model checkpoint's digest length is fixed by its algorithm, so two algorithms never share a
     key.
   - Unresolved pointers are JSON pointers to the field: `/machine`, `/identifiers/1`,
     `/capture/device_identifiers/0`, `/software/2/commit`.
3. **`transform_upstream.position`** (migration 0007) is the edge's 0-based place in the
   transform's `upstream`, unique per transform. `LineageEdge.position` and chain flattening read
   it.
4. **Reads.** `thread` reads its members with one index range scan on the thread id
   (`THREAD_MEMBERS`, filtered to `registration_key <= as_of`): `thread_member (thread_id,
   registration_key)`, or the primary key's `(tenant_id, thread_id)` prefix when the planner
   prefers it; both read only the thread's rows. World
   order, transaction order, per-set resolution, collapsing and any merge are pure functions
   (`threads.order`, `threads.merge`) over those rows. Threads are returned whole (ADR 0006 §7).
   - **Current view.** A record id registered by several packages is one entry. Its packages are
     listed by `(tx_seq, package id bytes)`, its roles are the union over those packages, and its
     registration key is that of its first package.
   - **Chains.** A transform is registered at a point when a `transform_record` row for it has a
     registration key at or before the point. A chain that reaches a transform not registered by
     then, or a cycle, is unknown. An unknown chain, like one holding a version that is not SemVer,
     compares with nothing, so it is never dominated and dominates nothing. A shared ancestor
     (a diamond) appears once per path that consumed it, as flattening depth first gives.
   - **Revisions** (ADR 0003 §5) are computed at read time from `source_location` rows registered
     by the point. There is one edge per superseding/superseded revision pair whose two contents
     are different lineage sets of the same kind in this thread. A revision that supersedes a
     revision of the same bytes (a move) is not an edge.
   - **Unresolved members** are listed by `(registration key, record id, package id, pointer)`.
   - **`lineage`** lists `registered_by` in registration order, nodes sorted by transform id,
     edges by `(transform id, position)`, and siblings (same kind, exact anchor, another
     transform) by `(kind, record id, package id)`. An upstream not registered by the point is a
     node with `NotCovered` fields, and its edge is kept. A kind without a transform
     (`source_artifact`, `source_revision`, `source_absence`, `transform_record`) has
     `transform_id` `NotApplicable` and an empty DAG.
5. **Requests.** A `thread` call is checked in this order, and the first failure rejects it with
   an empty payload (ADR 0004 §2): `as_of` outside the contract (`invalid_request`), beyond the
   catalog (`as_of_out_of_range`), no preference (`preference_required`), then a preference,
   order, key or merge outside the contract (`invalid_request`). A merge on transaction order is
   `invalid_request`: merging is a world-order operation. A merge naming a clock that no package
   registered by the point holds is `unknown_clock`. A named id that no package registered by the
   point holds as a `clock_mapping` record is `unknown_mapping` (another kind's record id
   included), and the call is rejected. A thread of a reserved kind (`zone`, `task`, `person`) is
   empty, like any unknown key, and lists no links. `threads_of` and `lineage` reject an empty record id or
   a bad `as_of` with `invalid_request`. A rejection echoes `preference` and `merge` only when
   they are inside the contract, so it encodes. A key or order outside the contract is echoed as
   given: no such request decodes from the wire, so only an in-process caller can make one.
6. **The merge engine.** `threads.merge` implements ADR 0003 §3.1-§3.5 and ADR 0006 §8 over
   §9's reading of a mapping (slope, offset, bound, half-open validity window, and why it is
   unusable, if it is). It uses exact rationals, ranks simple paths by total bound then by
   mapping-id bytes, checks each hop's window against the interval as it stands, and reports
   `unsupported_mapping` and `mapping_out_of_range` with the paths tried. It never lists every
   path: per entry (and per distinct start tick of a clock) it walks depth first, following a
   hop only when the hop's window holds the interval as it stands, which is what makes a path
   usable, and keeps the best-ranked complete walk. The result is the one ranking every path
   gives. Each clock's outgoing windows are sorted by start with a running maximum of their
   ends, so `bisect` finds the windows that hold an interval in about log(windows) comparisons;
   piecewise mappings at ADR 0005's thread size (20 000 entries, 100 windows per hop) merge
   every entry. The walk is bounded as a backstop: 10 000 window checks per entry and 1 000 000 per merge. An entry
   whose walk reaches either bound stays on its clock with a `mapping_out_of_range` finding
   saying so; a finding names at most 16 tried path prefixes (each ending at the hop whose window
   failed) and says how many there were. Property test P7 checks the walk against the full
   ranking on synthetic mappings; regression tests cover piecewise mappings (one per sync
   window) and a chain with 3¹² open paths; the catalog contract merges the quadruped worked
   example's stated mapping.
7. **Clocks are never interleaved.** ADR 0003's "records on another clock" are the contract's
   per-clock `Partition(kind="clock", clock_key=…)`. Each partition names its clock and is ordered
   only within itself. The partitions are ordered by their smallest registration key, then by
   clock bytes, with the untimed partition last.
8. **Identity links** (ADR 0003 §1.5, root ADR 0050 §4). Registration writes one
   `thread_identity_link` row per id a link's `right` states: one when `Known`, one per candidate
   when `Ambiguous`. Each row holds the link's `left` and that right id as canonical JSON
   `{namespace, value}`, the state (`known` or `ambiguous`) and the record's `assertion_kind`. A
   thread whose kind is keyed by a `LogicalId` (`machine`, `sensor`, `site`, `asset`, `run`) and
   whose declared key equals either side lists the link as a `ThreadLink` from the left id to the
   right id. **A link states no entity kind**: schema 3's `IdentityLink` has no field for one,
   so the edge's `entity_kind` is `NotCovered` (catalog-api 1.5.0), never the kind of the thread
   that lists it. It is not `Unknown`, which would mean the evidence could state the kind and
   does not; here the record has no place to state it. `from_key` and `to_key` carry the listing thread's kind only because they are keys
   of the lookup the caller made: they say "this link names the id you asked about", not "this
   link is about a machine". ADR 0003 §1.6 (an equal key of another kind is another thread)
   stands: the link is listed on each of those threads separately, each listing joins nothing,
   and none of them gains a kind from the link. Edges are listed by `(registration key, link record id, package id, right id)`,
   at the catalog point, in every preference: a link is not an entry and has no lineage set. A
   thread with no members still lists its links. Nothing is joined: no other thread's record is
   read, and `threads_of` is unchanged. `software_version` keys are version tokens, not logical
   ids, and list no links. A link's `validity` is not copied; a consumer reads it from the link
   record. An id the catalog API cannot express as a thread key refuses the package, as in §1.
9. **Clock mappings** (ADR 0003 §3, root ADR 0050 §5). Registration writes one
   `thread_clock_mapping` row per `ClockMapping`: its clocks and the merge's reading as canonical
   JSON. `slope = rate`, `offset = anchor.target − rate·anchor.source`, both exact. The bound is
   `residual_bound` when `Known` and 0 when it is `KnownAbsent` (stated: there is none); any
   other state makes the mapping unusable (`unsupported_mapping`, naming the field), as does an
   anchor or rate that is not `Known`. The validity window is on the source clock, start
   inclusive and end exclusive: a `Known` side is a bound, a `KnownAbsent` side is open, and an
   `Unknown` or `NotCovered` side, or a window that is not `Known`, cannot be checked, so no
   entry can use the mapping (`mapping_out_of_range`). A `NotApplicable` window (a timeless
   relation) holds at every instant. An inverted hop's window is the image under `f`, still
   half-open. A merge reads a mapping id's row from its first registration at or before the
   point; a record id has one body in every package (ADR 0002 §6).

## Alternatives considered

- **Compute membership at read time from `record.body`.** Rejected. A thread read would scan
  bodies across packages for `Stream.run` and the candidates, which misses ADR 0005 §5's budget.
  `body` is also NULL when a record holds U+0000 (ADR 0009 §1), so some members would silently
  drop out.
- **A materialised view refreshed after registration.** Rejected. A refresh is not part of the
  registration transaction, so a reader could see a package without its threads, and a refresh
  is not append-only.
- **Store only membership and join `record` for world time and transforms.** Rejected. The
  extra join per entry is the main cost of a large thread. The copied columns come from the same
  verified line, and foreign keys pin each row to its record and registration.
- **Read upstream order from `transform_record` bodies.** Rejected. The body can be NULL (U+0000
  in a config), and the order is part of the transform's identity, so it belongs beside the edge.
- **Treat an unknown chain as the lowest version.** Rejected. That guesses a precedence that no
  evidence states, and it would let a registered v1 beat an orphan v2.
- **Return an unmerged thread with a warning when a mapping is unknown.** Rejected. The caller
  asked for one timeline. A response that silently is not one reads as success. An unknown id is
  an argument error, like an unknown clock.
- **Read mappings and links from `record.body` at read time.** Rejected for the reason
  membership is not: `body` is NULL when a record holds U+0000, and a link lookup by id would
  scan every link body.
- **Treat a residual bound that is not stated as 0 (ADR 0003 §3.1's "0 if it declares none").**
  Rejected. That rule predates root ADR 0050's `Knowledge` field: `Unknown` means the bound could
  be stated and is not, so 0 would claim an exactness no evidence states. `KnownAbsent` is the
  declared "none".
- **Use a mapping whose window is not stated as valid everywhere.** Rejected. That extrapolates,
  which ADR 0003 §3.4 forbids; only a stated open side or a timeless relation is unbounded.
- **Rank every simple path first, then scan the list per entry.** Rejected: exponential in the
  length of a chain of parallel mappings (piecewise sync windows over sensor → host → PTP →
  GPS), so an ordinary merge could stall a read. Pruning by window gives the same answer.
- **Drop the kind from a link's keys.** Not possible in 1.x: `from_key` and `to_key` are
  required `ThreadKey`s, and removing or retyping a field is a major version (ADR 0004
  Consequences). 1.5.0 adds `entity_kind` instead; a 2.0.0 may replace the keys with bare ids.
- **List a link only on its left thread, or add the linked thread's entries.** Rejected. A
  consumer reaching either name must find the edge; adding entries is the merge ADR 0003 §1.5
  forbids.

## Consequences

- Registration writes a few more rows per evidence record, typically one to three. The scale
  harness now loads and measures the thread index (`thread_index_*`). Re-measure at the L2 gate
  at 10⁵ packages.
- MVL-94 (rebuild from packages) must replay the log through registration, so the thread index
  is rebuilt with everything else. No separate rebuild path exists.
- A merge works on any catalog holding a stated `ClockMapping`; derived (inferred) mappings
  under a package's `derived/` (MVL-36) are not indexed and are not nameable until an ADR
  decides how the Ledger catalogs derived records.
- MVL-97's per-clock interval index can reuse `thread_clock_mapping`'s reading rather than parse
  mappings again.
- Package schema 4's lifecycle kinds (root ADR 0051's commissioning, maintenance, incident and the
  rest) name machines in `machines`, but join no thread: they are not in ADR 0003 §2's table.
- A new thread kind, membership field or world-time field still needs a superseding ADR to 0003.
  It also needs a membership change here, and a catalog rebuild, because the index holds the old
  membership.
