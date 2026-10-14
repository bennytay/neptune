# 0029 — Clock descriptions as stated claims: declared units, epochs and civil zones (graph-schema 2.5.0)

- Status: Accepted
- Date: 2026-10-15
- Issue: MVL-191

## Context

A filmed Demo v1 run asked "When exactly did the stop on ARM-3A happen on 2026-09-14? Do the records agree?".
The agent answered "I can't give you a single clock time": every time it saw was `tick 1789396358 on clock
rec:sha256:7f95…`. The gold answer (Q7) is 14:32:38 in the syslog and 14:33:10 in the CMMS log, both PLANT-2
local wall time in America/New_York, as Deploy's declaration states for each export.

The facts that make those ticks readable are already in the Ledger, and Memory never carried them:

- Each clock is a compiler `TimestampDomain` (root ADR 0005 §2). It states the clock's `field` and `scope`, its
  `role`, its `resolution` (exact seconds per tick), its `epoch` and its `timescale`, each `Knowledge`-wrapped.
- A civil clock has a `civil_time_zone` companion (root ADR 0061 §1). Its zone is the IANA name as declared,
  or `Unknown` / `NotCovered`. Deploy writes one per civil clock a mapping reads. For the syslog and the
  downtime log, the zone comes from the caller's `--source-zone` in the mapping's transform config (Deploy ADR
  0017 §2), and the companion cites that configuration.
- A wall clock's ticks count from 1970-01-01T00:00:00 of that wall clock (root ADR 0023 §2, ADR 0061 §2).
  They are not POSIX instants (Deploy ADR 0017 §1).

Memory's graph holds only `valid: {start: {domain_id, ticks}}`. The clock nodes (ADR 0011 §1) have edges
(`has_clock`, `maps_to`), but nothing says what their ticks count. Context cannot render a time without going
back to the Ledger, which it does not read for this.

## Decision

1. **New predicate `clock_description`** (`clock` → new value type `clock_description`, cardinality `one`,
   vocabulary 14). There is one claim per clock node. Its object is a `ClockDescription`
   (`neptune_memory.schema.clock_description`, JSON `#/$defs/ClockDescription`), copied and never derived:
   - `field` and `scope`: the domain's own, verbatim.
   - `role`, `resolution`, `epoch` and `timescale`: the domain's states as declared. `Unknown` and `NotCovered`
     stay what they are. A state's own citation is lifted to the claim, so every state inherits the claim's
     provenance, as a `ClockMap` parameter does (ADR 0011 §2). `KnownAbsent` is refused, since it always cites
     its own evidence: such a domain gets a `clocks.unreadable_state` finding and no claim.
   - `civil_time_zone`: the companion's zone state as declared (`Known`, `Ambiguous`, `Unknown` or
     `NotCovered`). A companion never holds `NotApplicable`, so here `NotApplicable` means exactly "no
     companion names this clock". That says nothing about whether the clock is a wall clock. Any other state
     does say it is one, because only a civil clock has a companion.
   - `zone_declared_by`: the `adapter_id` of the companion's transform record, as that record states it
     (`deploy_event_log_map`, `deploy_lifecycle_map`). This tells "the source states the zone" apart from "a
     mapping's configuration states it". It is `Unknown` when the transform record is not in the Ledger or
     several adapters declare the zone, and `NotApplicable` when no companion names the clock.
2. **`memory.clocks`** (`consolidate/clocks.py`, version 1, takes no config) reads every `timestamp_domain`,
   `civil_time_zone` and `transform_record` with the compiler's strict readers. Records are de-duplicated by
   id across packages, and an id with two contents is a `clocks.record_conflict` finding and is used nowhere.
   The claim:
   - holds over the clock's whole time: `[INT64_MIN, open)` on the clock itself. It says how the ticks count,
     not something that happened at a time.
   - cites the domain's evidence, each companion's and any evidence a state cites of its own.
   - lists as records the domain, the companions and their transforms.
   - is `stated` when every provenance is, else `observed` (an MCAP `log_time` is `observed`).

   Several companions on one clock leave its zone in doubt even when they agree (root ADR 0061 §3,
   `civil_zone_repeated`). That gives a `clocks.zone_repeated` finding, and the zone is `Ambiguous` between the
   names they state, or `Unknown` when there are fewer than two.
3. **Nothing converts.** The description never names an instant, an offset or a UTC time. Reading a wall time
   as an instant needs a tz database release, and relating two clocks needs a stated mapping. Both stay where
   root ADR 0061 §2 and ADR 0011 put them. Two clocks that declare the same zone are still two clocks. Only a
   consumer comparing their declared wall times may say so, and it says it is a difference of declared wall
   times.
4. **Contract.** graph-schema **2.5.0** (minor, additive) adds `clock_description` and its value type. Every
   2.x golden still validates. The golden graph adds `memory.clocks` (priority 5): 16 descriptions of the
   worked examples' clocks. The acceptance snapshot gains 77 claims (1021 → 1098), one per domain of the
   corpus's two packages, and no other claim id moves. Context and Deploy pin 2.5.0 in the same change.

## Alternatives considered

- **Extend `memory.time`** (ADR 0011) with the descriptions. A new version of `memory.time` re-ids every
  `has_clock`, `maps_to` and `clock_map` claim in every graph for no change in what they say. A separate
  consolidator only adds claims.
- **`declared_value` claims on the clock, one per property** (ADR 0025). A `DeclaredValue` holds a value, so
  `Unknown`, `NotCovered` and `Ambiguous` would have to be dropped or turned into text. One literal keeps every
  state, and a reader gets the whole clock in one claim.
- **Write `civil_time_zone: null` when no companion exists.** Canonical JSON forbids `null`, because
  missingness is `Knowledge` (ADR 0004). `NotApplicable` is free, since a companion can never hold it.
- **Treat `epoch unix` with timescale `Unknown` as a wall clock** (the shape ADR 0023 §2 gives a zone-less
  civil time). Another adapter may declare a Unix-epoch clock whose timescale it does not know. Only the
  companion is a record that the clock is civil, so only the companion says so here.
- **Store the zone on each event claim, or convert wall times to UTC at consolidation.** The first repeats one
  fact about a clock on every claim. The second is the silent conversion that root ADR 0061 §2 and
  non-negotiable 4 forbid.

## Consequences

- Context can render a tick in its clock's own terms (`2026-09-14 14:32:38`, wall clock, America/New_York as
  declared) and give each clock a readable label, without the Ledger and without converting (Context ADR
  0022).
- Every graph gains one claim per declared clock. They are not edges, so traversals and budgets are unchanged
  unless a reader asks for them.
- A new adapter that declares a zone in its source's own bytes shows up as its own `zone_declared_by` with
  nothing to change here.
- Revisit when Memory reads a derived tz-database transform (instants from wall times), when a companion grows
  a per-row form (root ADR 0061 §3), or when the Ledger exposes domains through the catalog API rather than the
  export.
