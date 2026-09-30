# 0012 — Time types: `Timestamp`, `Duration`, `TimestampDomain`, clock roles

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-60 (sub-issue of MVL-4)

## Context

ADR 0005 fixed the shape of time: integer ticks in a source-scoped `TimestampDomain`, with no conversion at
parse time and no comparison across domains. It left these to MVL-4: the Python types, `Duration`, and the
list of clock-identity kinds. Every adapter that reads time depends on these types, so they are a long-lived
contract.

Two ADR 0005 points needed interpretation before they could be typed:

- **§2 says clock identity is `Knowledge`-wrapped.** An adapter, however, always knows *where* it read ticks
  from (MCAP `log_time`, the `t` column). What it may not know is *what that clock is*.
- **§6 says equality across domains is not defined.** Python equality also drives hashing, sets, dict keys
  and deduplication, so a raising `__eq__` breaks ordinary containers.

## Decision

1. **Types** live in `neptune.model.time`. All are frozen dataclasses.
   - `Timestamp(ticks, domain_id)` and `Duration(ticks, domain_id)`.
   - `ticks` is an `int` (not `bool`, not float) in signed 64-bit range. Out of range raises, including
     results of arithmetic. Nothing clamps or wraps.
   - `domain_id` is the domain's tier-2 `RecordId` (ADR 0003). A new adapter version therefore gives new
     domain ids, like every other lineage-scoped record.
2. **Clock identity has two parts.**
   - **Structural, always known:** `field` is the source's own name for the time field, verbatim, and `scope`
     is where in the source that field lives, outermost first (`("/imu",)`; `()` for the whole source).
   - **`role: Knowledge[ClockRole]`**, the part that may be unknown. It says what event the ticks mark:

     | `ClockRole` | Ticks mark | Examples |
     |---|---|---|
     | `receive` | a recorder received or logged the data | MCAP `log_time`, rosbag record time |
     | `publish` | the producer sent it | MCAP `publish_time` |
     | `sample` | the time the producer attributes to the data | ROS `header.stamp`, PX4 `timestamp`, camera exposure |
     | `document` | a time written in a document or register | inspection date, operator note |

   Which physical clock produced the ticks is not a role. It is described by `timescale` and `epoch`. PX4's
   `timestamp` is `sample` + `monotonic` + `boot`; GPS week-seconds are `sample` + `gps` + `gps`. Keeping the two
   axes separate avoids an enum that mixes them (for example "device clock" against "header stamp").
3. **The other domain properties** are each `Knowledge`-wrapped, as ADR 0005 §2 requires:
   - `resolution: Knowledge[Fraction]`: exact, positive seconds per tick. JSON is
     `{"denominator":…,"numerator":…}` in lowest terms. Floats and ints are rejected at construction.
   - `epoch: Knowledge[Epoch]`, one of `unix`, `gps`, `boot`, `first_sample`, `simulation_start`.
   - `timescale: Knowledge[Timescale]`, one of `utc`, `tai`, `gps`, `posix`, `monotonic`, `simulated`.
   - `declared_monotonic: Knowledge[bool]`. This is what the source declares; observed violations are
     findings.

   A value outside these lists is `Unknown` plus a finding until an ADR adds a member. Adding a member is
   additive and re-lineages nothing.
4. **`==` is record equality, not simultaneity.** `Timestamp(5, A) == Timestamp(5, B)` is `False`, which says
   only that the two records differ. There is no operator for temporal equality. Ordering (`<`, `<=`, `>`, `>=`,
   and therefore `sorted`) and subtraction across domains raise `DomainMismatchError`, a `TypeError`, as
   ADR 0005 §6 requires. Timestamps stay hashable. This is how ADR 0005 §6's "equality … not defined" is read.
5. **`Duration` carries its domain.** Its ticks mean nothing without the domain's resolution. The API allows
   `Timestamp ± Duration`, `Timestamp − Timestamp`, `Duration ± Duration`, negation and same-domain
   ordering. It has no scaling, no conversion to seconds and no cross-domain form.
6. **No conversion API.** Nothing here turns ticks into seconds, `datetime`, UTC, another timescale or another
   resolution. Those are derived transforms with provenance. They need an alignment (MVL-36) or an explicit
   derivation, which can use `resolution` exactly.
7. **`TimestampDomain` here has no provenance or `schema_version` field.** The record envelope arrives with
   MVL-3 and MVL-1 for all entities at once, as it does for `SourceArtifact`. States inside the domain already
   carry their own provenance (ADR 0011).

## Alternatives considered

- **A single `ClockKind` enum** (`mcap_log_time`, `ros_header_stamp`, `px4_boot`, `gps`, …). This is closed
  over formats, so every adapter would need an ADR, and it mixes the event axis with the clock axis.
  `field`/`scope` already name the format-specific source verbatim.
- **Wrapping `field`/`scope` in `Knowledge`.** An adapter cannot read ticks without knowing where it read them,
  so the wrapper would always be `Known` and would only add noise (ADR 0004 §3 scope rule).
- **Raising in `__eq__` across domains.** It follows the letter of ADR 0005 §6, but a `set` or `dict` holding
  timestamps from two clocks then raises on a hash collision. That is a failure mode worse than the one it
  prevents.
- **`Duration` as bare ticks or as seconds.** Bare ticks can be added to a timestamp in another resolution.
  Seconds require a conversion and a float or `Fraction` at parse time.
- **Float resolution.** 1e-9 has no exact binary form. Two adapters computing "the same" resolution could
  produce different bytes.

## Consequences

- Adapters can express everything ADR 0005 lists, including ambiguous or undeclared clocks, without inventing
  semantics.
- Sorting mixed-domain timestamps raises. Storage ordering by `(domain id, ticks, source order)` (ADR 0005 §7)
  must use an explicit key. It never uses temporal comparison.
- Converting a float or decimal time column into ticks (ADR 0005 §4) needs one shared, documented rounding
  helper. It lands with the first adapter that reads such a column, so adapters do not each invent one.
- Revisit if a real source marks an event that none of the four roles describes, or if hashing and set use of
  timestamps turns out not to be needed and a stricter `__eq__` becomes affordable.
