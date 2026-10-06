# 0015 — compare_runs, and runs and events found by name

- Status: Accepted
- Date: 2026-10-07
- Issue: MVL-191

## Context

Demo v1 asks "what changed on ARM-3A since the last good run" and "why did INC-C3-0011 happen". The facts are in
Memory (work orders and their actions, stated causes, named configurations and their declared values; Memory ADR
0025), but an agent following the skill (`neptune_query`, two hops from the machine, any predicate, 50 items /
20 000 tokens) gets none of them: the graph channel scores by hop only, so the machine's 70-odd direct claims fill
the budget, and Memory states a configuration's values once per run it was bound to. `neptune_entities` could not
find a run or an event at all: both are keyed by record, a content address it never offers.

## Decision

1. **`compare_runs`, a third explain clause** (`{"kind": "compare_runs", "before": <run>, "after": <run>}`, each a
   run subject by declared id). It carries, as claim items, what memory states differs between the two runs, and
   says no cause:
   - each run's `configuration_active_during` claims and its name;
   - for each configuration bound to a run, its `has_name` and `declared_value` claims over that binding (that
     run's own copy). A value is carried when its (key path, value, unit) is not stated for the other run: a
     changed value on both sides, a value only one run's configurations state on its side. Values stated alike
     are not carried;
   - every `maintenance` event that `involves` the later run's machine (its `recorded_by` machine and the ids
     memory states are `same_as` it), with its name, stated cause and each action's description. On the runs'
     clock only events between the two runs' starts count; on a clock no stated mapping ties to the runs' (a
     CMMS's), the event is carried and a `not_covered` gap says it is not ordered against them. No clock is
     converted.
   It produces no trail: the packet's items are the answer, rendered and cited as any other. Every carried
   claim scores 1.
2. **`neptune_compare_runs`**, an MCP tool for that clause: `before`, `after`, `include_inferred`, optional
   `as_of`, `max_items` (default 100) and `max_tokens` (default 60 000, Context's estimate of the items' JSON).
   The acceptance corpus's comparison is 67 items, about 49 KB (12 000 tokens) of rendered answer.
3. **Runs and events are found by name.** The entity index offers a run or event node under the `has_name`
   memory states for it (a run sheet's run name; a work order or incident number, Memory ADR 0026), and the
   listing shows each entity's names. A name two nodes share is a mention with both candidates.
4. **`query-packet` 1.5.0, a minor.** `Query/$defs/Explain` gains `CompareRuns`; nothing is narrowed.
5. **The skill prescribes it.** For "what changed since …" and "why did … happen": list runs by name
   (`neptune_entities` with `kind: run`), read the incident if one is named, then `neptune_compare_runs`.

## Alternatives considered

- **Better ranking for `neptune_query`.** Scoring by predicate would encode what matters in Context; the clause
  states a comparison memory can make without judging relevance.
- **Ordering maintenance against the runs by wall clock.** The CMMS and the bags are on unrelated clocks; Context
  never converts, so it carries the events and says they are unordered.
- **A comparison trail.** A new trail type would widen the packet schema and both renderers; the claims alone
  answer the question, cited.

## Consequences

- One call answers both demo questions' change facts within an agent's reading.
- A configuration bound to neither run, or values that changed in a configuration no run was bound to, are not
  in a comparison; `neptune_query` still reads them.
