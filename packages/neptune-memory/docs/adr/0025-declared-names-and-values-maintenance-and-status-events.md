# 0025 — Declared names and values, maintenance and status events (graph-schema 2.2.0)

- Status: Accepted
- Date: 2026-10-07
- Issue: MVL-191

## Context

The Demo v1 gate asks "why did INC-C3-0011 happen" and "what changed on ARM-3A since the last good run". The
facts reach the Ledger but stop at Memory:

- A work order (`maintenance_event`, WO-26-0911: four actions, a diagnosis, a part swap) gives no claim: the event
  index (ADR 0013) reads incidents, interventions and event tables only.
- The compiler's `status_report` records (package-schema 10, root ADR 0071; 23 hand-eye residual warnings naming
  CAL-ARM3A-0911) give no claim.
- An incident's stated `root_cause` gives no claim.
- A configuration node is a Ledger thread id (ADR 0024) and nothing names it; a run has no name claim.
- What a configuration declares (a calibration id, a 1.86 px reprojection error, the board, the z offsets) lives
  only in `configuration_value` and `calibration` records. No predicate can say a record declares a value at a
  key.

## Decision

1. **Maintenance events are events.** `memory.events` (version 2) reads `maintenance_event` as it reads an
   incident: node `record:<rec id>`, at its stated `performed` instant (none: `untimed_event`). Claims:
   `evidenced_by` the record, `event_kind` `maintenance` (a new registered kind; the record states no kind of its
   own, so no `declared_kind`), `stated_cause` its diagnosis verbatim, `involves` each machine it names and each
   part unit it removed or installed (by declared serial), `at_site`. Each action it lists is its own event,
   `record:<rec id>/actions/<i>` (as a timeline entry is), with `has_description` the action's text verbatim,
   cited at its own span, `evidenced_by` and `involves` the machines. `has_description` stays `one`: four actions
   are four events, never four contradicting descriptions. A related-record list (a downtime ticket) has no
   predicate yet and gives no claim.
2. **Status reports are events.** A `status_report` is an `observed` event at its first stated time, in its
   stream's clock order; the other clocks are reached through stated mappings only, as for every event.
   `declared_kind` is its level's one declared name (`WARN`), else its level integer; `event_kind` only through a
   vendor mapping keyed by its `convention` (`ros_diagnostic_status`), by name (`text`) or level (`integer`);
   `not_an_event` drops it. `has_description` is its message; each key/value it states is a `declared_value`
   (text, or an integer with an `unknown` unit). `safety_state` is not read yet.
3. **An incident's root cause is `stated_cause`.** `stated_cause` (event → text, `one`) is the cause a record
   states, verbatim: an incident's `root_cause`, a maintenance event's `diagnosis`. It is never inferred.
4. **`declared_value`, a predicate and a value type.** `declared_value` (configuration, event →
   `declared_value`, `many`). The literal (`DeclaredValue`) is a key `path` (keys verbatim, positions as
   integers, at most 64 steps), a `type` (`text`, `integer`, `real`, `boolean`, `reals`) and the `value` in that
   type, exactly as the record types it: nothing converted or normalised. A number's unit is the literal's, as
   declared (`Known`, `Unknown` where the source states none, `Ambiguous`); text and booleans are
   `not_applicable`. Each claim cites the value's own place in the source: its span, else its record's JSON
   pointer.
5. **`memory.declared` (version 1), a new deterministic consolidator** reading `configuration_snapshot`,
   `configuration_value`, `calibration`, `source_revision` and `run_declaration`, and the claims of
   `memory.configuration` and `memory.runs` (`after`):
   - a configuration node is the thread its snapshot or calibration opens (`threads_of`, ADR 0024); it gets one
     `declared_value` per scalar `configuration_value` of its snapshot (collections and aliases are not values;
     their leaves are) and per `calibration` parameter (its declared name as a one-step path, text or numbers in
     source order, its unit as declared);
   - a configuration's `has_name` is the path its bytes were found at, verbatim, `observed` (the compiler models
     a path as an observation of where bytes were seen); bytes found at two paths name nothing;
   - a run's `has_name` is the name its `run_declaration` states (`stated`, cited at the manifest entry); two
     declarations naming one run differently name it nothing (`name_conflict`);
   - every claim holds where `memory.configuration` places the configuration (`configuration_active_during`,
     `has_configuration`) or `memory.runs` places the run (its record's `evidenced_by`), one claim per placement,
     citing that placement's records. A node placed nowhere gets no claim and a `declared.unplaced` finding with a
     count: Memory gives no time to what states none;
   - a value that is not `Known` (a null, a blank, an ambiguous reading) or a calibration parameter that cites no
     place of its own is a counted finding (`value_absent`, `value_unstated`, `value_ambiguous`,
     `value_unlocated`), never a fact.
6. **graph-schema 2.2.0, a minor.** It adds the two predicates, the value type, `#/$defs/DeclaredValue` and the
   `maintenance` kind (`VOCABULARY_VERSION = 12`). Nothing is narrowed, so every 2.x golden validates. Context
   and Deploy raise their pins to 2.2.0 in the same change (as for 2.0.0): Deploy's reader refuses a literal
   type it does not know, and Context reports a predicate beyond its pin as a gap (Context ADR 0014).

## Alternatives considered

- **Values in the configuration lineage consolidator.** It reads the same snapshots, but a new version of it
  re-ids every lineage claim the demo answers already cite. A separate consolidator adds claims and changes none.
- **One `has_description` per action on the maintenance event.** `has_description` is `one`: four actions over
  one instant would be four contradictions the resolver cuts down to one.
- **A text literal `"key = value"`.** It loses the value's type and the path's structure, and a key holding `=`
  is ambiguous. A typed literal keeps both and stays comparable by consumers without parsing.
- **Open-ended validity from the first placement.** A configuration's values would be claimed after any
  evidence of it. Per-placement intervals claim only where a binding or span puts it.
- **The manifest's snapshot path as a `stated` name.** Only bound snapshots have one, and reading it means
  joining a binding to the manifest's own `configuration_value` by JSON pointer; the revision path is the same
  text for every snapshot, observed by the compiler.

## Consequences

- `memory.events` version 2 is a new lineage: every one of its 81 claim ids in the acceptance snapshot changes
  (the version is hashed into each id), and the build withdraws the version 1 claims. Run, configuration,
  identity and every other consolidator's claim ids are unchanged.
- The acceptance snapshot gains the work orders, their actions, the residual warnings, the incident's stated cause,
  named configurations and runs, and the calibration values (claim counts in the PR).
- A configuration no run is bound to and no machine span places (`cell_config.yaml` in the acceptance corpus)
  declares nothing in the graph until something places it; the finding says how many values wait.
- Context and Deploy read the new claims at their 2.2.0 pins; Context's packet schema widens (query-packet
  1.4.0). Rendering them in evidence-pack templates is a later Deploy template version.
- Revisit when the compiler states a configuration's own validity or name, or when `safety_state` and related
  records (a downtime ticket) need claims.
