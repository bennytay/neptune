# 0011 — The time-domain registry: clocks, mappings and chains, never estimated

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-130

## Context

Every robot logs on clocks of its own: a flight controller's boot clock and its GPS receiver's time, a
manipulator's controller clock and its wrist camera's hardware clock, an AMR's boot clock and its site's NTP
server. Memory stores valid time on the clock a record declares and never compares two clocks (ADR 0002 §3,
ADR 0005 §2). Context still has to ask "what was the robot doing at 14:02 site time", so the graph must say which
clocks a machine has and how they relate, without ever coercing one claim onto another clock (ADR 0007 §4).

The compiler already says how clocks relate. A canonical `ClockMapping` (root ADR 0050 §5) is what a source
states, `observed` or `stated`: an affine map `target(t) = anchor.target + rate * (t - anchor.source)` with a
residual bound and a validity window on the source clock. A `derived/clock_mapping` line (root ADR 0060) has the
same fields and is a fit, `inferred`. Neither is ever re-estimated or re-timed. Forces:

- **Never invented.** A mapping Memory made up (an offset guessed from two clocks with one epoch, a window
  assumed open, a fit of its own) would put a fact in the graph that no evidence states.
- **Claims have no edge properties.** A claim is `subject predicate object`; a mapping is an edge *and* its
  parameters, and convert needs the exact parameters (a rate is an exact fraction; a float loses it).
- **Inference lives in `derived/`.** `consolidate/` emits observed and stated claims only, so an estimate cannot
  be relayed from there.
- **Revision.** A re-sync states a new mapping of one clock pair from some instant. Nothing in either record
  says which supersedes which; only their windows and the Ledger's arrival order do, and a consolidator sees one
  snapshot without arrival order.
- **Every robot.** Nothing may assume a flight controller, GPS or one clock per machine.

## Decision

### 1. Clocks and `has_clock`

- New node type `clock` (entity tier). `node_id` is the clock's compiler `TimestampDomain` record id: the clock
  as declared. Two clocks with the same timescale and epoch (two hosts' POSIX clocks) are two nodes.
- `has_clock`: `machine` → `clock`, `many`. `memory.time` emits one per run whose record declares its machine
  (`Known`, a declared value), per clock the run's records carry: every stream's `clocks` and the clocks the
  run's `first` and `last` are stated on. Valid over the interval the records observe the clock: the earliest
  first and latest last stated *on that clock* (run or stream), as `[first, last + 1)`; failing that, the run's
  own `[first, last + 1)` on the clock the run states it on. An unstated last is `OPEN`; a last at `INT64_MAX`
  is `OPEN`. A clock no record places in time is a `time.clock_unobserved` finding and no claim; a last before
  the first is `time.untimeable_clock`. One claim per (machine, clock, run), so a new package adds claims and
  never changes earlier ones. Assertion kind: `stated` when every cited record is, else `observed`.
- Run and stream records are read with the compiler's strict `run_from_json` and `stream_from_json`.

### 2. A mapping is a `maps_to` edge plus a `clock_map` literal

- `maps_to`: `clock` → `clock`, `many`, the traversable edge (so `neighbours` and inverse lookups see it).
- `clock_map`: `clock` → new value type `clock_map`, `many`, the parameters. Both claims of one mapping have the
  same subject, valid interval, assertion kind, evidence and records.
- A `ClockMap` (`schema.clock_map`) holds `target` (a clock record id), `method`, and `anchor`, `rate` and
  `residual_bound` exactly as the record states them, each a `Knowledge` state that inherits the claim's
  provenance (as a quantity's unit does; ADR 0002 §2). `method` is the compiler's `stated` or `co_sampled`, or
  `composed` (§3). `KnownAbsent` is refused: a mapping stated to have no rate cannot be read. Its JSON is
  `#/$defs/ClockMap`, with the compiler's `ClockAnchor`, `Fraction` and `Duration`.
- Valid time is the record's validity window on the source clock, as declared (no `CivilClock` placement). A
  side the evidence states open (`KnownAbsent`) is open: `OPEN`, or `INT64_MIN` ticks for a start. A validity,
  start or end that is not stated (`Unknown`, `NotCovered`, not a window) grounds **no** claim and gives a
  `time.validity_unstated` finding: root ADR 0060 §7, a bound not stated is not assumed open.
- `assertion_kind` is the record's: `observed` or `stated` from `memory.time`, `inferred` from the derived
  consolidator (§4). The claim cites the record's evidence and every citation its states carry.

### 3. Revision and chains

- **Revision.** Mappings of one pair (same source, same target, both declared or both estimated) are pieces of a
  history. Each mapping holds over its window minus every overlapping window of its pair that **starts later**
  (`Interval.minus`, so a short later window splits it). A cut mapping's claims cite the records that cut it.
  This is the resolver's own rule for `one` facts (the later `valid_from` wins; ADR 0005 §1) applied to windows
  the evidence states, so it needs no arrival order and gives the same pieces in any package order. Two mappings
  of a pair that start at one instant and state different parameters both stand, with a
  `time.conflicting_mappings` finding; a conversion through them is `Ambiguous`. Identical parameters
  corroborate.
- **Chains.** Every chain of 2 to `MAX_CHAIN_HOPS = 4` mapping pieces followed source to target, visiting no
  clock twice, whose hops all state an anchor and a rate, is emitted as a separate `maps_to` + `clock_map` pair
  where every hop applies. Its `ClockMap` is `composed`: `chain` (the mapping records, hop by hop), `via` (the
  clocks between) and **no parameters** (`NotApplicable`): the offset is the hops' arithmetic, computed at query
  time (§5), so a chain claim never states a number no record states. Its window is exact:
  `s <= f(t) < e` iff `ceil(f⁻¹(s)) <= t < ceil(f⁻¹(e))` for integer `t` and an increasing `f`, folded from the
  last hop back. Chains are forward only; inverses are query-time arithmetic, which keeps chain claims linear in
  the paths rather than quadratic in the clocks. Kind: `inferred` if any hop is estimated, else `stated` if every
  hop is, else `observed`. A chain through a revised mapping is cut at the revision like the mapping.

### 4. Estimates are relayed by `derived/`, never made

`memory.time_estimates` (`neptune_memory.derived.clocks`) reads `derived/clock_mapping` lines with the compiler's
`inferred_clock_mapping_from_json` and applies §2–§3 to them: inferred claims for each estimate, and for every
chain with at least one estimated hop (declared hops included). Its model is
`ModelRef("neptune.clocks", CLOCKS_VERSION)`, the compiler pass whose fits they are; each claim also cites the
estimate's transform record, which names the exact fit. Confidence is `Unknown`: the compiler states a residual
bound, not a probability. `memory.time` reports an estimate it skips as an INFO `time.estimated_mapping`
finding. Malformed or conflicting records are reported once, by the consolidator whose input they are.

### 5. `schema.clocks.convert`

`convert(reader, ticks, from_clock, to_clock, as_of, *, include_inferred=True, max_hops=8) -> Conversion` walks
the direct `clock_map` claims of one snapshot breadth first, forward (`rate * t + offset`) or backward (the exact
inverse), applying a mapping only where its valid interval holds the instant on its source clock. Ticks are exact
`Fraction`s, never rounded; the bound accumulates `rate * bound + residual` forward and
`(bound + residual) / rate` backward, `Unknown` once a hop states none. Declared mappings first; estimated ones
only when no declared chain converts, and the result is marked `inferred`. `result` is `Known`, `Ambiguous`
(readings that hold disagree; never one picked) or `Unknown` with `MissingHop`: the clocks reached, the clock not
reached, and the mappings that exist but do not apply (outside validity, or no anchor or rate). Caller errors
(ticks outside signed 64-bit, a bad clock id, a negative `max_hops`, `as_of` past `head`) raise.

### 6. Contract

Vocabulary 4 (`has_clock`, `maps_to`, `clock_map`; the every-node-type predicates widen to `clock` and bump their
versions), the `clock` node type and the `clock_map` value type are graph-schema **1.2.0**, a minor release:
every 1.0.0 and 1.1.0 golden validates. The golden graph adds `memory.time` (priority 4): the drone's three clocks
and the quadruped's stated `starting_time` → `log_time` mapping. A new generation (ADR 0006 §7).

### 7. Withdrawal stays MVL-132's

Within one build, a revision ends the old mapping and its chains at the revision. Across builds, the version a
build emitted open before the revision is a different claim (its valid time differs) and stays current beside
the closed one until ADR 0007 §5's build withdrawal ends it. A strict `xfail` in
`tests/test_g1_stress_clock_mapping.py` and `tests/test_clock_convert_memory.py` pins that and flips with
MVL-132.

## Alternatives considered

- **One `maps_to` claim with the parameters as its object**: the clock-to-clock edge disappears, so a reader can
  only find a clock's inbound mappings by scanning every claim.
- **`maps_to` as a `one` predicate, so the resolver closes revisions by arrival**: one subject maps to several
  clocks at once (boot → GPS and boot → site), so different targets would contradict; a per-pair node would
  invent an entity no record declares.
- **Composed parameters in chain claims** (an offset as a fraction of target ticks): a number no record states,
  and a second copy of arithmetic that `convert` does exactly from the hops.
- **Every pair in a connected component, inverses included**: quadratic in clocks for a fleet on one GPS time;
  `convert` already inverts exactly at query time.
- **An unstated window end as `OPEN`** (as identity reads an `IdentityLink`): converting past where a sync was
  observed is extrapolation, which root ADR 0060 §5 and §7 forbid.
- **Placing civil-declared clocks on `CivilClock`** for mapping windows: it would join two hosts' POSIX clocks
  that no mapping joins, the silent synchronisation root ADR 0060 rejects.
- **Relaying estimates from `consolidate/` as `observed`**: it would launder a fit into evidence.
- **A later-arriving restatement of one window wins**: arrival order is not in the snapshot, and "later is
  better" is an assumption; both stand as a conflict until a person's assertion settles it.

## Consequences

- Context relates any two clocks a chain of evidence joins, exactly, with the bound the evidence gives, and gets
  the missing hop when nothing joins them.
- Two mapping claims per mapping and per chain; chain count is bounded by paths of at most four hops.
- A correction that restates a whole window is a conflict, not a revision; a retraction assertion (root ADR
  0062) is the way to settle one when Memory reads them for mappings.
- Revisit when MVL-132 lands withdrawal (the `xfail`s flip), when the Ledger exposes arrival order per record,
  or when fleets need chains longer than four hops.
