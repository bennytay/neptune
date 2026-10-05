# 0012 — Episodes from stated task evidence: one attempt per tasked run, stated boundaries, no inferred outcome

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-133

## Context

Fleet aggregates (MVL-186), spatial deltas (MVL-136) and Context's traversal need episodes: a task attempt within a
run, its boundaries on a clock a query can name, the interventions during it and its outcome. The issue lists the
evidence that bounds one: task manifest entries, mission or job logs, operator interventions, declared outcomes,
e-stops and faults. What the Ledger holds today is narrower:

- **Tasks.** No compiler kind states a task, a mission or a job (root ADR 0047 §9: "there is no task record
  kind"; the `task` family is reserved for MVL-33). The only task statement is a run's: a manifest says run R
  executed task T, which ADR 0009 reads from the `run_declaration` stand-in as `executes_task(run, task)`.
- **Outcomes.** No kind declares a task's outcome. An `Intervention`'s `outcome` is the intervention's
  ("mission completed" after a remote assist), not the attempt's.
- **Stops.** `incident_record` (root ADR 0051) states incidents with the instant they `occurred`; nothing tells an
  emergency stop or a fault from a near miss.
- **Interventions.** `intervention` states `machines`, `related` ids and `start` / `end` on a clock. Intervention
  topics in a log are streams whose meaning is inferred (root ADR 0049), and Memory never reads series.

Getting this wrong invents task boundaries from timing, turns "mission completed" into a success rate, or
attributes one robot's intervention to another's attempt. The coordinator's rule for this issue is to read only
kinds the compiler or Ledger produce, and to report what is missing rather than add stand-ins.

## Decision

### 1. What episodes read

`memory.episodes` runs after `memory.runs` in a plan and reads its claims (ADR 0003 §4): for each run node, the
`evidenced_by(run, <Run record>)` claims that place it on each clock (its own interval, a parts' span, civil
projections), its `recorded_by` / `recorded_by_candidate` and its `executes_task` / `executes_task_candidate`.
Claims of any other consolidator, and inferred claims, are never grounds. From the Ledger it reads `run` ids (to
tell a run's own placement from its assemblies'), `timestamp_domain` (civil clocks, as ADR 0009 §2 places
instants), and `intervention` and `incident_record` through `consolidate/event_records.py`, small strict readers
meant to be shared with the event index (MVL-134). A refused event record is `episodes.malformed_record`; one id
with two contents is `episodes.record_conflict`, neither used. A Ledger holding runs but no `memory.runs`
placement is `episodes.no_run_claims` (info) and builds nothing.

### 2. What an episode is

An episode is a stated attempt at a task within a run. While the run's declared task is the only statement of an
attempt, **a run node with task evidence (`Known` or candidates) holds exactly one episode**, and a run with none
holds **no episode** (the issue's third archetype): `episodes_of(claims, run)` reads `Unknown` for it and
`NotCovered` for a run no claim names. An episode with nothing stated about it would be a second node for the run.

### 3. Boundaries and identity

On each clock the run is placed on, the episode's interval is the span of those placements, and every claim about
the episode holds over it. Its boundaries are claims with an `instant` object on that clock, each citing the
records that state it: `starts_at` is the earliest stated start; `ends_at` the latest stated end (half-open, as
`valid_to`), only when every placement on that clock states its end, since one that does not may run on
(`Unknown`). Placements of one run node that do not coincide are parts of it (a bag split in two, ADR 0009's one
declared id across packages) or two statements of it, never two attempts, so they widen the span rather than
compete.

The end is `Ambiguous`, every reading an `ends_at_candidate` on every clock, when a stated stop lies strictly
inside the episode: an `incident_record` that names the run in `related` (`stated`) or names its machine
(`observed`, by its time), and `occurred` after the start and before the end. The attempt may have ended there;
whether it resumed is not stated, so the stated end stays a reading. A lone candidate (a stop in a run whose end
is not stated) reads back as `Unknown`: it only might be the end, as ADR 0009 §4 reads a lone candidate. A stop
on a clock the episode is not placed on, from an incident that names the run, is `episodes.event_unplaced`.

The episode node is `episode:sha256:<hex>` over `{run, boundaries, records}`: the run node, its window on every
clock and the records of the placements stating them. Stops and interventions are claims about an episode, never
part of its id, so a late incident or ticket never re-keys it; new placement evidence (another `Run` record, a
clock mapping) does.

### 4. Interventions

`intervened(episode, <Intervention record>)` when the intervention names the run in `related` (`stated`), or
names the run's one stated machine and its `[start, end]` (or its one stated instant) overlaps the episode on one
clock (`observed`). It is `intervened_candidate` when the machine is ambiguous on either side, or when an
intervention names the run but its stated times fall outside the episode (`episodes.intervention_outside`). An
intervention naming the run with no time on the episode's clocks is held as stated and reported
(`episodes.event_unplaced`, info). An intervention never cuts an episode: it states that it happened, not that
the attempt ended (the manipulator archetype). Intervention topics are not read.

### 5. Claims and vocabulary

- `episode_of(episode, run)` is the issue's `part_of`; `executes_task` / `executes_task_candidate` (already
  `episode`-domained) are `performs`, copied from the run's grounds with their kinds and evidence.
- New (vocabulary **7**, graph-schema **1.5.0**, a minor release after MVL-130's 1.4.0 / 6): `starts_at`,
  `ends_at` (`one`, `instant`), `ends_at_candidate`, `intervened`, `intervened_candidate` (`many`; an
  `intervened` object is a `record`, as ADR 0002 §1 keeps Episode-tier records out of the nodes), and `outcome`
  (`one`, `text`, verbatim, never inferred). The issue's `episode_interval` is the start and end pair.
- `outcome` is registered so consumers can traverse it, and **v1 never emits it**: no record declares one.
  `outcome_of` reads `Known` (one declared text), `Ambiguous`, `Unknown` (nothing declares one) or `NotCovered`.
  A success is never inferred from an intervention's text, an incident or a run that ended.

### 6. Consolidator

`memory.episodes` version `1`, deterministic, no configuration (`episodes.unknown_config`). The reader's
`episodes()` query stays `NotCovered` (G3); `episodes_of`, `boundary_of` and `outcome_of` read the claims.

## Alternatives considered

- **Memory-local stand-ins for task entries and outcomes**, as ADR 0009 did for `run_declaration`: would let
  missions and declared outcomes be tested now, but the coordinator ruled out new kinds; the gap is reported for
  the compiler (MVL-33's task family) instead.
- **Records of one run that state different starts or ends as competing readings**: a split recording's parts
  would read as a disagreement about where one attempt starts.
- **One `Unknown`-bounded episode per run with no task evidence**: a node per run that states nothing the run
  node does not; aggregates would count attempts nobody declared.
- **Cut the episode at each incident**: assumes every incident stops the attempt and that nothing resumed; a near
  miss would split a mission. An end candidate states only what the record says.
- **Cut at each intervention**: an intervention mid-task is part of the attempt (the manipulator archetype).
- **Read an intervention's `outcome` as the episode's**: it describes the intervention.
- **Recompute run placements from records** (importing `consolidate.runs` internals): duplicates its policy; its
  claims are the published result.
- **An episode id from the run alone**: stable, but the issue asks for ids from boundaries and evidence, which is
  what lets a future task kind segment one run into several attempts without colliding.

## Consequences

- Every tasked run is queryable as an attempt with its task, boundaries per clock and interventions; consumers
  get the predicates now and keep them when real task evidence arrives.
- Gaps, for the compiler: a task / mission / job record kind with its boundaries and declared outcome (then
  several episodes per run, and `outcome` emitted), an e-stop / fault event kind distinct from incidents, and
  declared intervention topics. Each is a new consolidator version and lineage (ADR 0003 §3).
- Re-keying on new placement evidence leaves the old episode's claims current until withdrawal (ADR 0007 §5,
  MVL-132) lands.
- Revisit when MVL-33 lands a task kind, when MVL-134 publishes an event index (stops could come from it), or when
  MVL-130's clock chains let events on other clocks be placed.
