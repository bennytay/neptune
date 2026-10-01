# 0005 — Split closures, bi-temporal findings and the resolver's config

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-196

## Context

ADR 0002 §4 defined the superseding resolver. ADR 0003 §3 added lineage retirement to it. The review of #53
found five problems that must be fixed before MVL-105 publishes graph-schema v1:

- **Lost tails.** A losing claim kept only the part before its winner, so evidence nothing contradicted
  disappeared. If a source stated `a` over [0, 10) and another claimed `b` over [2, 4), nothing held [4, 10).
  Likewise a stated [0, 3) plus an inferred `b` from 2 left no `b` on [3, open).
- **Findings outside time.** `ResolutionFinding` had no transaction, and `as_of` returned only claims. A
  `clock_mismatch` pair was therefore current with no conflict marker.
- **Closure hash ignored config.** A closure's `config_hash` covered `{narrows, winner}` but not the
  priorities or vocabulary that decided the winner. A different configuration produced the same lineage.
- **Implicit UTC in the examples.** ADR 0002's worked examples put zone-less dates on
  `CivilClock(posix, unix, 86400 s)`. That is an implicit UTC, which ADR 0002 §3 itself forbids.
- **Person.** ADR 0002 §1 says "person is declared only" but never said whether observed or stated claims may
  name a person.

## Decision

Code: `neptune_memory/schema/supersede.py`, `interval.py` (`Interval.minus`) and `predicates.py`.
This ADR supersedes ADR 0002 §4 and its worked examples. ADR 0002 §1–§3 and §5 stand, refined only by §5
below. ADR 0003 §3 (lineage) stands, and §6 restates how it interacts with split pieces. `GRAPH_SCHEMA_VERSION`
stays `0`. `RESOLVER_VERSION` becomes `"2"`.

### 1. Split closures

`resolve(claims, registry, priorities) -> Resolution(claims, findings)` keeps ADR 0002's input handling:

- It drops closures, clears bookkeeping and collapses duplicates to the earliest `recorded_at`.
- It folds in arrival order `(recorded_at, consolidator priority, claim id)`.
- It raises on the same caller bugs as before, and on a priority that is not an `int`.

Two claims still contradict on the same terms: same subject, a `one` predicate, a different object, and
overlapping valid intervals on one clock.

1. **Winner.** The winner has the higher assertion rank (observed = stated > inferred), then the later
   `valid_from` *of the original assertion*, then the later arrival. A closure piece competes as its original,
   never with its own start, so a split piece starting at 4 does not outrank a claim from 3.
2. **The arriving claim `A`.**
   - *Winners* are the overlapping current rival versions that `A` does not beat. With no winners, `A` is
     current as asserted.
   - Otherwise `A`'s own version is stored with `superseded_at = A.recorded_at`, because it was never current
     whole. Every sub-interval of `A.valid` that no winner covers becomes a closure version.
   - If no sub-interval remains, `A` gets an `overridden_on_arrival` finding that names the winners.
3. **Losers.** A loser is an overlapping rival version that `A` beats and that overlaps one of `A`'s kept
   parts. Its version gets `superseded_at = A.recorded_at`. Every sub-interval of the loser that `A`'s kept
   parts do not cover becomes a closure version: before, after, or between `A`'s parts. `A`'s stored version
   lists the losers in `supersedes`.
4. **No resurrection.** A part a winner took never returns, even if that winner is later narrowed or retired.
   Pieces only shrink.
5. **A closure version** copies the version it narrows except in these fields:
   - `valid` is the kept sub-interval.
   - `recorded_at` is the arriving claim's transaction.
   - `supersedes` is `(narrowed version id,)`. Following it past closures leads to the original assertion.
   - `provenance` has the original assertion's evidence, then the evidence of the claims that took the rest
     (de-duplicated). Its records are the union, and its transform is `memory.supersede`, version `"2"`.
   - `config_hash` is `config_hash({narrows: narrowed version id, resolver: resolver_config_hash})`.

   The id derives from content like every claim id, so pieces have deterministic ids. Pieces of one version
   differ by interval. Pieces of two versions differ by `narrows`, even when they share evidence.

### 2. Findings are bi-temporal

`ResolutionFinding(code, claim, others, recorded_at, superseded_at)`. `Resolution.findings` is ordered by
`(recorded_at, claim, code, others)`.

- **`clock_mismatch`** names one pair: two versions of a `one` fact with different objects on different clocks.
  `claim` is the later arrival and `others = (earlier,)`. The finding is recorded at the transaction where both
  versions become current. It is superseded when the first of them stops being current. A narrowed version's
  pieces get their own findings, so the marker follows the versions it names.
- **`overridden_on_arrival`** is recorded at the arrival and never superseded: that claim was never current.
- **`as_of(resolution, tx) -> Resolution`** returns the claim versions *and* the findings with
  `recorded_at ≤ tx < superseded_at`.

### 3. The resolver's config is in the hash

`resolver_config(registry, priorities)` is `{priorities, vocabulary: registry.to_json(), vocabulary_version:
VOCABULARY_VERSION}`, and `resolver_config_hash` hashes it. Every closure's `config_hash` covers this hash, so
any of these changes is a new closure lineage with new closure ids:

- a change of priority;
- a vocabulary extension or widening;
- a vocabulary version bump.

Asserted claims' ids do not change. A history resolved under one configuration contains closures that another
configuration does not recreate. Passing it to `resolve` under the new configuration therefore raises (forged
resolver output). It is re-resolved from `assertions(history)`.

### 4. Declared clocks in the worked examples

A date with no stated zone is not an absolute instant, so it stays on its source's own domain (ADR 0002 §3).
The examples are restated on times each record *declares* as POSIX instants: Unix seconds, from a source
domain whose timescale, epoch and resolution are all `Known`. These sit on `CivilClock(posix, unix, 1 s)`.
Written dates are UTC labels for the reader only: "2 Mar" is 1772409600, `2026-03-02T00:00:00Z`.

### 5. Person

`person` stays declared only, and ADR 0002 §1 is refined as follows:

- **Inferred claims never name a person** (`declared_only`).
- **Observed and stated claims may name a person** only when both of these hold:
  - the node id is a declared identifier `<namespace>:<value>` (a valid `LogicalId`, the form ADR 0003 §1
    derives);
  - the claim cites at least one Ledger record in `provenance.records`.

  Anything else is `undeclared_person`. A badge reader's log may observe `badge:4411`. A person seen in a frame
  is not identified by observation: recognising them is inference and is refused.

`check_claim` enforces this, so `resolve` and the consolidator runner enforce it too.

### 6. Lineage

ADR 0003 §3 is unchanged. Retiring a lineage supersedes every current version whose original assertion is
in that lineage, so every split piece is retired, including pieces cut at the retiring transaction itself.
`LineageError("lineage_reuse")` names the full lineage: consolidator id, version and config hash.

### Property specification (tested with hypothesis and exhaustive permutations)

P1–P6 are ADR 0002's: order-free, idempotent, nothing deleted, `as_of` is history, consistent, total arrival
order. They now also hold under lineage upgrades interleaved with splitting. P3 also checks that every closure
is a sub-interval of its root assertion and starts with its evidence. P4 covers findings: `as_of(resolve(all),
tx)` has the claims and findings that `resolve(claims recorded ≤ tx)` leaves current. Two properties are new:

- **P7, uncontested parts survive.** Without an upgrade, every instant of every `one` assertion is held by
  some current version on its fact and clock.
- **P8, retirement is total.** No current version, split piece or not, belongs to a replaced lineage.

### Worked examples (executed in `tests/test_supersede_examples_memory.py`)

1. **Calibration replaced (arm).** `wrist-camera has_calibration cal-03-02` is observed from 2 Mar (tx 1).
   July's recalibration is observed from 14 Jul (tx 2).
   - At tx 2, March's version is superseded, and a closure holds it over [2 Mar, 14 Jul).
   - July's claim `supersedes` March's.
   - `as_of(1)` shows March open-ended.
2. **Robot moved between sites (AMR).** `amr-12 located_at warehouse-a` is stated from 2 Mar (tx 5), and
   `warehouse-b` from 10 Jun (tx 9).
   - The current claims are a over [2 Mar, 10 Jun) and b over [10 Jun, open).
   - At tx 10, a position on the AMR's boot clock gives a `clock_mismatch` with a's closure, recorded at tx 10.
   - `as_of(10)` carries the finding and `as_of(9)` does not.
3. **Operator overrides an inferred identity (quadruped).** `run recorded_by spot-07` is inferred
   (confidence 0.82). `spot-03` is stated by the operator log. Both start at the same instant, and the stated
   claim wins on rank.
   - If the guess arrived first, the guess is superseded.
   - If it arrived second, the guess is `overridden_on_arrival`, because the winner covers all of it.
4. **Stale fact superseded by maintenance (marine ROV).**
   - `maintenance_state "thruster 3 fault"` (inspection, 1 May, tx 2) is closed by `"operational"`
     (maintenance, 10 Jun, tx 7).
   - An older `"operational"` record from 1 Apr is filed late at tx 9. It is split on arrival into [1 Apr,
     1 May) and [10 Jun, open), and the latter corroborates the repair.
   - History reads operational → fault → operational. `as_of(8)` shows the graph before the late filing.
   - The ROV logbook's bare date `2026-05-20` has no zone, so it stays on the logbook's own day clock. It is
     never ordered against the civil records, only flagged `clock_mismatch` while both are current.

## Alternatives considered

- **Keep tail-cutting** (ADR 0002): it drops evidence nothing contradicts, and the result depends on whether a
  winner is bounded. Rejected by the issue and the coordinator.
- **Resurrect a loser when its winner is retired or narrowed**: the current state would depend on re-folding
  past decisions. A part taken by a winner stays taken. ADR 0002 already rejects resurrection.
- **One finding per arrival with every mismatched rival in `others`**: its active window is undefined once
  any one rival changes. One finding per pair is active exactly while both versions are.
- **Findings outside `as_of`**: a consumer reading a snapshot sees two conflicting current claims and no
  marker.
- **`config_hash` over `{narrows, winner}` only**: a priority change would keep the closure lineage. A hash of
  the config alone, without `narrows`, collides for corroborating claims with equal evidence.
- **Allow observed or stated claims to name any person node**: free text or perception would put people in the
  graph with no declared identity behind them.

## Consequences

- A loser narrowed by k winners yields up to k + 1 closure versions. Storage pays for kept evidence.
- Changing priorities or the vocabulary changes every closure id. Stored histories are re-resolved from their
  assertions, never patched.
- `as_of` takes and returns a `Resolution`. MVL-105's `MemoryReader` builds on this signature.
- `clock_mismatch` is computed over version pairs per fact, which is quadratic in that fact's versions.
  Revisit if one fact accumulates many cross-clock versions.
- Revisit when `ClockAlignment` (compiler MVL-36) is consumed, which turns some mismatches into comparisons.
  Revisit also if consumers need per-predicate superseding policies beyond `one`/`many`.
