# 0016 — Procedure revisions in compare_runs, graph-schema 2.3.0 pin and query-packet 1.6.0

- Status: Accepted
- Date: 2026-10-14
- Issue: MVL-191

## Context

Memory ADR 0027 states SOP revisions as `procedure_revision` events on the machine they apply to: each label
and step a `declared_value`, each revision-history row its own event, each blank label or cell a `stated_blank`
(graph-schema 2.3.0). The demo's "why did INC-C3-0011 happen", "what changed since the last good run" and "what
is unknown about it" need three of those facts: revision C relaxed the hand-eye acceptance from 0.8 px to 2.0 px
and dropped the 10-pick verification on 2026-09-11, with no change record and no approver named. The skill's path
is `neptune_compare_runs`, which carried configurations and maintenance only; "what is unknown" did not compare
the runs at all. At the 2.2.0 pin Context reports `stated_blank` as beyond its pin.

## Decision

1. **Pin graph-schema 2.3.0.** `pins.py` holds `CATALOG_API_VERSION = "1.7.0"` and
   `GRAPH_SCHEMA_VERSION = "2.3.0"`; `contracts/lock.toml`, `docs/contracts.md` and `pinned.json` agree.
2. **`query-packet` 1.6.0, a minor.** `stated_blank` joins the predicate enums of `DiffTrail` changes and
   `Query/$defs/Graph`. Nothing else changes; every 1.x query and packet is valid at 1.6.0. `procedure_revision`
   is an `event_kind` text value; the packet names no event kinds.
3. **`compare_runs` carries what changed between a procedure's revisions** on the later run's machine (its
   `recorded_by` machine and the ids Memory states are `same_as` it). Revisions are grouped by the procedure
   their `has_name` states. Within a group:
   - a revision's `declared_value` (key path, value) or `stated_blank` (label) is carried when some other
     revision does not state it alike: a changed limit shows on both sides, a dropped step on the side that has
     it, a blank approver beside the named one;
   - a revision-history row (`<revision>/revisions/<i>`) is carried, with its description, values and blanks,
     when some other revision lists no row stating exactly the same: rows both revisions copy are not;
   - each revision with anything carried carries its `has_name`; a procedure with one revision carries nothing.
   Revisions are not filtered by time: the revision in force at the earlier run matters as much as the later
   one, and Memory dates each on its document's own calendar, unrelated to the runs' clocks. A `not_covered` gap
   says they are carried, not ordered against the runs. Every carried claim scores 1, as ADR 0015 §1.
4. **The skill compares the runs for "what is unknown about this incident?" too**, and tells the agent to read
   `stated_blank` as "the document names no ...", never "none exists".

## Alternatives considered

- **Carry every claim of every revision.** About 45 more items: the acceptance comparison would pass its
  100-item default budget. What both revisions state alike did not change, as for configuration values.
- **Keep only the revision effective between the two runs.** No stated mapping relates a document's calendar
  to a run's clock, and the earlier revision is half the change.
- **A new trail type for procedure diffs.** A packet schema change and two renderers; the claims are the answer.

## Consequences

- The acceptance comparison grows from 67 to 86 items (about 62 KB rendered), inside the default budget.
- Packets over a 2.3.0 graph carry the new claims; packets over older graphs keep their bytes.
- Deploy raises its graph-schema and query-packet locks to 2.3.0 and 1.6.0 in the same change.
