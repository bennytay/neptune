# 0014 — graph-schema 2.2.0 pin and query-packet 1.4.0

- Status: Accepted
- Date: 2026-10-07
- Issue: MVL-191

## Context

Memory ADR 0025 publishes graph-schema 2.2.0, a minor: `declared_value` (a configuration's or a status's value
at a key path, as stated), `stated_cause` (an incident's root cause, a work order's diagnosis), the
`declared_value` literal and the `maintenance` event kind. At the 2.0.0 pin, Context reports those claims as
beyond its pin (ADR 0012 §9), so the Demo v1 answers to "why did INC-C3-0011 happen" and "what changed on ARM-3A
since the last good run" lose the 1.86 px reprojection error, the z offsets and the work order's diagnosis.
The packet schema embeds the Memory definitions and predicate enums at the pin, so the pin moves the packet.

## Decision

1. **Pin graph-schema 2.2.0.** `pins.py` holds `CATALOG_API_VERSION = "1.7.0"` and
   `GRAPH_SCHEMA_VERSION = "2.2.0"`; `contracts/lock.toml`, `docs/contracts.md` and `pinned.json` agree.
2. **`query-packet` 1.4.0, a minor.** The export differs from 1.3.0 only by widening:
   `ContextPacket/$defs/DeclaredValue` is added, `TypedLiteral` gains the `declared_value` variant, and the
   predicate enums of `DiffTrail` changes and `Query/$defs/Graph` gain `declared_value` and `stated_cause`.
   Every 1.x query and packet golden validates against 1.4.0. A claim item already carries any claim with its
   object and provenance, so no item type changes. `maintenance` is an `event_kind` text value; the packet
   names no event kinds.
3. **A declared value is rendered as declared.** The agent renderer writes
   `declared value at <path JSON>: <type> <value JSON> (<unit>, as declared)`; the human renderer writes the
   literal's JSON in a code span with its unit. Neither converts, compares or judges the value.
4. **A declared value is found by its words.** The lexical channel indexes a `declared_value` claim's key path
   and value as claim text, as it indexes a text literal, so `reprojection_error` or `CAL-ARM3A-0911` finds it.

## Alternatives considered

- **Stay at 2.0.0.** The demo's Q1/Q2 would cite no calibration value, and Deploy's reader (which moves to
  2.2.0 in the same PR) would read what Context drops.
- **Render a declared value as plain text (`z = 0.0702`).** It drops the declared type and the unit state, and
  a key holding `=` reads ambiguously; the JSON form keeps both.

## Consequences

- Packets over a 2.2.0 graph carry the new claims; packets over older graphs keep their bytes.
- Deploy and Learn may raise their `query-packet` locks to 1.4.0; Deploy does so in the same PR.
