# 0017 — Estimated clock mappings in a tenant graph

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191

## Context

The compiler fits clock mappings from co-recorded anchors and writes them to `derived/clock_mapping`
with `assertion_kind: inferred` (root ADR 0060). Take the acceptance corpus: the cell PC runs about
96.7 s ahead of the controller and HMI. The only thing that places its bag times on the incident
report's clock is 36 such fits. ADR 0011 §4 built `memory.time_estimates` (`derived.clocks`) to relay them
as inferred `maps_to` and `clock_map` claims. ADR 0016 §3's registration set, which `memory consolidate`
and `rebuild` run, holds only the eight deterministic consolidators. `consolidate/` may not import
`derived/`, so no CLI-built graph could hold an estimate.

Memory's rules for inferred evidence still hold:
- it stays `inferred` end to end;
- a consumer drops it with one flag;
- nothing inferred lands in `consolidate/`'s output as stated or observed.

## Decision

1. `memory consolidate` and `memory rebuild` take `--with-estimates`. It adds
   `Registration(EstimatedClocksConsolidator(), {"model": CLOCKS_MODEL.to_json()})` at priority 0 to ADR
   0016 §3's set (`cli.registrations`). Off by default: the deterministic graph stays the default graph.
2. The run's `MemorySnapshot` records the consolidator and its build like any other. A graph built with
   it is extended only with it; dropping it is ADR 0016 §4's refusal and takes a rebuild.
3. Every claim it adds is `inferred` and names the compiler's clock pass as its model (ADR 0011 §4).
   Readers drop them with `include_inferred=False` (ADR 0006). An inferred claim always loses a contest
   to a stated or observed one by assertion rank (ADR 0016 §2.4).
4. Deterministic consolidators never ground anything on an estimate. The shared record gate
   (`run_records._strict`) treats a record as inferred when its top-level `assertion_kind` says so,
   which is the `derived/` line shape of root ADR 0060 §6, as well as when its `provenance` says so.
   `memory.runs` and `memory.events` report such a line as an INFO `inferred_record`, not as a malformed
   record. So no event time and no `co_occurs_within` is derived through an estimate. Relating events
   on an estimated mapping is a separate design (an inferred derived consolidator over the event
   index); it is not decided here.

## Alternatives considered

- **Add the estimates consolidator to `default_registrations`**: `consolidate/` would import `derived/`,
  which AGENTS.md rule 2 forbids, and every graph would carry inference whether its tenant wanted it or
  not.
- **A second graph per tenant for derived claims**: readers would have to merge two histories with two
  generations. The resolver already ranks inferred claims below evidence, and the reader already filters
  them, so one graph holds both without either leaking into the other.
- **Leave the 36 derived lines reported as malformed**: an error finding for a well-formed estimate is
  noise that hides real corruption, and it says nothing about why the line was not used.

## Consequences

- The acceptance-corpus snapshot is built with `--with-estimates`. It holds the 36 fits as inferred
  `maps_to` and `clock_map` claims, and `schema.clocks.convert` can align bag and report times, marked
  inferred.
- The gate change alters only the findings for `derived/` lines. Claims are unchanged for every input,
  so no consolidator version moves.
- Revisit when Memory reads the catalog API: the CLI flag then becomes a tenant setting.
