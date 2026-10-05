# 0009 — Run threads and cross-package continuation from RunAssembly records

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-131

## Context

Episodes (MVL-133), events (MVL-134) and Context's traversal (MVL-144: "everything about AMR-07 at site B in
March") need runs as graph nodes: who recorded them, where, for which task, which files they hold, and when, on a
clock a query can name. The compiler states each piece separately. A `Run` is one declaration (a recording's
header, a rosbag2 `metadata.yaml`, a manifest entry) with its declared `logical_id`, `machine` and inclusive
`first` / `last` instants, each on the clock its evidence states. A `RunAssembly` (root ADR 0050 §7, ADR 0066 §1)
says which files form one `Run`; its `rule` and producer transform are the grouping rule and its version. A
`ClockMapping` relates two clocks; a `TimestampDomain` may declare itself civil. What a manifest says a run
involved (site, task) is declared (root ADR 0047) but no record carries it yet.

Getting this wrong merges two robots' sessions, invents an order between logs on clocks that cannot be compared,
or turns a manifest typo into a site. A recording ingested in two uploads is the hard case: each upload's
assembly sees only its own part.

## Decision

### 1. What runs reads, and how it is parsed

Parsing (`consolidate/run_records.py`) is separate from the policy (`consolidate/runs.py`). Compiler kinds are read
with the compiler's strict readers: `run`, `run_assembly`, `source_revision`, `clock_mapping`, `timestamp_domain`
and `site`. A record they refuse, or one stating a blank or padded id (ADR 0006 §9), is `runs.malformed_record`; an
inferred record (a `derived/` grouping) is `runs.inferred_record` and never a ground. Records are admitted by id
across packages, and one id with two contents is `runs.record_conflict` with neither used, except assemblies and
revisions, which are keyed within their package: an assembly's id is its file list's evidence id, so two uploads of
one bag share it with different members.

One kind is a Ledger stand-in until the compiler records a manifest run's involvement: `run_declaration {id, run,
machine, site, task, evidence}`. `run` names the run by declared logical id, or by record as
`{"namespace": "record", "value": <run record id>}` (the golden graph's convention); the three roles are compiler
`Knowledge` of a `LogicalId`; it is `stated`.

### 2. Nodes and intervals

A run node is its `Run`'s declared logical id (`node_ref`, ADR 0003 §1.1), else `record:<run record id>`; an
`Ambiguous` logical id is keyed by record (`runs.ambiguous_run_id`). Runs with one declared id in any number of
packages are therefore one node, each record its own `evidenced_by` claim with its own interval: a shared declared
run id needs no `continues`, which links distinct nodes only.

Every claim about a run is emitted once per *placement* of each of its records, and cites the placement:

- **Primary.** `[first, last + 1 tick)` on `first`'s clock; `first` and `last` are inclusive (root ADR 0018 §1),
  and an inclusive last instant is written as the next tick (root ADR 0050 §3). A clock that declares a civil
  timescale, an absolute epoch and its resolution is that `CivilClock`, as identity places instants (ADR 0008 §2).
  No `last`, a `last` on another clock (`runs.end_on_other_clock`) or one at the clock's last tick: open end. A
  `last` before `first`: `runs.inverted_interval`, nothing placed.
- **Span.** A run that states no `first` (a manifest's folder, a rosbag2 metadata) holds over its assembly's
  recording parts' span when they are all on one clock; otherwise `runs.untimed_run` and no claim.
- **Civil projection.** Only where evidence states it: each `clock_mapping` from the placement's clock to a
  civil-declared clock with a `Known` anchor and rate, whose window (bounds not stated are open) covers the
  placement. Start rounds down, end up, and both widen by the stated residual bound, so the projection contains the
  true interval. Direct mappings only, in their stated direction; chains are MVL-130's. Never guessed.

### 3. Membership and continuation

- `has_member(run, record:<SourceRevision>)` per member of each assembly naming the run, with the assembly's
  `assertion_kind`, citing the member's evidence, the assembly and its producer transform (the rule's version);
  `evidenced_by(run, record:<assembly>)` beside it. This is the issue's `member_of(artifact, run)`: an artifact is an
  Episode-tier record, never a node (ADR 0002 §1), so the run is the subject.
- A recording member's *part* is a `Run` its bytes declare in the assembly's package (revision → content id →
  `Run.provenance.evidence.source`). Parts of one assembled run, from any number of packages, are compared on one
  clock (a civil placement if any, else the primary). Sorted by start, a part that starts at or after the previous
  one ends `continues` it (`observed`): nobody stated the order, the records' times show it. Overlapping parts are
  concurrent and parts declaring different machines are different robots: neither continues the other. Parts whose
  clocks differ are `continues_candidate` both ways: the evidence does not order them.

### 4. Declared roles

`recorded_by`, `at_site` and `executes_task` (the issue's `performed_by`, `at_site`, `under_task`) have grounds:
each `Run.machine`, and each `run_declaration` naming the run. A ground decides (`Known`) or offers candidates
(`Ambiguous`). The role is `Known` when the deciding grounds name one id and every ambiguous ground has it among its
candidates: one claim per deciding ground, each with its own `assertion_kind` and evidence. Otherwise every reading
of every ground is a `*_candidate` claim, and grounds that decide differently are `runs.declarations_disagree`. No
ground: no claim (`Unknown`). A run with no machine ground of its own takes its recording parts' machines; parts
that differ (a folder of two robots' logs) are candidates and `runs.parts_differ` (info): a multi-robot session is
real, so none is chosen. A site the Ledger's site register does not declare is still claimed as stated and is
`runs.site_unregistered`; with no site record at all the register is not covered and nothing is flagged.

`involvement(claims, run, predicate)` reads a role back as `Knowledge`: `Known`, `Ambiguous`, `Unknown`, or
`NotCovered` for a run no claim names. A lone `continues_candidate` is `Ambiguous` with the run itself, the
"continues nothing" reading, as a lone `same_as_candidate` is (ADR 0003 §1.3).

### 5. Consolidator

`memory.runs` version `1`, deterministic, no configuration (`runs.unknown_config`). It reads no previous claims.

### 6. Vocabulary and contract

`recorded_by` and `executes_task` are reused: they already mean "the machine whose log a run is" and "a task
attempted". New: `at_site` (`one`), `has_member`, `continues`, and `recorded_by_candidate`, `at_site_candidate`,
`executes_task_candidate`, `continues_candidate` (all `many`). A claim object cannot be `Ambiguous`, so the ambiguity
is the predicate, as for identity. `VOCABULARY_VERSION = 4`. The JSON Schema gains `CorePredicate`, the core names:
a vocabulary-only change exported an identical schema, which the registry cannot publish, and ADR 0006 §4 requires
predicates consumers traverse to be published. graph-schema **1.2.0** is a minor release; 1.0.0 and 1.1.0 still load
and pass the suite. The golden plan is unchanged; only its resolver generation moves.

## Alternatives considered

- **New `performed_by` and `under_task` predicates**: two names for one meaning beside the published
  `recorded_by` and `executes_task`; consumers would have to union them.
- **`member_of(artifact, run)` with an artifact node type**: makes Episode-tier records nodes, against ADR 0002 §1.
- **Order parts by file name or by assembly member order**: names and list order are not times; a rename would
  reorder a run.
- **`continues` between every pair of parts in time order**: links concurrent logs (two robots, or a robot's bag
  and its controller log) as if one followed the other.
- **Chain clock mappings to reach civil time**: MVL-130 owns transitive mappings and their citations; projecting
  through a chain here would duplicate it.
- **Pick the run record's machine over a manifest's** when they disagree: a priority the evidence does not state.
- **Drop a site the register does not declare**: loses a stated fact for a missing register row.
- **Read the compiler's inferred session proposals**: inferred input belongs to a `derived/` consolidator with a model.

## Consequences

- Runs are queryable by machine, site, task, member file and interval on the run's clock, and on civil time where
  a mapping exists; Context can follow `continues` across uploads.
- Claims per run grow with placements (primary plus each civil projection); a store pays for the second clock.
- The `run_declaration` stand-in is replaced when the compiler records manifest runs' involvement; claims then move
  to a new lineage (ADR 0003 §3).
- Revisit when MVL-130 publishes clock conversions (project through its chains), when the Ledger catalog API keys
  anchored runs (`record:` ids become thread keys), or when the compiler states run continuation itself.
