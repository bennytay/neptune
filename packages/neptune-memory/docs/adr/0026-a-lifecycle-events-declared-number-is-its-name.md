# 0026 — A lifecycle event's declared number is its name

- Status: Accepted
- Date: 2026-10-07
- Issue: MVL-191

## Context

An event node is keyed by its record (`record:<rec id>`, ADR 0013): a content address. People name a work order
or an incident by its number (WO-26-0911, INC-C3-0011), which the record declares in `identifiers`, and no claim
carried it, so a reader (Context's entity index) could not find "INC-C3-0011" at all.

## Decision

1. `memory.events` version 3 gives an `incident_record`, `intervention` or `maintenance_event` event a `has_name`:
   the value of the one declared identifier its record states, verbatim (`INC-C3-0011`), `stated`, cited where the
   record states it. Several distinct values, or none, name nothing. A timeline entry, an action or a status is
   not named.
2. The name is text, never the node's key and never identity: two records stating one number stay two events
   (joining them is identity's, ADR 0019). The vocabulary is unchanged (`has_name` holds for every node type).

## Alternatives considered

- **`same_as` to a declared-id node.** Only identity grounds `same_as`, and a number is not a second node.
- **A new predicate.** `has_name` already means "a declared display name, verbatim".

## Consequences

- Version 3 is a new lineage: every `memory.events` claim id changes again (as from version 1 to 2).
- Context finds events by number (Context ADR 0015).
