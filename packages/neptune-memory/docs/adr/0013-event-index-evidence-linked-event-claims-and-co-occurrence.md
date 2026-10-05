# 0013 — Event index: evidence-linked event claims and co-occurrence that is never cause

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-134

## Context

"Why did the arm-cell incident happen?" needs events as graph nodes. Context's retrieval (MVL-144) and the incident
timeline evidence pack (MVL-159) both read them: what happened, of which kind, how severe as stated, on which
machine, where, when on a named clock, and which other events happened near it. The evidence comes as different
records. The compiler states lifecycle records: an `incident_record` (from a CMMS row, an incident form or a ticket,
with an optional timeline) and an `intervention` (root ADR 0051), and Deploy maps both from CMMS, ticket and
Formant exports. Tabular evidence comes as `structured_table` and `structured_record` rows (root ADR 0020 §5).
Deploy's ROS 2 diagnostics mapper writes one such table, `diagnostic events`, with a `@clock:stamp` cell that
names the clock. PLC, safety-controller and syslog exports are tables like it.

The compiler does not yet record some of this evidence. A bag's topics (ROS diagnostics or an e-stop topic inside an
MCAP) are streams with no decoded payloads (Deploy ADR 0010 §7, `bag_payload_not_decoded`). A flight log's logged
messages are series rows, not records. Times written as text in a CSV are only text. Memory reads no stand-in for
any of these.

The danger is the incident story. Two logs on two clocks (the cell PC runs 96.7 s ahead of the HMI) put side by
side without a stated mapping invent an order. "Within 5 s of" read as "caused by" is the trap the acceptance
corpus sets.

## Decision

### 1. What events reads, and how it is parsed

`consolidate/event_records.py` parses, with the compiler's strict readers: `incident_record`, `intervention`,
`structured_table`, `structured_record`, `timestamp_domain` (read by `identity_records.clock`, which now also
carries its stated resolution) and `clock_mapping` (`run_records.mapping`). A record they refuse is `events.malformed_record`. An inferred record is `events.inferred_record`
and is never a ground. One record id with two contents is `events.record_conflict`, and neither is used. The same
record in two packages is one record. `consolidate/events.py` decides.

### 2. Nodes, time and placement

- **One node per statement.** An `event` node (a new entity node type) is `record:<rec id>`. A timeline entry is
  `record:<rec id>/timeline/<i>`. Two records about one incident (a CMMS row and a PDF report) are two events.
  Relating them is identity's job: a declared incident id is not merged here.
- **Time as declared.** An instant `t` is `[t, t + 1 tick)` on its record's clock. Only records that have no end
  are instants: an incident's `occurred`, a timeline entry, and a row of a table declared without `end`. An
  intervention is `[start, end)`; `end == start` is an instant, and `NotApplicable` (an instantaneous
  intervention) is an instant.
- **Ends that are declared but not stated stay open.** An intervention's end, or a declared `end` column, that is
  `Unknown` or `NotCovered`, a blank cell, or an unreadable cell leaves the end **open**. The event may still have
  been going on, so it is never an instant. These give `events.end_unstated` and `events.end_unread`. An
  `Ambiguous` end is also left open (`events.end_ambiguous`), with its readings in the finding's details; no
  reading is chosen. `KnownAbsent` (stated as not ended) is open with no finding. If its end is on another clock,
  the end is open (`events.end_on_other_clock`). An end before the start places nothing
  (`events.inverted_interval`). An `Ambiguous` start is `events.ambiguous_time` and a missing one is
  `events.untimed_event`: no claim is made, and no reading is chosen.
- **Table times** are integer ticks, or integer seconds plus nanoseconds (a ROS `stamp`) scaled exactly by the
  clock's stated resolution. A stamp finer than the resolution, a clock with no stated resolution, or a time
  written as text is `events.untimed_event`. Text is never parsed into a time.
- **Placements.** Every claim about an event is emitted on its own clock. A domain that declares itself civil is
  that `CivilClock` (ADR 0002 §3). Every claim is emitted again on each clock that a stated `clock_mapping` from
  the event's clock reaches directly, citing the mapping and its target. The mapping math is exact (`Fraction`);
  the result is rounded out to whole ticks and widened by the residual bound; an `Ambiguous` bound widens by its
  largest reading. **An unstated bound is unknown, never zero** (non-negotiable 4). The event is still placed
  through the mapping, unwidened and citing it, but the placement is *unbounded*: nothing is decided by
  comparing it (§4), and the mapping gets one `events.bound_unstated` finding. A mapping whose validity window
  does not cover the event is not used for that event; an ambiguous window counts only where every reading
  agrees (`events.ambiguous_window`, as runs read it). Chains wait for MVL-130.

### 3. Claims and the event vocabulary

- `evidenced_by(event, record)`: the record (a timeline entry's is its incident record).
- `event_kind(event, text)` (`one`): always one of the registered kinds `EVENT_KINDS`, published as
  `#/$defs/EventKind`: `collision`, `emergency_stop`, `failsafe`, `fault`, `incident`, `intervention`,
  `mode_change`, `near_miss`, `protective_stop`, `reset`, `safety_field_violation`, `stale`, `warning`. A
  source's own kind reaches one only through a vendor mapping declared in config. For an `incident_record` the
  mapping is keyed by the stated severity (`"near miss" → near_miss`), and otherwise the kind is `incident`. For an
  `intervention` the mapping is keyed by the stated mode, and otherwise the kind is `intervention`. A table row is
  keyed by its kind cell. A mapping is keyed by the declared **type and value**: `{"text": {...}, "integer":
  {...}}`. The level `2` and the text `"2"` are different kinds, and neither is coerced into the other.
  Lifecycle mappings are text only. An unmapped kind gives no `event_kind` claim (`Unknown`,
  `events.kind_unmapped` with its type and a count). The reserved target `not_an_event` declares a row not to be an event (an OK status, an info line).
  Canonical JSON has no null.
- `declared_kind` and `stated_severity` (`one`, text or integer as declared) and `has_description` (`one`,
  text) are verbatim, and a severity is never ranked.
- `involves` (`many`, machine or asset), `at_site` (widened to events) and `in_zone` (`one`) come from declared
  ids. An `Ambiguous` id is one `*_candidate` claim per reading, and an unstated one is no claim. A field stating a
  blank or padded id (ADR 0006 §9) is not used at all and gives `events.id_unusable`: the record's other fields
  still count.
  `runs.involvement` reads these back as `Known`, `Ambiguous`, `Unknown` or `NotCovered`. Timeline entries carry
  only their time, text and record. They do not inherit their incident's machine or place.
- **Assertion kind** is the value's own, else its record's. An `event_kind` takes the kind of the declared
  value it was mapped from. Lifecycle records are `stated`.

### 4. Co-occurrence, never causation

- **Which events are compared.** Events from different sources are compared (the evidence `source` of their
  record). Entries of one log or one report are never paired.
- **Which clock.** A pair is compared on a clock both are placed on. Clocks where both placements are bounded come
  first, then the clock with the fewest mappings, with ties broken by the clock's id. A clock reached through a mapping gives an onset range `[lo, hi)`.
- **When a claim is made.** With the configured window `W` in ticks (`window_seconds` / the clock's resolution,
  rounded down), the events co-occur when every reading of both onsets fits in `[min lo, min lo + W)`. For exact
  instants that means `|a − b| < W`. Then `co_occurs_within` is claimed, one claim each way, `observed` (the
  records' times show it). **The claim's valid interval is that window**: its length is `W`, its clock is the
  clock compared on, and it cites both events, their placements and any mapping used.
- **When it is not.** If some readings fit and others do not, no claim is made. There is one
  `events.co_occurrence_undecided` finding per clock, with a count. A pair whose only shared clock is reached
  through a mapping with no stated bound is never decided: if the mapping's own reading puts the pair near each
  other, the clock gets one `events.co_occurrence_unbounded` finding, with a count. A clock with no stated resolution cannot
  measure the window (`events.window_unscaled`), and neither can a window shorter than one of its ticks
  (`events.window_below_resolution`). Two event clocks that no mapping relates, directly or through a shared
  target, are `events.clocks_unrelated`: Unknown, never compared. A mapping's window can cover some events on a
  clock and not others. The uncovered events are not skipped silently: the finding counts them, with
  `partial: true`. Events with the same clock and the same reach are checked together. The first 16 clock pairs
  are named, and one more finding says that others exist.
- **Limits.** The scan visits onsets in order and stops where a later onset can no longer share a window. It
  skips a run of the event's own source in one step and takes at most `max_partners` (default 64) later
  partners per event, so the work is bounded by events times `max_partners`, never by events squared. The
  claimed pairs are the nearest first, in seconds, so clocks with different resolutions rank alike. At most
  `max_partners` pairs are claimed per event; an event that had more is `events.co_occurrence_capped`.
- **No cause.** No predicate names a cause. A record's own stated root cause stays in the record.

### 5. Consolidator and config

`memory.events` version `1`, deterministic, reads no previous claims. Its config, resolved by `resolve_config`, is
`{co_occurrence: {window_seconds: "5", max_partners: 64}, vendors: {}, tables: []}`. A vendor is
`{"text": {declared: kind}, "integer": {"<integer>": kind}}`. `window_seconds` is an
integer or plain decimal text with at most nine decimals, at most one day (86,400 s). It is resolved to one
spelling (`5`, `"5"` and `"5.0"` hash alike), and `max_partners` is at most 4,096. A table is declared by its
declared name: `vendor`, `kind`, `at` (`{ticks}` or `{seconds, nanoseconds}`), `clock` (`{column}`, a companion
cell holding a `timestamp_domain` id, or `{record}`), and optionally `end`, `machine`, `site`, `zone`
(`{column, namespace}`), `severity` and `description`. Each refused part is one `events.invalid_config` finding,
and the other parts run. A table whose header lacks a declared column is `events.table_unusable` and is not read.

### 6. Vocabulary and contract

New: node type `event`. New predicates: `event_kind`, `declared_kind`, `stated_severity`, `has_description`,
`involves`, `involves_candidate`, `in_zone`, `in_zone_candidate` and `co_occurs_within`. `at_site` and
`at_site_candidate` widen to events (v2). `evidenced_by`, `has_name`, `same_as` and `same_as_candidate` widen to
every node type, `event` included. `EventKind` is published. `VOCABULARY_VERSION = 8`, after MVL-130 (6) and
MVL-133 (7). graph-schema **1.6.0** is a minor release; earlier goldens still load and pass the suite.

## Alternatives considered

- **Key an event by its declared incident id** (`cmms.incident:INC-0007`): this merges a CMMS row and a PDF report
  when their namespaces match and splits them when they do not. That is identity resolution done implicitly,
  against ADR 0003.
- **Event kinds as one predicate each** (`emergency_stop(event)`): every new kind would be a schema change, and
  "what kind was it" would become a scan over predicates.
- **Classify timeline text or messages into kinds** ("Operator presses the E-stop" → `emergency_stop`): that is
  inference, and it belongs under `derived/` with a model.
- **Parse text times in CSVs**: zones and formats would be silent assumptions. Deploy's lifecycle mapper reads text
  times by declared formats into records. Memory reads what it produces.
- **Co-occurrence as the hull of both events, with the window only in the config hash**: the window would not be
  readable from the claim. A config hash cannot be read back.
- **A `co_occurs_within_candidate` for undecided pairs**: its window could not be stated honestly when the onsets
  are uncertain. A finding says so without a claim.
- **Treat an unstated residual bound as zero**, as ADR 0009 projects runs: a co-occurrence decided that way would
  be a definite claim resting on an assumed exact mapping. Placing the event through the mapping (cited, with a
  finding) keeps it findable, and deciding nothing keeps it honest.
- **An instant for an end that is declared but blank**: it would turn a blank into the fact "it lasted one tick".
- **Compare across clocks by assuming they agree** (both "local time"): this is exactly the 96.7 s trap.
- **Read bag topics or flight-log messages directly**: Memory never re-parses raw evidence (AGENTS.md).

## Consequences

- Context can answer "what happened around INC-C3-0011" with cited events on the HMI clock: the bag's e-stop,
  mapped through the survey's clock mapping, sits next to the operator's e-stop entry, and neither is called the
  cause.
- Diagnostics tables can be large. Claims grow with rows × placements, and the co-occurrence sweep only compares
  onsets within the window.
- Revisit:
  - when the compiler decodes bag topics or flight-log messages into records (read them as new kinds, a new
    consolidator version);
  - when MVL-130 publishes clock conversion (project through its chains);
  - when identity links two reports of one incident;
  - when a consumer needs an `EventKind` this list lacks (a vocabulary version).
