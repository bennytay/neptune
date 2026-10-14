# 0019 — Procedures for the parts the maintenance changed, in compare_runs, and the graph-schema 2.4.0 pin

- Status: Accepted
- Date: 2026-10-14
- Issue: MVL-191

## Context

"Did the maintenance on ARM-3A before INC-C3-0011 follow the vendor's procedure?" needs the vendor's advice
beside the work order. Memory ADR 0028 states the vendor bulletin SB-2026-117 as a `procedure_revision` event
that `involves` the part its `Applies to` line names (`vendor.part:FS-0340`), and, on a reviewer's assertion, that
part `same_as` the serial WO-26-0911 installed (`serial:FS-0340`). `neptune_compare_runs`, the skill's path for
"why" and "what changed", carries procedures only through the machine (ADR 0017): the bulletin names no machine,
so nothing reached it. Graph-schema 2.4.0 is a golden-only release (identity v5).

## Decision

1. **Pin graph-schema 2.4.0.** `pins.py` holds `CATALOG_API_VERSION = "1.7.0"` and
   `GRAPH_SCHEMA_VERSION = "2.4.0"`; `contracts/lock.toml`, `docs/contracts.md` and `pinned.json` agree. The
   schema and vocabulary are 2.3.0's; query-packet stays 1.6.0.
2. **`compare_runs` carries the procedures for the parts the carried maintenance involves.** For each
   maintenance event the comparison carries (ADR 0015), each `asset` it `involves`, closed over the `same_as`
   claims memory states between assets (withheld inferred claims excluded): every `procedure_revision` event
   that `involves` one of those parts carries its `has_name`, each `declared_value` and each `stated_blank`, with
   the claims that link it (the event's `involves`, each `same_as` on the way, the procedure's `involves`). A
   `not_covered` gap says they are dated by the documents themselves, listed as what they advise, not what was
   done, and not placed before, between or after the runs. Every carried claim scores 1. Nothing joins a part by equal text: without a stated
   `same_as`, the vendor's FS-0340 and the CMMS serial are two parts and nothing is carried.
3. **The skill** reads such a procedure as what should have been done, compares it step by step with what the
   work order records, and never states a step was done because the procedure requires it.

## Alternatives considered

- **Carry every procedure that involves any part of the machine's history.** No such relation is stated; the
  maintenance carried is the one the comparison already found relevant.
- **Carry only the steps.** The name, the applicability and the issue date are what make it the vendor's advice
  for this part; the links show why it is in the answer.
- **A new tool for procedure compliance.** A packet schema change for what the claims already answer.

## Consequences

- The acceptance comparison grows from 86 to 96 items (about 70 KB rendered), inside the 100-item default budget.
- A deployment whose maintenance names parts gets the procedures that apply to them, by stated identity only.
- Deploy raises its graph-schema lock to 2.4.0 in the same change.
