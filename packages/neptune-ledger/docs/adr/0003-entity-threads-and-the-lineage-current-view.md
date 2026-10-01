# 0003 — Entity threads: one declared key, per-clock order, and a named lineage preference

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-87

## Context

The Ledger's value over a pile of packages is the **thread**: every catalogued record about one entity,
across every package, in a defined order, with the lineage that says which transform produced each record
and which record is current under a stated preference. Threads must be defined before anything is
indexed, because every layer above (Memory, Context, Deploy, Learn) will join on them.

Facts from the compiler that constrain the design:

- **Identity tiers** (root ADR 0003). Tier 1 identifies bytes; tier 2 (`RecordId`) identifies one record
  *in one lineage* and changes whenever the adapter version or config changes; tier 3 (`LogicalId`,
  `(namespace, value)`) identifies a real-world thing and is declared, never inferred. The issue text says
  "logical id (ADR 0003 tier 2)": the logical id is tier **3**. A tier-2 id cannot key a thread, because
  adapter v2 gives every record a new tier-2 id. Cross-version joins go through `EvidenceRef` and locator
  equality (root ADR 0003, Consequences).
- **Clocks** (root ADRs 0005, 0012). A timestamp is `(ticks, domain_id)`; `domain_id` is a tier-2 id;
  domains are scoped to one source; cross-domain ordering is a type error; relating two domains takes an
  explicit alignment record (MVL-82 `ClockMapping`).
- **Missing identity is explicit.** A URDF's `HardwareConfiguration.machine` is `NotCovered`; a recording's
  `Run.logical_id` is usually `Unknown`. Many records (streams, documents, configurations) have no
  logical id at all, so a thread design keyed only by `LogicalId` would leave most evidence unthreaded.
- **Transforms** (root ADR 0016). `TransformRecord(adapter_id, adapter_version, config_hash, upstream)`;
  adapter versions are SemVer (root ADRs 0008, 0024), ordered only by SemVer §11 precedence (root ADR 0014).

Storage (package, record, source, transform and clock tables, `tenant_id`, transaction time) is ADR 0002
(catalog data model). This ADR defines semantics over those tables and adds no table of its own.

If this is wrong, identities get merged by accident, order changes between rebuilds, or a parser upgrade
silently replaces what a downstream consumer reproduced against.

## Decision

### 1. Thread key

1. A thread is identified by `ThreadKey(kind, key)` within one tenant. Threads never span tenants; every
   lookup is scoped by ADR 0002's `tenant_id`.
2. `key` is exactly one of:
   - **Declared**: one `LogicalId(namespace, value)` (root ADR 0003 tier 3), or one declared content
     identifier of software (§2, `software_version`). Namespace and value compare as exact Unicode code
     point sequences: no case folding, trimming or normalisation (`spot-07` ≠ `Spot-07`).
   - **Anchored**: `EvidenceAnchor(source content id, locator)`, the record-level `EvidenceRef` of the
     record that opens the thread (root ADR 0016 §1, locator as its JSON array). It is tier 1 plus a
     locator, so it is the same for every transform version that cites the same evidence the same way. It
     is used only for kinds that have no declared identifier (§2). It identifies "what this evidence
     declares", never a real-world individual: two byte-identical files are the same evidence.
3. `thread_id = "sha256:" + hex(sha256(canonical JSON of {"key": …, "kind": …}))`, canonical JSON as in root
   ADR 0002. It is a derived index key, rebuildable from packages alone.
4. **A thread never spans two keys.** A `Machine` that declares both `("serial", "BD-1047")` and
   `("manifest", "spot-07")` is a member of two machine threads. Co-declaration is evidence that identity
   resolution (MVL-35) uses; the Ledger reports it (`threads_of(record)`) and never unions the threads.
5. **Threads are related, never merged.** The only relation between two declared threads is an MVL-82
   `IdentityLink` record, exposed as an edge `(link record id, from key, to key, assertion_kind, Knowledge
   state)`. A thread query never includes another thread's records; a caller follows the edge itself.
6. **Equal key, different kind, different thread.** `("serial", "X")` declared as a `Machine` identifier
   and as a `HardwareComponent` identifier gives a machine thread and a sensor thread.

### 2. Membership and thread kinds

A record joins a thread in one of three roles, from the field table below and nothing else:

- `subject`: the record declares the entity (its `identifiers`, its own `logical_id`, or it opens the
  anchored thread);
- `cites`: one of its fields names the entity's declared key (`Calibration.machine`);
- `part_of`: it references a `subject` record of the thread by tier-2 id **within the same package**
  (`Stream.run`). Tier-2 references are lineage-scoped, so they resolve only inside their package.

Rules: only a `Known` value with `stated` or `observed` provenance creates membership. An `Ambiguous`
value with candidates puts the record in each candidate thread's `unresolved` list, outside the ordered
entries. `Unknown`, `KnownAbsent`, `NotCovered` and `NotApplicable` create nothing. Records with
`inferred` provenance never open or join a thread. Membership is one hop, never transitive: a stream
`part_of` a run is not thereby in the run's machine thread. A thread **entry** is `(package id, record
id)` with the set of roles it holds; the same record id registered by two packages is two entries.

| Thread kind | Key | `subject` | `cites` / `part_of` |
|---|---|---|---|
| `machine` | declared | `Machine.identifiers` | cites: `Run.machine`, `HardwareConfiguration.machine`, `SoftwareConfiguration.machine`, `Calibration.machine` |
| `sensor` | declared | `HardwareComponent.identifiers` where `category = sensor` | cites: `Image`/`Video` capture `device_identifiers` |
| `site` | declared | `Site.identifiers` | cites: `Site.parent`, `Asset.site` |
| `asset` | declared | `Asset.identifiers` | cites: `Asset.parent` |
| `run` | declared if `Run.logical_id` is `Known`, else anchored on the `Run`'s evidence | `Run` | part_of: `Stream.run` |
| `stream` | anchored | `Stream` | — |
| `configuration` | anchored | `HardwareConfiguration`, `SoftwareConfiguration`, `Calibration` | part_of: `HardwareComponent.configuration` |
| `software_version` | declared: `("git_commit", sha)`, `("container_image_digest", …)`, `("model_checkpoint_hash", …)` from a `SoftwareItem`'s `commit` / `digest`, namespace = the compiler's version kind token | — | cites: `SoftwareConfiguration` with a `Known` commit or digest on any item |
| `document` | anchored | `DocumentRecord` | part_of: `DocumentBlock.document` |
| `zone`, `task` | declared | reserved: no schema-v1 record kind declares them | — |
| `person` | declared only | reserved: only a stated identifier field on a record kind; never names read from text, never observed faces or voices, never inferred | — |

Software names and release text never key a thread: two builds both labelled `nav2 1.2` may differ.
Component categories other than `sensor` open no thread in v0; they are reachable through their
configuration. A new row (a reserved kind becoming live, a new record kind, a new field) is a superseding
ADR, never an implementation choice.

### 3. Ordering

**World time of an entry.** From this table only; any other record kind has no world time.

| Record kind | Start `s` | End `e` |
|---|---|---|
| `run`, `stream` | `first` | `last` |
| `calibration` | `valid_from`, else `performed` | `valid_until`, else `performed` if `s` came from it |

Only `Known` timestamps count. If `s` is not `Known` but `e` is, `s := e` (a point at the end). The
entry's clock is the domain of `s`. If `e` is not `Known`, or is on a different clock from `s`, it is
**open** (sorts after every closed end) and is returned to the caller as stated, never converted.

**Ordering clock.** Two `TimestampDomain` records are one ordering clock iff they have the same source
content id, `field` and `scope` (root ADR 0012 §2) and their `resolution`, `epoch` and `timescale` are all
`Known` and equal. This lets lineage siblings of one source share a clock without conversion: their ticks
are the same integers at the same resolution. Otherwise a clock is its own domain id. The **clock key** is
the canonical JSON of `{content_id, epoch, field, resolution, scope, timescale}` in the first case and
`{"domain": domain_id}` in the second. Two sources never share a clock, whatever their timescale.

**World order** (the thread's order) is a sequence of **partitions**:

1. one partition per ordering clock, holding the entries whose `s` is on that clock, sorted by
   `(s ticks, e ticks with open last, registration key, record id, package id)`;
2. partitions sorted by `(smallest registration key among their entries, clock key bytes)`;
3. a final **untimed** partition, sorted by `(registration key, record id, package id)`.

The registration key is the registering package's transaction key in ADR 0002's transaction order (Ledger
monotonic clock plus sequence); it only breaks ties within a clock and orders untimed entries. Record id
and package id compare as UTF-8 bytes of their canonical strings. Entry identity is `(package id, record
id)`, so the key is a strict total order. Adjacency carries temporal meaning **only inside a partition**;
the response marks partition boundaries and each partition's clock key.

**Cross-clock merge** only when the caller names a reference clock key and a set of MVL-82 `ClockMapping`
record ids. The Ledger builds an undirected graph whose nodes are clocks and whose edges are the named
mappings. A clock with exactly one simple path to the reference is mapped along it. A clock with no path
stays separate. A clock with more than one path stays separate and the result reports
`ambiguous_mapping_path` with the paths; the Ledger never chooses. A mapping that is not monotone
increasing is not used and is reported. An instant outside a mapping's validity window is not mapped; its
entry stays in its native partition. Mapping is evaluated in exact rationals as MVL-82 freezes the
function. The mapped start is the interval `[lo, hi]` in reference ticks: `lo = floor(f(s) − bound)` and
`hi = ceil(f(s) + bound)`, where `bound` is the mapping's declared residual bound and 0 if it declares
none. Merged entries are sorted by `(lo, hi, clock key bytes, then their native partition key)`. Because
mappings are monotone and the native key follows, the merged order restricted to one clock equals that
clock's own order. Each entry carries its mapped interval and the mapping ids used; stored ticks are
never rewritten.

**Transaction order**, only when the caller asks for it: `(registration key, record id, package id)` over
all entries, timed or not. This is the only order in which ingestion time leads. The Ledger never uses
host wall clock, file mtime or source-declared ingest time for any order.

### 4. Lineage and the current-view resolver

1. A **lineage set** is `(thread, record kind, source content id)`: the thread's entries of that kind whose
   record-level evidence is that source. Members of one lineage set with the same locator and different
   transforms are **siblings**. The set, not the locator, is the unit of selection, so an adapter version
   that makes locators finer still has its whole output selected together.
2. `history(thread, order)` returns every entry in the requested order (§3), each with its transform,
   lineage set and registering package. Nothing is collapsed or hidden.
3. `current(thread, preference, order)` selects **one transform per lineage set** and returns that
   transform's records in the requested order. Each lineage set resolves to `Known(transform)`,
   `Ambiguous(candidate transforms)` or `NotCovered`. Entries with the same record id from several packages
   appear once, listing every registering package in registration order.
4. **The preference is required.** There is no default. A call without one is rejected by the API
   (MVL-88), never filled in.
   - `latest_transform`: each transform has a **chain**: the `(adapter_id, adapter_version)` pairs from the
     root adapter to itself, flattening `upstream` depth first in consumed order (root ADR 0016 §4). Two
     chains are comparable iff their adapter-id sequences are equal. They compare element-wise from the
     root by SemVer §11 precedence. The greatest chain wins. Equal precedence (different config hash, or
     build metadata only) is broken by the transform's first registration key, latest wins, and the result
     says `tie_broken_by: registration`. Incomparable candidates (different adapter ids or chain shapes,
     or a version that is not valid SemVer) give `Ambiguous`. "Latest" never means "most recently
     ingested" except as that final tie-breaker.
   - `pinned(transform id)`: that transform in every lineage set where it appears; `NotCovered` elsewhere.
     Never a fallback.
   - `as_registered_by(package id)`: the transform that package used for the set; `NotCovered` where the
     package has no member; `Ambiguous` if it used more than one.
5. Every resolver call is evaluated at a catalog point `as_of` (an ADR 0002 transaction key). If the
   caller omits it, the latest committed point is used **and returned**, so the call can be replayed
   exactly. A result is a pure function of `(catalog as of the point, thread key, preference, order,
   reference clock, mapping ids)`.

### 5. Supersession is never deletion

- Registering a package only adds rows. A newer package re-ingesting the same source with another
  transform adds lineage siblings. The older rows stay, unflagged and unchanged.
- "Current" is computed at read time, or held in a derived index that is rebuildable and marked derived.
  It is never a flag, tombstone or update written onto a record row.
- A **source revision** (new bytes at the same location, root ADR 0009 `supersedes`) is new evidence, not a
  lineage sibling. It forms a different lineage set, keeps its own world time, and is exposed as a
  `revises` relation between the two entries. No preference hides the earlier revision. Which one is "in
  force" at an instant is a question for the layers above, answered from the intervals the Ledger returns.
- Removing a package (for example for legal reasons) is out of scope here and needs its own ADR.

### 6. Worked examples

**A. Manipulator URDF re-parsed by adapter v2 beside v1.** Source `sha256:a1…` (`ur5e_cell.urdf`).

| Package | Reg. key | Transform | Record | Locator |
|---|---|---|---|---|
| P1 | k1 | T1 = `neptune.urdf` 1.0.0 | `HardwareConfiguration` h1, components c1…c9 | `<robot>` element |
| P2 | k2 | T2 = `neptune.urdf` 2.0.0 | `HardwareConfiguration` h2, components d1…d9 (finer locators) | `<robot>` element |

- `h1.machine` and `h2.machine` are `NotCovered` (a URDF names no robot), so neither joins any machine
  thread, whatever `<robot name>` says. h1 and h2 open the same anchored `configuration` thread, because
  their anchor `(sha256:a1…, <robot> locator)` is equal. c* and d* join it `part_of`.
- Lineage sets: `(thread, hardware_configuration, a1)` = {h1, h2}, which are siblings. `(thread,
  hardware_component, a1)` = {c1…c9, d1…d9}: v2's finer locators mean no c/d pair are siblings, but they
  are in one set.
- No world time, so the untimed partition holds h1, c1…c9 (k1), then h2, d1…d9 (k2), each group by record id.
- `current(latest_transform)` resolves both sets to `Known(T2)`, giving h2 and d1…d9 and no v1 component,
  because chains `[(neptune.urdf, 1.0.0)] < [(neptune.urdf, 2.0.0)]`. `current(pinned(T1))` and
  `current(as_registered_by(P1))` give h1 and c1…c9. `history` gives all 20. P1's rows are untouched.
- If v2 had cited a different locator for `<robot>`, its anchor would differ and h2 would open a second
  configuration thread. The Ledger does not guess that the two are the same thread; both remain reachable
  by source content id in the catalog (ADR 0002).

**B. Legged robot's camera calibration replaced.** Machine `("serial", "QX-0042")`.

| Package | Reg. key | Source (same location `calib/camchain.yaml`) | Record | `valid_from` |
|---|---|---|---|---|
| P3 | k3 | `sha256:b1…`, revision r1 | `Calibration` C1 (`subject` `cam0`) | ticks 1714000000 on D1 (document clock of b1) |
| P4 | k4 | `sha256:b2…`, revision r2 `supersedes` r1 | `Calibration` C2 (`subject` `cam0`) | ticks 1719000000 on D2 (document clock of b2) |

- Both `cite` the machine thread `("serial", "QX-0042")`, and each opens its own anchored configuration
  thread, because the content ids differ. `subject` `cam0` is a name and keys no sensor thread.
- World order in the machine thread: D1 and D2 belong to different sources, so they are two clocks even
  though both are UTC/Unix ISO-8601 dates. The result is two partitions, [C1] then [C2], ordered by
  k3 < k4. The order inside each partition is temporal; the order between them is not.
- With reference clock D1 and an MVL-82 `ClockMapping` M (D2→D1, offset 0, stated, bound 0), the merged
  partition is C1 (`lo` 1714000000) then C2 (`lo` 1719000000), each carrying M's id.
- C1 and C2 are **not** siblings: they are in different lineage sets (b1 ≠ b2). `current(latest_transform)`
  returns both. The replacement shows as a `revises` edge C2 → C1 from r2 `supersedes` r1, and nothing is
  hidden. Whether C2 is in force at a given instant is for Context or Memory to decide from `valid_from` /
  `valid_until`.

**C. Mobile robot run spanning two packages.** A manifest declares run `("manifest", "night-42")` on
machine `("manifest", "amr-11")`. It was recorded as two MCAP splits ingested a day apart.

| Package | Reg. key | Source | Records (all T5 = `neptune.mcap` 0.3.0) | Clock of `first` |
|---|---|---|---|---|
| P5 | k5 | `part0.mcap` `sha256:c0…` | `Run` R0 (`logical_id` Known via manifest), `Stream` `/scan` S0, `/odom` O0 | `log_time` of c0 (L0) |
| P6 | k6 | `part1.mcap` `sha256:c1…` | `Run` R1 (same `logical_id`), `Stream` `/scan` S1, `/odom` O1 | `log_time` of c1 (L1) |

- R0 and R1 are `subject`s of one declared run thread. S0 and O0 are `part_of` it through R0 (inside P5),
  and S1 and O1 through R1 (inside P6). Both runs `cite` the machine thread `amr-11`. Each stream also opens
  its own anchored stream thread.
- World order of the run thread: partition L0 = [R0, S0, O0], then partition L1 = [R1, S1, O1]. Suppose
  R0 and S0 share `first` = 1000 and `last` = 9000: the tie falls through `e`, then the registration key
  (equal, both k5), then record id bytes, so the order is the same on every rebuild.
- With reference L0 and an estimated `ClockMapping` L1→L0 from MVL-36 (bound 2 ticks), the two partitions
  merge into one interval-ordered sequence. Without it they stay apart; the Ledger never assumes two
  `log_time`s agree.
- `current(as_registered_by(P6))` gives R1, S1, O1, and `NotCovered` for the three c0 lineage sets. A
  missing half is stated, not blank.
- If the manifest had not declared `night-42`, R0 and R1 would each open an anchored run thread. Relating
  them is MVL-34's `RunAssembly`, not a Ledger guess.

### 7. Property-test specification (for the L2 implementation)

Use Hypothesis (already a workspace dev dependency) with a fixed `derandomize=True` profile in CI. Test
module basenames must be unique repo-wide (`test_ledger_thread_order_properties.py`).

**Generators.** Catalogs of 1–6 packages with distinct registration keys and 0–40 records. Records are
drawn from the §2 kinds across manipulator, legged, aerial, marine and wheeled fixtures. They must cover:
forced ties (equal `s`, equal `e`, equal registration key); missing and open ends; starts and ends on
different clocks; the same record id in two packages; domains that do and do not meet the ordering-clock
rule; SemVer chains with prereleases and build metadata; chains with different adapter ids; and
monotone and non-monotone `ClockMapping`s with validity windows.

| # | Property |
|---|---|
| P1 | **Input-order invariance.** For a fixed catalog state, permuting the order in which records, packages, transforms or mappings are supplied to the indexer yields an identical result. |
| P2 | **Byte determinism.** The serialised result (canonical JSON) is byte-identical across two runs, two processes and different `PYTHONHASHSEED` values. |
| P3 | **Strict total order.** The entry comparator is irreflexive, antisymmetric and transitive, and no two distinct entries compare equal. |
| P4 | **Clock isolation.** Without mappings, every partition holds exactly one clock key. Adding or removing entries on clock B never changes the relative order of entries on clock A. |
| P5 | **Append-only history.** For `t1 < t2`, `history(as_of t1)` is a subsequence of `history(as_of t2)`, and the relative order of two entries on one clock never changes as packages are added. |
| P6 | **Ingestion time only breaks ties.** Changing registration keys while keeping their relative order leaves world order unchanged. Inside a partition, entries with distinct `s` are ordered by `s` whatever their registration keys. |
| P7 | **Merge consistency.** With monotone mappings, the merged order restricted to any one clock equals that clock's unmerged order. Non-monotone, out-of-window and multi-path mappings never move an entry out of its native partition. |
| P8 | **Resolver.** `current` without a preference is rejected. `pinned(T)` returns only T's records. `as_registered_by(P)` returns only transforms that P registered. `latest_transform` picks the maximum under §4.4, or `Ambiguous` exactly when the candidates are incomparable. Every lineage set appears exactly once as `Known`, `Ambiguous` or `NotCovered`. `history` ⊇ `current` for every preference. |
| P9 | **Worked examples.** §6 A–C are golden fixtures whose expected orders and resolutions are written out in full. |

## Alternatives considered

- **Key threads by tier-2 record id.** Rejected: tier-2 ids are lineage-scoped, so every adapter upgrade
  would start a new thread and lineage could not be seen.
- **Merge threads when an `IdentityLink` says `Known`.** Rejected: links are claims, may later be
  contested, and are owned by Memory. A merged thread cannot be un-merged without rewriting history.
  Edges keep both views available.
- **One canonical clock per thread by converting to UTC.** Rejected: most robot clocks are boot or
  monotonic, and conversion at this layer violates "no conversion" (root ADR 0005). Partitions plus
  caller-named mappings keep every comparison explicit.
- **Order by ingestion wall clock.** Rejected: it reflects when a package was registered, not what
  happened, and differs between rebuilds. It survives only as the opt-in transaction order and as a
  tie-breaker.
- **Default preference `latest_transform`.** Rejected: a consumer that reproduced results against v1
  would silently move to v2. Naming the preference costs one argument.
- **"Latest" = most recently registered transform.** Rejected: backfilling an old adapter version would
  make it "latest". SemVer precedence on comparable chains reflects intent; registration order only breaks
  exact ties.
- **Pick a winner across different adapters.** Rejected: there is no evidence-based order between, for
  example, a URDF adapter and an SDF adapter. `Ambiguous` plus `pinned` is honest.
- **Select per sibling (per locator) in `current`.** Rejected: an adapter that makes locators finer would
  leave stale v1 records with no v2 sibling in the "current" view. Selecting per lineage set avoids that.
- **Anchor threads at the source only, or by declared names.** Rejected: source-only anchors put three
  robots' hardware entries from one manifest into one thread; names are not identifiers.
- **Admit `Ambiguous` candidates as ordinary members.** Rejected: that fabricates membership. The
  `unresolved` list keeps them discoverable without asserting them.

## Consequences

- Threads, partitions and current-views are derived indexes over ADR 0002's tables, rebuildable from
  packages alone. L2 implements them against §7. MVL-88 fixes the API names and types for `history`,
  `current`, `threads_of`, the preference variants and the result states.
- Consumers get world order that never lies about cross-clock simultaneity. In return they have to handle
  partitions, and they have to name mappings when they want one timeline.
- Anchored threads depend on adapters keeping a subject's locator stable across versions, which is the
  compiler's own cross-lineage join contract. An adapter that changes it splits the thread (example A,
  last bullet). Revisit if that happens in practice, by adding an explicit, provenanced locator
  correspondence from the compiler, not a Ledger heuristic.
- Adding a thread kind, a membership field or a world-time field is a superseding ADR. The reserved
  `zone`, `task` and `person` kinds go live only that way.
- Revisit if MVL-82's `ClockMapping` function is not monotone or carries no bound, or if a consumer needs a
  cross-lineage record identity that per-set selection cannot provide.
