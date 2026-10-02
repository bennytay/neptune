# 0002 — Graph tiers, node and edge schema, and the bi-temporal claim model

- Status: Accepted; §4 and the worked examples superseded by 0005
- Date: 2026-10-02
- Issue: MVL-102

## Context

Memory turns Ledger packages into a graph that Context, Deploy and Learn query by time. A fact about a robot changes:
calibrations are replaced, machines move between sites, operators correct guesses, and maintenance makes old
inspection findings stale. Evidence often arrives late or out of order. If the schema overwrites facts, Memory
cannot say what was true at a given time, or what it believed at a given time. If superseding depends on arrival
order or on undefined tie-breaks, two replays of the same Ledger give different graphs, which breaks the
programme's determinism rule. The compiler already defines time (`Timestamp` in a named clock domain),
evidence (`EvidenceRef`), missingness (`Knowledge`) and assertion kinds, so Memory must reuse them, not redefine
them. Clocks must never be silently converted.

## Decision

Code: `neptune_memory/schema/` (`nodes.py`, `interval.py`, `claim.py`, `predicates.py`, `supersede.py`).
`GRAPH_SCHEMA_VERSION` stays `0` until MVL-105 publishes v1 (JSON Schema, `MemoryReader`).

### 1. Tiers and nodes

- **Episode tier**: Ledger records and evidence refs. These are never nodes and never copied. A claim refers to
  them by id: `LedgerRecordRef(record_id)` as an object, and `EvidenceRef`s in its provenance.
- **Entity tier** (`NodeType`): `machine`, `sensor`, `site`, `zone`, `asset`, `task`, `person`, `software_version`,
  `model_version`, `configuration`, `policy`, `run`, `episode`. `machine` covers every robot: arm, AMR, legged,
  humanoid, aerial, marine and vehicle.
- **Context tier**: `deployment`, `fleet` and `programme`. Each has a summary, which is a `has_summary` claim.
  A generated summary is an inferred claim produced under `derived/`.
- A node is `NodeRef(node_type, node_id)` and nothing else. Every attribute and every edge is a claim.
  `consolidate/` derives `node_id` (MVL-103); here it is opaque, non-empty text.
- **`person` is declared only.** An inferred claim that names a person, as subject or object, is refused
  (`declared_only`).

### 2. The claim

| Field | Type | Meaning |
|---|---|---|
| `subject` | `NodeRef` | what the claim is about |
| `predicate` | registered name | from the vocabulary (§5) |
| `object` | `NodeRef` \| `TypedLiteral` \| `LedgerRecordRef` | a node makes the claim an edge |
| `valid_from`, `valid_to` | `Timestamp`, `Timestamp` \| `OPEN` | valid time, `[from, to)` on one clock (§3) |
| `recorded_at` | `LedgerTx` | when the Ledger recorded it |
| `superseded_at` | `LedgerTx` \| `OPEN` | when this version stopped being current |
| `assertion_kind` | `observed` \| `stated` \| `inferred` | compiler `AssertionKind` or compiler `INFERRED` |
| `confidence` | `Knowledge[float]` | `NotApplicable` for observed/stated; `Known(p∈[0,1])` or `Unknown` if inferred |
| `provenance` | `ClaimProvenance` | ≥1 `EvidenceRef`, Ledger record ids read, consolidator id + version + config hash |
| `supersedes` | sorted `ClaimId`s | the claims this version superseded |

- `TypedLiteral(datatype, value, unit)`: `text`, `integer`, `real` (a finite float, or `NonFinite` as the
  source wrote it), `boolean`, `quantity` or `instant` (a `Timestamp`). Only a `quantity` has a unit, and that
  unit is `Known(Unit)`, `Unknown` or `Ambiguous` exactly as declared, and it inherits the claim's provenance.
  Objects compare by canonical JSON, so `5 mm` and `0.5 cm` (or `5` and `5.0`) are different objects.
- `id = "claim:" + sha256(canonical JSON of {subject, predicate, object, valid, assertion_kind, confidence,
  provenance})`. It covers what is asserted and by whom, never the bookkeeping (`recorded_at`, `superseded_at`,
  `supersedes`). Re-recording the same assertion therefore produces the same claim.
- **Edges are claims.** There is no separate edge type, so every relationship is bi-temporal.

### 3. Two time axes

- **Valid time** uses compiler `Timestamp`s on one clock domain, stored as declared, never converted to UTC. The
  domain is either a compiler `TimestampDomain` (a robot's boot clock, an MCAP `log_time`) or a `CivilClock`
  (timescale, epoch and resolution). A `CivilClock`'s `domain_id` is derived from its definition, so civil time
  from any source lands on the same timeline. A consolidator maps a source domain onto a `CivilClock` only when
  that domain's timescale, epoch and resolution are all `Known` and match. A civil time with no stated zone stays
  on its source's domain. Ordering two clocks raises `DomainMismatchError`. Relating clocks is a `ClockAlignment`
  record, never an operator.
- **Transaction time** is `LedgerTx`, the Ledger's non-negative commit sequence number. It is totally ordered and
  never a wall clock. If MVL-85 defines a different shape, a superseding ADR adapts `LedgerTx`.

### 4. Superseding

`resolve(claims, registry, priorities) -> Resolution(claims, findings)` is a pure function. It drops resolver
closure versions from its input, clears bookkeeping, collapses duplicate ids to the earliest `recorded_at`, and
folds the assertions in **arrival order** `(recorded_at, consolidator priority, claim id)`. Within one
transaction, the higher priority arrives later; the claim id breaks any remaining tie, so the order is total.
Priorities are configuration: a consolidator with no priority is a `ValueError`.

1. **Contradiction**: same subject, a `one`-cardinality predicate, a different object, and overlapping valid
   intervals on the same clock. `many` predicates never contradict, and the same object corroborates.
2. **Winner** on the overlap: the higher assertion rank (observed = stated > inferred), then the later
   `valid_from`, then the later arrival.
3. The **loser's** current version gets `superseded_at = arriving.recorded_at`. If it began before the winner, a
   **closure version** is recorded at that transaction with `valid_to = winner.valid_from`,
   `supersedes = (loser,)` and provenance `memory.supersede` (the loser's original evidence plus the winner's;
   `config_hash` hashes `{narrows, winner}`, so distinct narrowings never share an id). Otherwise nothing of it
   remains current. The resolver cuts tails, never heads, so a loser is not resurrected after a bounded winner.
4. The **arriving** claim's `supersedes` lists the claims it beat. If it loses on arrival, it is stored with
   `superseded_at = recorded_at` (it was never current) and gets a closure version, or, if nothing is left, an
   `overridden_on_arrival` finding.
5. **Different clocks** are never compared. The pair is reported as a `clock_mismatch` finding and both stay
   current.

`as_of(history, tx)` returns the versions with `recorded_at ≤ tx < superseded_at`. Nothing is ever deleted.

Preconditions raise instead of producing findings, because they are caller bugs and not hostile data:
`consolidate/` turns non-conforming claims into findings before they reach `resolve`. These preconditions are
a claim that breaks the vocabulary (`ClaimSchemaError`), a consolidator with no priority, a priority for the
reserved `memory.supersede`, and an input closure version that this resolution does not recreate, which is
forged resolver output.

**Property specification**, tested with hypothesis and an exhaustive permutation test:

- P1, order-free: byte-identical output for any permutation of the input.
- P2, idempotent: `resolve(resolve(x).claims) == resolve(x)`, and duplicates change nothing.
- P3, nothing deleted: every input id is in the output with identical content.
- P4, `as_of` is history: `as_of(resolve(all), tx)` equals the current claims of `resolve(claims recorded ≤ tx)`.
- P5, consistent: no two current claims for one (`one` predicate, subject, clock) overlap with different objects.
- P6, total arrival order: no two distinct claims share an arrival key.

### 5. Vocabulary governance

`predicates.py` registers each predicate as a `PredicateSpec(name, version, domain node types, range node or
value types, cardinality, description)` in an immutable `PredicateRegistry` (`CORE_PREDICATES`,
`VOCABULARY_VERSION = 1`). `violations` and `check_claim` refuse unknown predicates and wrong subject or object
types, and `resolve` refuses non-conforming claims. `extend` adds names. A new version of an existing name must
widen it: a higher version, the same cardinality, and a superset domain and range. Every old claim therefore stays
valid, and narrowing needs a new name.

### Worked examples (executed in `tests/test_supersede_examples_memory.py`)

Civil dates are day ticks on `CivilClock(posix, unix, 86400 s)`.

1. **Calibration replaced (arm).** `wrist-camera has_calibration cal-03-02` is observed from 2 Mar (tx 1). The
   July recalibration is observed from 14 Jul (tx 2). At tx 2, March's version gets `superseded_at = 2`, a closure
   version recorded at tx 2 holds it over [2 Mar, 14 Jul), and July's claim `supersedes` March's. `as_of(1)` still
   shows March as open-ended.
2. **Robot moved between sites (AMR).** `amr-12 located_at warehouse-a` is stated from 2 Mar (tx 5) and
   `warehouse-b` from 10 Jun (tx 9). The current claims are a [2 Mar, 10 Jun) and b [10 Jun, open). A position on the
   AMR's boot clock contradicts on another clock, so it yields `clock_mismatch` and nothing is coerced.
3. **Operator overrides an inferred identity (quadruped).** `run recorded_by spot-07` is inferred
   (confidence 0.82) and `recorded_by spot-03` is stated by the operator log, with the same `valid_from`. The stated
   claim wins on rank. If the guess arrived first, it is superseded and the operator claim `supersedes` it. If the
   guess arrives after, it is `overridden_on_arrival` and was never current.
4. **Stale fact superseded by a maintenance record (marine ROV).** `maintenance_state "thruster 3 fault"` (an
   inspection on 1 May, tx 2) is closed by `"operational"` (a maintenance record on 10 Jun, tx 7). An older
   `"operational"` record from 1 Apr, filed late at tx 9, is narrowed on arrival to [1 Apr, 1 May). History reads
   operational → fault → operational, and `as_of(8)` shows the graph before the late filing.

## Alternatives considered

- **Overwrite or mutate the old edge** (set `invalid_at` in place): the pre-correction belief is lost, so `as_of`
  answers wrongly for transactions before the correction.
- **Last-writer-wins by arrival**: a late-filed old record would overwrite a newer fact, and an inferred guess
  could override an operator.
- **Separate edge and attribute types**: two schemas would have to stay bi-temporal in step. One claim type is
  smaller.
- **Normalise valid time to UTC**: this violates the no-silent-assumption rule and is impossible for boot and
  simulated clocks. Refusing to compare across clocks is honest.
- **Wall-clock `recorded_at`**: not deterministic. The Ledger's sequence number is.
- **Confidence on every claim**: deterministic claims have no probability, so `NotApplicable` states that
  explicitly.

## Consequences

- MVL-103 imports `Claim` and builds claims; field names are fixed by this ADR.
- MVL-105 adds JSON Schema export, `MemoryReader` and the v1 pin on top of these types.
- Context consumers read `as_of` and valid-time filters, and never re-resolve.
- Cross-clock facts stay unresolved until `ClockAlignment` (compiler MVL-36) is consumed. That gap is visible as
  findings.
- Revisit this ADR if the Ledger's transaction clock is not a sequence, if resurrection after a bounded winner is
  needed, or if consumers need per-predicate superseding policies beyond `one`/`many`.
