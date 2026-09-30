# 0005 — Timestamp domains

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-55

## Context

Robotics evidence is full of clocks that look alike and are not:

- MCAP `log_time` vs `publish_time`;
- ROS `header.stamp` vs bag receive time;
- PX4 boot-relative microseconds vs GPS time;
- camera hardware timestamps, simulated time (`/clock`), and naive wall-clock strings in operator notes and
  PDFs.

GPS time runs ahead of UTC by the accumulated leap seconds. POSIX time ignores leap seconds. Monotonic clocks
restart at boot.

The usual shortcut is to convert everything to float seconds since the Unix epoch in UTC. That shortcut destroys
information in three ways:

- floats lose sub-microsecond precision at present-day epochs;
- clocks that were never synchronised become falsely comparable;
- a guessed time zone becomes indistinguishable from a declared one.

The audit ranks clock semantics as risk #1 because a mistake here forces a rewrite of every adapter and all
alignment.

## Decision

1. **A timestamp is `(ticks: int, domain_id)`.**
   - Ticks are an integer count in the domain's resolution and must fit a signed 64-bit integer. A value that
     does not fit is a finding, never a clamp.
   - There are no bare floats and no bare integers without a domain anywhere in `model/` or in series columns.
     In Parquet, ticks are `int64` and the domain is identified per column.
2. **`TimestampDomain` is an entity** with provenance. Each of these properties is `Knowledge`-wrapped (ADR 0004)
   and filled only from evidence:
   - **clock identity**: what the clock is, for example "MCAP `log_time` of source S", "`header.stamp` on topic T
     of source S", "PX4 `timestamp` (µs since boot) of source S";
   - **resolution**: seconds per tick, as an exact rational such as 1/10⁹. It is never a float;
   - **epoch / reference**: Unix epoch, boot, GPS epoch, first sample, or unknown;
   - **timescale**: UTC, TAI, GPS, POSIX (leap-second-free UTC), monotonic, simulated, or unknown;
   - **monotonicity**: declared or not, as distinct from observed. Observed violations are findings.
3. **Domains are scoped to one source by default.** Two sources never share a domain by assumption. That holds
   even for two MCAP files from the same robot on the same day. Stating that two domains are the same clock, or
   how they relate, is a `ClockAlignment` record produced by the alignment pass (MVL-36). It carries the method,
   the evidence and the error bounds, and it never rewrites stored ticks.
4. **No conversion at parse time.**
   - Nothing is converted to UTC, to another timescale, or to another resolution while parsing.
   - An adapter records ticks exactly as the source encodes them.
   - If the source encodes time as a float (for example float seconds in a CSV column), the adapter converts it to
     integer ticks at a resolution it declares, using a documented rounding rule. That conversion is part of the
     transform, and the original lexical value stays addressable through the row's locator (ADR 0006).
5. **Textual timestamps.**
   - An ISO-8601 string with an explicit offset denotes an instant. The adapter stores it as ticks in a domain
     whose timescale and epoch the string itself declares. The offset is preserved as stated data.
   - A string with **no** zone is never assumed to be UTC or the host's local zone. Its domain's epoch is
     `Ambiguous` or `Unknown`, and a finding says so.
6. **Cross-domain comparison is a type error.** Ordering, subtraction or equality between timestamps of different
   domains is not defined in the API. Durations exist only within one domain. Comparing across domains requires
   an explicit alignment.
7. **Series ordering.** Time-series rows are sorted by `(domain id, ticks, source order)` (ADR 0002). Source order
   is the tie-breaker because duplicate timestamps are common and meaningful.

The Python types, the `Duration` design and the exact list of clock-identity kinds are MVL-4's. Alignment
methods and error models are MVL-36's.

## Alternatives considered

- **Float seconds since the Unix epoch.** Universal and convenient. A double has about 0.24 µs resolution at
  current epochs, so nanosecond hardware stamps are lost. It also erases which clock produced the value.
- **Normalise to UTC nanoseconds at parse time**, with the original kept on the side. Every consumer would read
  the normalised value and trust it, while the normalisation depends on assumptions (zone, leap seconds, clock
  sync) that are frequently wrong. That is a silent assumption with good intentions. A normalised view is a
  derived transform with provenance.
- **`datetime` objects.** They carry a zone but not a clock identity, cap resolution at microseconds, and cannot
  represent boot-relative or simulated time honestly.
- **One domain per robot or per run by default.** It reduces alignment work, but it asserts clock synchronisation
  that the evidence rarely proves. Domains are cheap to merge later through alignment and impossible to split
  after a false merge.

## Consequences

- No precision is lost at ingest time, and no clock relationship is invented.
- Multi-file runs start with many domains. Time-aligned queries across them wait for MVL-36 or an explicit
  manifest statement. This friction is deliberate.
- Every adapter must name its clocks. A format with ambiguous time semantics produces `Unknown` or `Ambiguous`
  domain properties plus findings, which surfaces real gaps in teams' data.
- APIs and consumers carry a domain id with every timestamp, which makes signatures slightly heavier.
- Revisit if a supported format encodes time that does not fit in signed 64-bit ticks at its native
  resolution, or if alignment proves unworkable without a shared default domain.
