# 0014 — Traceability and incident reconstruction are templates over pack compiler 2

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-161
- Amends: ADR 0013 §1 (subjects), §4 (template format), §5 (timeline conflicts, other clocks), §8
  (supersession bounds), §9 (the WinAnsi rule), §10 (outputs)
- Uses: Memory ADR 0002 §3, 0010, 0011, 0013 (PR #125), 0014 (PR #129); Deploy ADR 0012 §2

## Context

Demo v1's headline artifact is an incident reconstruction: one timeline of what happened, on one clock, every row
cited, with clock conflicts and gaps shown. Its companion is a configuration traceability report per machine:
as commissioned, every change, as maintained, as requalified, the runs under each configuration, the calibration
state and the authorisation in force. ADR 0013 meant both to be templates. Four things the templates need were
missing from compiler 1:

1. **An incident is an event node** (Memory ADR 0013 §2). A spec could only name a site, deployment or machine.
2. **Two records of one incident are two nodes.** A CMMS row and a syslog line about one stop are two `event`
   nodes; identity relates them with `same_as` (Memory ADR 0013, Revisit). Compiler 1 found a time conflict only
   within one node, so a CMMS row and a syslog line 32 s apart were two quiet entries.
3. **A report's timeline entries share only a record with the incident.** They are `record:<id>/timeline/<i>`
   nodes evidenced by the incident's record (Memory ADR 0013 §3). A path could only hop node to node.
4. **Real graphs hold clock maps.** Memory's `memory.time` (graph-schema 1.4.0, on main) emits `clock_map`
   literals, and calibration drift (1.7.0) emits `delta` literals. Compiler 1 refused the whole snapshot.

The MVL-159 review added two findings in the same code: a states section compared every pair of a node's spans
(4,000 spans took 2 s), and a timeline hid an event's other-clock claims whenever the event had a pack-clock
placement, uncounted and uncited.

## Decision

### 1. Subjects and hops

- A spec's subject may be an `event` (`SUBJECT_TYPES`: deployment, event, machine, site).
- A hop may be `{"predicate": P, "direction": "shared"}`: from a node to every other node with a current claim of
  P stating the same object (canonical bytes), citing both claims. `evidenced_by` + `shared` reaches the timeline
  entries of an incident's report, and other events resting on the same record.

### 2. Event identity and time conflicts

- A timeline section may name `same_event` predicates (template format, timeline sections only, a non-empty list
  without repeats). Current claims of them between two nodes in scope join those nodes into one event (connected
  components, either direction). An inferred link joins nothing unless the spec includes inference; a
  `*_candidate` link is not named and so never joins: it is shown in its own section, every reading.
- An event (a node, or nodes joined) placed at two or more different intervals on the pack clock is a
  **conflict**, as in compiler 1. Every placement on the pack clock is shown, inside the interval or not. Each
  conflicting entry lists every other placement with `start_difference_ticks` (other start − this start, in
  ticks of the pack clock). Ticks are never converted to seconds: the pack does not know a clock's resolution.
- Joined placements at the same interval corroborate: both are shown, `known`, with the nodes they are joined to.
- Entries name the nodes and claims that join them (`same_event` in the JSON). Those claims are cited.

### 3. Clocks: placed, restated, not placed

- An entry is on the pack clock only as Memory placed it: natively, or through a clock mapping Memory states
  (Memory ADR 0013 §2), whose records the entry names (`placement_records`, ADR 0013 §5).
- A claim on another clock that a pack-clock claim restates (same node, predicate and object) is counted per
  section as `excluded.other_clock_restated`, and shown under "Left out". A claim on another clock with no such
  twin is listed under `other_clocks`, which a timeline renders as **NOT PLACED**. Nothing on another clock
  disappears.
- **A civil zone is not a mapping.** A `civil_time_zone` record (Deploy ADR 0012 §2) says what a local clock's
  ticks mean. It never relates two clocks, so the pack never converts by it: two local clocks that declare one
  zone are two clocks (the corpus's cell PC ran 96.7 s ahead of the HMI on the same zone). Memory carries no zone
  into the graph yet. When it does, the zone is a statement about a clock node that a template selects like any
  other.
- The incident template lists the involved machines' `has_clock`, `maps_to` and `clock_map` claims, so the
  mappings behind a placement, or their absence, are in the pack with their parameters as stated.

### 4. Templates

- `configuration-traceability@1` (machine): the configuration chain (`has_configuration`, `configuration_candidate`
  as ambiguous, `configuration_unknown` as unknown); `succeeds`; runs recorded by the machine with their
  configuration; calibration in force on mounted sensors and drift (`has_calibration`, `calibrated_with`,
  `calibration_candidate`, `drift`); the records that produced a calibration (`calibrated_by`); authorisation
  envelopes at the machine's site; `not_covered_by_authorisation`. The pack never labels a stage. "As
  commissioned" and "as requalified" are the records a span cites, in the claims index (non-negotiable 2).
- `incident-timeline@1` (event): records of the incident (`same_as`, `same_as_candidate`); the reconstruction (a
  timeline with `same_event: [same_as]`, reaching the incident, its joined and candidate records, the statements
  sharing their records, and events involving its machines, at its site or in its zone); the involved machines'
  configuration, changes and runs; clocks and mappings; co-occurrence, never cause. Episode boundaries (MVL-133)
  and zone traversals (G3, MVL-136/137) are not in it. Each is a new template version when its predicates are
  published.

### 5. Exports

`render_claims(pack)` writes the pack's claim set as a graph-schema `ClaimsResult` in canonical JSON:
`as_of` (the snapshot head), `claims` (cited claims on the pack clock), `other_clocks` (the rest) and `findings`
(the resolver findings the pack lists), each exactly as the snapshot holds it. It is a result as of the head,
so it holds current versions only; a superseded version a finding cites stays in `pack.json`. The CLI writes it as `claims.json`
beside `pack.json` and `pack.pdf`, with the same never-overwrite rule.

### 6. Compiler 2

- **Overlap conflicts by sweep.** A states section finds overlapping spans of a `one` predicate with different
  objects per (node, predicate, clock) in start order. An interval stays active until a later start reaches its
  end, and each entry is marked at most once, so the work is O(n log n). It gives the same answer as compiler 1's
  pairwise rule (tested on random spans); 50,000 spans take about 1–2 s, including the case where every
  span states a different object and all overlap.
- **Literals.** `clock_map` and `delta` join the literal datatypes the snapshot reader accepts, each checked
  against graph-schema's `ClockMap` (1.4.0) and `Delta` (1.7.0) shapes or refused (`snapshot_malformed`). A
  clock map is stated about a clock node: every anchor's source instant is on that clock, its target instant and
  residual bound on the map's target. The PDF shows them as stated (a clock map's anchor with both clocks named,
  rate and bound; a delta's components and unit). Any other datatype is still refused.
- **PDF metadata.** `/Producer` names the compiler version.
- **Supersession** is bounded: `recorded_at <= superseded_at <= head` (one transaction may record and supersede a
  version: the resolver folds a transaction's assertions in order).
- **WinAnsi.** The `<` of a literal `<U+` in source text is escaped too (`<U+003C>U+`), so an escape in the PDF is
  never mistaken for source text.
- `COMPILER_VERSION` is `2`. What compiler-1 packs say changes (`other_clock_restated` in every section, other-clock
  claims of placed events listed when nothing restates them, and the timeline's conflict caption and NOT PLACED
  heading in the PDF), so every pack id changes. The golden digests move, and the PR says why.

### 7. The Demo v1 snapshot

`tests/fixtures/packs/acceptance_corpus.graph.json` is generated by `tests/deploy_pack_corpus.py`. It is a frozen
graph document about acceptance corpus 1.0.0 (PLANT-2: ARM-3A, LEG-01, INC-C3-0011; S-007: AMR-07, INC-0007).
Memory cannot build it from the corpus yet (events and calibration are unmerged, identity does not link
incident records, and the compiler decodes no bag payloads). It is therefore hand-written in the contract's shape,
as ADR 0004 §4 allows for committed packages. Its rules:
- every claim cites a corpus file by its locked content id with a real locator, or a named synthetic source where
  the corpus holds nothing (S-007's CMMS incident row, fleet manager syslog and time-sync statement, an
  intervention log, AMR-07's controller log and a lidar calibration);
- configuration ids follow the corpus's gold answers and stay Unknown where no record states one;
- one clock per export or document (the mapper keys one per column, so a real build splits further);
- the only mappings are those a source states.
It validates against graph-schema 1.4.0 plus the `event` node type and the `delta` literal.
`docs/samples/incident-timeline-INC-C3-0011.pdf` is its rendered reconstruction, checked by a test.

## Alternatives considered

- **Key an incident by its declared id across records.** That is identity resolution done in a report; Memory ADR
  0013 rejects it for the same reason.
- **Pick the CMMS time (or the earliest) as the event's time.** It chooses a winner. Both times and the
  difference are the finding.
- **Convert local times by their civil zone to compare them.** That is the silent UTC conversion the issue
  forbids, and it would line up two clocks that the corpus shows disagree by 96.7 s.
- **A separate incident compiler or Python report classes.** Templates as data were ADR 0013's point. Four small
  compiler capabilities keep every report a template.
- **Configuration spans as timeline entries.** A machine with several spans would read as one event at several
  times, a false conflict. Configuration has its own sections in the incident pack.
- **Seconds in the difference.** The pack knows ticks, not a clock's resolution. Resolution lives in the
  TimestampDomain record, and the appendix says how to resolve it.
- **Keep compiler 1 output byte-identical and gate everything on new template keys.** Both review findings
  change what existing packs say (restated counts, unhidden claims), so a version bump was due anyway.

## Consequences

- Demo v1 renders both reports from one snapshot. The CLI produces any of them from a spec.
- When #125, #129 and #124 merge, the snapshot test validates against the published schema unpatched, and Deploy's
  graph-schema pin moves (a contracts PR). When Memory publishes a snapshot export, a Memory-built snapshot of
  the corpus replaces the hand-written one, and the goldens move with an explanation.
- Gaps for Memory: identity linking of event records; a civil zone on a clock node; consolidator findings in
  the graph document (ADR 0013).
- Revisit when G3 publishes zone traversals (a new incident template version), or when a clock's resolution
  reaches the graph (differences could then be shown in seconds as a provenanced derivative).
