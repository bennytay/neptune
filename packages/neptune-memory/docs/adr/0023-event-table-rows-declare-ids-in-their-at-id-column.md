# 0023 — Event table rows declare their ids in their @id column

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191
- Amends: ADR 0013 §1 (what an event table row declares), ADR 0019 §1 (the ids identity resolves to events)

## Context

ADR 0019 lets an assertion name an event by an id the event's record declares, but read ids only from
`incident_record` and `intervention`. A row of a declared event table (ADR 0013) declared none, so the
acceptance corpus's INC-C3-0011 assertion (`cmms.downtime:DT-26-0914-01` and `syslog:4182`) dangled on the
syslog side. Deploy's typed event tables already carry each row's own id as a column `@id:<namespace>`
(Deploy ADR 0017 §1): the `syslog events` table has `@id:syslog`, keyed by `Seq`.

## Decision

1. A row of a `structured_table` whose `Known` header has a column `@id:<namespace>` (a record namespace)
   declares `<namespace>:<cell>`: certainly for a `Known` text cell, possibly for each text candidate of an
   `Ambiguous` one; a blank or padded value is refused as ADR 0019 refuses one. Any other cell declares nothing.
2. Identity reads these ids only for rows `memory.events` placed as events (keyed by the row's record),
   exactly as it reads an event record's `identifiers` (ADR 0019 §1): one row certainly declaring an id is
   one node; several, or a possible one, give candidates.
3. No event-table config key: the column name is the declaration, and identity, which takes no
   configuration, cannot see `memory.events`'s. The corpus Memory config is unchanged.
4. Identity stays at version `4` (ADR 0021): neither change is published yet, so both land in one lineage.

## Alternatives considered

- **An `id: {column, namespace}` key in the event-table config.** Only `memory.events` reads that config;
  identity would need a copy of it (a config of its own, which changes its lineage) or a new predicate to
  carry the id, a graph-schema change. Revisit for tables no mapper writes an `@id:` column for.

## Consequences

- The acceptance snapshot joins the syslog PSTOP row and the CMMS downtime intervention by `same_as`.
- Identity also reads `structured_table` headers and the `structured_record` rows that are events.
