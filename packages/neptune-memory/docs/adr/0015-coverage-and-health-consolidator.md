# 0015 — The coverage and health consolidator

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-129

## Context

"Was the lidar recording during the near-miss?" and "is this run usable for training?" are coverage questions:
what each run recorded, where a stream has holes, how fast it sampled against what its source declared, and what
the compiler found wrong with the bytes. The anomaly scores of MVL-188 and every episode query build on them.

The compiler states the pieces separately. A `Stream` (root ADR 0018 §2) declares its channel, its run, the clocks
its samples carry and what its source's index or summary declares: `message_count` and inclusive `first` / `last`
instants on one of those clocks. What the series actually holds is the Ledger's: registration reads every series
file's `time/<i>` columns into the time index (Ledger ADR 0015 §2), one row per stream and clock with the least and
greatest known tick and how many rows have, or lack, a known tick there. That row is not published by the catalog
API yet. `IngestFinding`s state what went wrong (a truncated chunk, a logger dropout) with the records they qualify
and a severity. A `SnapshotBinding` binds a run to a `HardwareConfiguration`, whose `HardwareComponent`s of category
`sensor` declare their identifiers; an `Image` or `Video` declares the device that captured it. **No compiler record
says which sensor a `Stream` recorded**, and **no record states a sensor's configured rate**.

Getting this wrong is worse than saying nothing: a camera reported absent because no stream is *named* like a
camera, a gap invented where samples merely lack a timestamp, a rate tolerance nobody declared, or a dropout
smoothed over would each state a fact the evidence never did. Claim objects cannot be `Unknown` or `KnownAbsent`
(ADR 0002 §2), and two clocks are never compared (ADR 0002 §3).

## Decision

### 1. What coverage reads, and how it is parsed

`consolidate/coverage_records.py` parses; `consolidate/coverage.py` (`memory.coverage`, version `1`, no
configuration: `coverage.unknown_config`) decides. Compiler kinds are read with the compiler's strict readers:
`run`, `run_assembly` and `source_revision` (through `run_records`), `stream`, `ingest_finding`,
`timestamp_domain`, `snapshot_binding`, `hardware_configuration`, `hardware_component`, `image` and `video`. A
refused record, or a declared identifier that is blank or padded (ADR 0006 §9), is `coverage.malformed_record`; an
inferred one `coverage.inferred_record`; one key with two contents `coverage.record_conflict`, neither used.
Records are admitted by id across packages; assemblies and revisions within their package (ADR 0009 §1).

One kind is a Ledger stand-in until the catalog API publishes the time index's series rows:
`series_interval {stream, clock, first, last, rows_known, rows_unknown}`, exactly the Ledger's `time_interval`
row with `subject = "series"` (`rows_known` ≥ 1, `first` ≤ `last`, `first = last` for one row). Like the Ledger,
there is no row for a clock no sample has a known tick on: a missing row is *not indexed*, never "no samples".

Nodes: a run is `runs.run_node` (ADR 0009 §2); a stream is `record:<stream record id>`, as the Ledger anchors a
stream thread on its record; a sensor is one node per `Known` identifier its component declares (the Ledger keys a
sensor thread per declared identifier), else `record:<component record id>`. Instants on a clock that declares
itself civil are placed on that `CivilClock`, as runs and identity place them.

### 2. Recorded spans, gaps and rates (per stream and clock)

- `recorded(stream → run)`, `observed`, over `[first, last + 1 tick)` of each series row: the series holds samples
  from its first to its last known tick on that clock. A row at the clock's last tick is not placed
  (`coverage.end_unrepresentable`).
- `gap(stream → run)`, `observed`, only where the stream's declared extent predicts samples: the source declares
  samples at its `first` and `last` instants, so `[declared first, series first)` and `[series last + 1, declared
  last + 1)` on the declared extent's clock, each clamped to the declared extent (outside it the source predicts
  nothing). Two clocks that declare one civil timeline are one clock here, as `place` puts them. Never when any row of the stream lacks a known tick on that clock
  (`coverage.untimed_samples`: an untimed sample may lie in the hole); never on a side where the series reaches
  past the declared extent (`coverage.extent_disagrees`); never inside the span, which a min/max row cannot see. A
  declared extent on two clocks or inverted predicts nothing (`coverage.declared_extent_unusable`); a declared
  extent whose clock has no series row is `coverage.series_not_indexed`; a series row naming a stream or clock the
  Ledger does not hold is `coverage.dangling_series`.
- `rate_declared` and `rate_observed` (stream → `quantity` in Hz): `(n − 1)` sample intervals over the first-to-last
  span times the clock's stated resolution, over that span. Declared: `n` the `Known` `message_count` with a usable
  declared extent, with the count's `assertion_kind`. Observed: `n = rows_known`, only when `rows_unknown = 0`. No
  stated resolution, fewer than two samples or a zero span: no claim, `coverage.rate_undetermined` with the reason
  (`clock_resolution_unstated`, `fewer_than_two_samples`, `zero_span`, `end_unrepresentable`).
  The value is the exact ratio rounded once to binary64. **Both claims stand side by side; nothing compares
  them**: no source declares a tolerance, so a "rate mismatch" would be Memory's judgement.

### 3. Integrity findings

Each `IngestFinding` becomes `integrity_finding(subject → text)`, `observed`, whose object is the compiler's severity
verbatim (`error`, `warning`, `info`); the finding is in the claim's records and its subject and related evidence in
its evidence. Subjects: each run and stream the finding's `records` name (any category: a dropout is `missing`),
and each run one of whose files (its declaration's bytes, or any member of an assembly naming it) the finding's
subject cites when its category says bytes did not reach the output (`corrupt`, `limit`, `failed`). It holds over
the run's span (ADR 0009 §2's primary placement: `[first, last + 1 tick)`, open when `last` is not stated); a run
with no `first`, or a `last` before it, places nothing (`coverage.unplaced_run`). A finding about another record
(a software configuration's missing release) is not the run's health and is not lifted.

### 4. Sensor presence

For each run and each `sensor` component of each hardware configuration a `SnapshotBinding` binds to it
(`coverage.dangling_binding` when the run or configuration is not in the Ledger), over the run's span:

- `sensor_recorded(run → sensor)` when a file of the run (an `Image` or `Video` whose bytes are the run's or a
  member's) declares one of the sensor's `Known` identifiers among its capture's device identifiers, citing it.
- `sensor_not_recorded(run → sensor)` (**KnownAbsent**) only when nothing in the run could be the sensor's: the run
  has no `Stream` (streams declare no sensor); every image or video of it definitely declares another configured
  sensor and not possibly this one; every file holding its samples (its assemblies' `recording` members, or, with no
  assembly, the bytes that declare it) is such an image or video; every member's revision is in the Ledger; the
  run's span is closed on one clock; no integrity finding names the run or its streams; and no `run`,
  `run_assembly`, `source_revision`, `stream`, `ingest_finding`, `image` or `video` record anywhere in the Ledger
  was unreadable or in conflict (it could be this run's).
- `sensor_presence_unknown(run → sensor)` (**Unknown**) otherwise, with `coverage.presence_undecided` naming each
  reason: `streams_declare_no_sensor`, `files_not_attributed`, `members_unresolved`, `recording_not_closed`,
  `integrity_findings`, `ledger_records_unreadable`. An `Ambiguous` device identifier that includes the sensor's is never a record.

Presence is about the run: a binding's validity window is not used, because whether a sensor recorded in the run
and whether nothing in the run could be its data do not depend on when within the run the configuration applied
(MVL-127 places that). A run with sensors in two bound configurations has the union. Because no compiler record
binds a `Stream` to a sensor, a camera configured on a run recorded as a bag of lidar and odometry streams is
**Unknown, not absent**: by the evidence, any stream could hold its images.

### 5. Missingness is the predicate

As for identity and configuration (ADR 0003 §1.3, ADR 0010 §4), the three presence states are three predicates. A
stream with no declared count, no declared extent or no series row gets no rate, gap or recorded claim: what is not
stated is `NotCovered`, and a finding is raised only where stated inputs fail to determine a value.

### 6. Vocabulary and contract

New predicates, all `many` (a stream has spans, gaps and rates per clock; a run has one claim per finding and
sensor): `recorded`, `gap` (stream → run), `rate_declared`, `rate_observed` (stream → quantity), `integrity_finding`
(run, stream → text), `sensor_recorded`, `sensor_not_recorded`, `sensor_presence_unknown` (run → sensor).
`VOCABULARY_VERSION = 10` (6 to 9 are other G2 consolidators, renumbered on merge); graph-schema **1.8.0** is a
minor release (the vocabulary annotation of ADR 0010 §6), earlier versions still load and pass the suite. The golden
plan is unchanged; only its resolver generation moves.

## Alternatives considered

- **Match streams to sensors by topic, schema name or frame** (`/camera/image` is the camera's): a name is not a
  declaration; the compiler's own media semantics are inferred and belong in `derived/`.
- **Report a configured sensor with no matching stream as absent**: the issue's "camera configured, no image
  stream" would then be a definite fact resting on a name match. It is Unknown until the compiler binds streams to
  sensors.
- **A declared-rate stand-in kind** (a sensor's configured Hz): no compiler record carries it; inventing one would
  put a shape in front of the compiler. The declared rate is the source index's own count over its extent.
- **Infer gaps inside the span from a rate**: a min/max row cannot locate them; claiming one would place a hole the
  evidence does not show. Sample-level gaps wait for the Ledger to publish row-level coverage.
- **Treat a missing series row as "no samples"**: the Ledger omits a row for a clock no sample has a tick on, and a
  stream may have no series file at all; neither says nothing was recorded.
- **Carry the finding as a record-reference object**: queryable only by fetching every record; the severity text
  answers "runs with error findings" from claims, and the record stays one hop away in provenance.
- **Use binding windows for presence**: the compiler's bindings state `Unknown` validity today; presence would be
  undecided almost everywhere for no gain in truth.

## Consequences

- Context can answer "what did this run record, where are its holes, what did the compiler flag" from claims, and
  MVL-188 can score health signals against stated and observed rates without Memory having judged them.
- Known absence is rare until the compiler states which sensor a stream recorded (a `Stream` sensor field or an
  alignment record); that is the trigger to revisit §4, and claims then move to a new lineage (ADR 0003 §3).
- When the catalog API publishes series rows (or row-level coverage), the `series_interval` stand-in is replaced and
  interior gaps become possible.
- Claims per stream grow with its clocks.
