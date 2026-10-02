# 0060 — Clock alignment: fitted mappings, found clocks and bounded instants

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-36

## Context

A package holds many clocks: an MCAP's `log_time` and a `publish_time` per channel, ROS header
stamps, a flight controller's boot clock, GPS time inside fix messages, civil dates in documents.
ADR 0005 keeps each apart and ADR 0050 froze the record that relates two of them, `ClockMapping`.
MVL-36 must produce those relations where the evidence supports them and say so plainly where it
does not. Forces:

- **Most relations are estimated.** Formats rarely state an offset; they write two readings side
  by side (a message's publish and log times, a GPS fix's UTC time and its boot-clock publication).
  Side by side is not one instant (ADR 0050 §5): the readings mark two events, and the latency
  between them is usually unstated.
- **The Ledger and Memory do exact arithmetic** on a mapping's rate and anchor (Ledger ADR 0003
  §3). Floats would make the same fit differ by platform.
- **No new record kind or schema version.** Package-schema 4 and 5 are held by other work; ADR 0050
  is the shape.
- **Packages are large.** A pass that loads every series row into memory, or decodes payloads,
  does not scale.

## Decision

1. **A pass, `neptune.clocks`, in the job's assemble phase** (after introspection, before staging),
   over the admitted sources' records and committed series runs. It reads only the time and value
   columns its rules name, writes no source tick, and runs only when the package holds two or more
   clocks (found ones included). Its outputs are derived tables and findings under its transform.
2. **Sync anchors come from rows that hold two readings.** Two rules, fixed in code:
   - `stream.co_recorded`: each extra clock of a stream against its first (`time/i` against
     `time/0`): an MCAP channel's `publish_time` against `log_time`, a header stamp against both.
   - **Found clocks**: a stream whose type, by its producer's published message definition, carries
     a receiver's time in its value fields gets an inferred `timestamp_domain` line (ADR 0050's
     `TimestampDomain` fields exactly) and anchors against its own clock: PX4 `time_utc_usec`
     (`vehicle_gps_position`, `sensor_gps`; microseconds, epoch `unix`, timescale `posix`; 0 means
     no time) and ArduPilot `GWk`,`GMS` (`GPS`, `GPS2`; milliseconds, epoch and timescale `gps`;
     week 0 means no time). A found clock is kept only where some row gives it a reading.
   A cell that is not a known integer in signed 64-bit range is never an anchor.
3. **The fit is exact and fixed.** Integer sums give the least-squares slope as a `Fraction`; the
   rate is that slope rounded with `limit_denominator(10^9)`; the anchor is the floored source mean
   and the rounded line value there. The residual is the largest distance, over every anchor, from
   that rounded line, rounded up to whole target ticks. Two passes over the rows, constant memory,
   no float, no randomness: the same rows in any order give the same bytes. One source instant
   gives an anchor with `rate` `Unknown`; a slope that is not positive gives no mapping and a
   `clock_not_increasing` finding.
4. **The bound is honest.** `residual_bound` = fit residual + the latency between an anchor's two
   readings. Nothing states that latency by default, so it is `Unknown`, and one
   `latency_unbounded` finding per rule and source lists each mapping's fit residual. A caller may
   state a latency bound per rule (`ClockConfig.slack`, seconds as a `Fraction`, part of the
   transform config); then the bound is `Known`, rounded up to target ticks.
5. **No extrapolation.** `validity` is `[first, last + 1)` of the anchors' source readings; outside
   it the mapping does not apply.
6. **Derived `clock_mapping` lines** carry ADR 0050 §5's fields exactly, with the derived envelope
   (`kind`, `schema_version`, `assertion_kind: inferred`) and `InferredProvenance` flattened as
   `evidence` and `transform`, like every derived kind (ADR 0036). Their states carry no
   provenance of their own; the reader refuses one. `method` is `co_sampled`: the anchors are
   read from one record's fields; the inference is the provenance, not a new method value.
   Stated mappings stay canonical and are never re-estimated.
7. **Aligning is a query, not a rewrite.** `ClockGraph.align(timestamp, clock)` walks stated then
   inferred mappings breadth-first, applying a mapping (or its inverse) only inside its validity,
   and returns `Aligned {instant, bound, path, inferred}`, where the bound grows as
   `rate · bound_so_far + residual_bound` and is `None` once any step's is `Unknown`; or
   `Unaligned {reason}`: `unsynchronised` (no chain of mappings joins the clocks),
   `outside_validity`, or `rate_unknown`. A validity bound that is not stated is not assumed open.
8. **Unsynchronised is explicit.** When the package's clocks form more than one group that no
   mapping joins, one `unsynchronised` finding lists the groups. A rule that matched a stream but
   found no reading pair gives `anchors_absent`; one source instant gives `single_instant`.

## Alternatives considered

- **Take the fit residual as the bound.** It hides the publish-to-receive or fix-to-publication
  latency, which is the larger error, and claims a precision the evidence does not give.
- **Float least squares, or a robust fit (RANSAC, Theil–Sen).** Floats differ by platform;
  RANSAC is random; a median-based fit needs every anchor in memory. The max residual over every
  anchor keeps outliers visible in the bound.
- **A new `fitted` mapping method or a latency field.** Both change ADR 0050's frozen shape and the
  package schema; the provenance already says `inferred`, and an `Unknown` bound plus a finding
  carries the latency gap.
- **Treat two clocks with the same epoch and timescale as one** (two hosts' POSIX clocks, two GPS
  receivers). That is the silent synchronisation ADR 0005 §3 forbids; it needs an anchor or a
  statement.
- **Decode payloads for anchors** (ROS `TimeReference`, header stamps in MCAP). Payload decoding is
  not built; the rules read columns adapters already write, and a decoder adds rules later.

## Consequences

- Within one recording, every channel's clocks join its log clock, so a camera frame and a joint
  state align, with an explicit bound when the user states the latency and an explicit `None`
  otherwise. Across recordings, clocks stay apart until a stated mapping or a shared anchor joins
  them, and the receipt says so.
- Packages with two or more clocks gain two derived tables, the `neptune.clocks` transform and
  findings, so their package ids change; one-clock packages are unchanged.
- The pass reads each matched stream's runs twice, column-projected.
- Revisit when payload decoding lands (more anchor rules), when a clock relation proves
  non-affine over a run (piecewise maps, ADR 0050's revisit trigger), or when the Ledger needs a
  bound where only a latency estimate exists.
