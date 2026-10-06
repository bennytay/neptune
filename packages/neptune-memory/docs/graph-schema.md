# Graph schema v1

This page states what Context, Deploy and Learn may rely on when they read Memory. The contract is
`contracts/graph-schema/v1.8.0/` (`GRAPH_SCHEMA_VERSION = 1`); [ADR 0006](adr/0006-graph-schema-v1-contract-surface-and-memory-reader.md)
records the decisions behind it. 1.1.0 (minor) adds the `stream` and `document` node types and `has_name`
([ADR 0008](adr/0008-identity-consolidator-on-compiler-identity-links-and-assertions.md) §6). 1.2.0 (minor) adds the
configuration lineage predicates ([ADR 0010](adr/0010-configuration-lineage-consolidator.md) §6); 1.3.0 (minor) adds the
run thread predicates ([ADR 0009](adr/0009-run-threads-and-cross-package-continuation.md) §6); 1.4.0 (minor) adds the `clock` node
type, the `clock_map` value type and `has_clock`, `maps_to` and `clock_map`
([ADR 0011](adr/0011-time-domain-registry-clocks-mappings-and-chains-never-estimated.md)); 1.5.0 (minor) adds the
episode predicates ([ADR 0012](adr/0012-episodes-from-stated-task-evidence.md) §5); 1.6.0 (minor) adds the
`event` node type, the event predicates and `EventKind` ([ADR 0013](adr/0013-event-index-evidence-linked-event-claims-and-co-occurrence.md) §6); 1.7.0 (minor) adds the
calibration history predicates and the `delta` value type ([ADR 0014](adr/0014-calibration-history-and-drift-consolidator.md)
§4, §6); 1.8.0 (minor) adds the coverage and health predicates
([ADR 0015](adr/0015-coverage-and-health-consolidator.md) §6). Earlier goldens still
validate and their graphs still pass the suite. The code is `neptune_memory.schema`. `tests/test_pins_memory.py` checks that this
page names every node type, predicate and finding code.

## Nodes

A node is `NodeRef(node_type, node_id)` and nothing else; every attribute and every edge is a claim (ADR 0002 §1).
`node_id` is opaque: a Ledger thread's declared logical id `<namespace>:<value>` (ADR 0003 §1), or for a `clock`
the record id of the `TimestampDomain` that declares it (ADR 0011 §1).

| Tier | Node type | What it is |
|---|---|---|
| entity | `machine` | any robot: arm, AMR, legged, humanoid, aerial, marine, vehicle |
| entity | `sensor` | a sensor |
| entity | `site` | a place where robots operate |
| entity | `zone` | a part of a site |
| entity | `asset` | a non-robot physical thing: fixture, charger, pallet, hull |
| entity | `task` | a task a run or episode attempts |
| entity | `person` | declared only; never named by an inferred claim (ADR 0005 §5) |
| entity | `software_version` | installed software |
| entity | `model_version` | a learned model |
| entity | `configuration` | a calibration, parameter set or description revision |
| entity | `policy` | an operating rule or control policy |
| entity | `run` | one recorded run |
| entity | `stream` | one recorded stream of a run: a topic, a channel, a log message type |
| entity | `document` | a declared document: a manual, an SOP, a datasheet, a register |
| entity | `episode` | a bounded segment of a run (an entity, not the Episode tier) |
| entity | `clock` | one declared clock; `node_id` is its compiler `TimestampDomain` record id |
| entity | `event` | something one record states happened: an e-stop, a fault, an intervention, an incident |
| context | `deployment` | a deployment, with a summary |
| context | `fleet` | a fleet, with a summary |
| context | `programme` | a programme, with a summary |

The Episode tier is the Ledger's records and evidence refs. They are not nodes: a claim points into the tier with
a `LedgerRecordRef` object and `EvidenceRef`s in its provenance.

## Predicates (`CORE_PREDICATES`, `VOCABULARY_VERSION = 10`)

A `one` predicate holds at most one object per subject at any valid instant on one clock, so a different object
over an overlapping interval supersedes. A `many` predicate never contradicts. The vocabulary only widens within a
major version (ADR 0002 §5).

| Predicate | Subject | Object | Cardinality | Meaning |
|---|---|---|---|---|
| `at_site` | event, run | site | one | the site a run or event took place at, as declared |
| `at_site_candidate` | event, run | site | many | ambiguous: the evidence names several sites |
| `authorised_configuration` | site | configuration | many | an authorisation envelope approves this configuration at the site over the interval |
| `calibrated_by` | configuration | record | many | the maintenance or requalification record that states the calibration resulted from it |
| `calibrated_with` | sensor | configuration | many | a calibration of the sensor, from its valid_from to its stated end or the next one |
| `calibration_candidate` | sensor | configuration | many | ambiguous: the calibration could be the sensor's over the interval; one claim per reading |
| `clock_map` | clock | clock_map | many | a `maps_to`'s parameters as the evidence states them, or the chain it composes |
| `co_occurs_within` | event | event | many | both events began inside the claim's valid interval, which is the configured window on that clock; different sources; never a cause |
| `configuration_active_during` | run | configuration | many | a configuration the run ran with, over the bound part of the run (a snapshot binding) |
| `configuration_candidate` | machine, run | configuration | many | ambiguous: the configuration in force could be this one; one claim per reading |
| `configuration_unknown` | machine, run | record | many | no configuration is stated over the interval; the record leaves it open, never filled |
| `continues` | run | run | many | a later part of one recording: the next part of a run its assembly states |
| `continues_candidate` | run | run | many | ambiguous: may be a later part; the evidence does not order them |
| `declared_kind` | event | text, integer | one | the event's kind exactly as its source declares it: a level, a code, a mode |
| `deployed_at` | deployment | site | one | where a deployment takes place |
| `drift` | sensor | delta | many | observed: two consecutive calibrations' declared values differ by the delta; no judgement |
| `ends_at_candidate` | episode | instant | many | ambiguous: may end here (a stated end, or a stop event inside it) |
| `ends_at` | episode | instant | many | where an episode ends (half-open, as `valid_to`), as its records state it: one claim per clock |
| `episode_of` | episode | run | one | the run an episode segments |
| `event_kind` | event | text | one | a registered event kind (`EventKind`), through a vendor mapping the config declares |
| `evidenced_by` | any node | record | many | a Ledger record about the node (Episode tier, by id) |
| `executes_task` | episode, run | task | many | a task attempted |
| `executes_task_candidate` | episode, run | task | many | ambiguous: the evidence names several tasks |
| `gap` | stream | run | many | the stream's declared first or last instant predicts samples here and its series holds none |
| `governed_by` | deployment, fleet, machine, site | policy | many | an operating rule or control policy that applies |
| `has_calibration` | sensor | configuration | one | the calibration in force |
| `has_clock` | machine | clock | many | a clock the machine's records carry, over the interval they observe it |
| `has_configuration` | deployment, machine, sensor | configuration | many | a parameter set, description file or other configuration in force |
| `has_description` | event | text | one | what the record says happened, verbatim: a message, a description, a reason |
| `has_member` | run | record | many | a source file the compiler's run assembly places in the run (its `SourceRevision`) |
| `has_name` | any node | text | one | a declared display name, verbatim; never an identifier |
| `has_summary` | deployment, fleet, programme | text | one | a context node's summary |
| `in_zone` | event | zone | one | the zone an event took place in, as declared |
| `in_zone_candidate` | event | zone | many | ambiguous: the record names several zones |
| `integrity_finding` | run, stream | text | many | a compiler finding about the evidence (truncation, corruption, a dropout); the object is its severity, verbatim |
| `intervened` | episode | record | many | a human intervention during the episode (an `Intervention` record) |
| `intervened_candidate` | episode | record | many | ambiguous: the intervention may have been during the episode |
| `involves` | event | asset, machine | many | a machine or asset the record names as involved, or the machine whose log it is |
| `involves_candidate` | event | asset, machine | many | ambiguous: the record names several possible machines or assets |
| `located_at` | asset, machine | site, zone | one | where it is |
| `maintenance_state` | asset, machine, sensor | text | one | serviceability as a record states it, verbatim |
| `maps_to` | clock | clock | many | a declared or estimated mapping, or a chain of them, takes its ticks to another clock's |
| `member_of_fleet` | machine | fleet | one | the fleet a machine belongs to |
| `mounted_on` | sensor | asset, machine | one | what a sensor is attached to |
| `not_covered_by_authorisation` | run | configuration | many | observed: no authorisation envelope in the Ledger names the configuration then |
| `operated_by` | run | person | many | a declared operator or supervisor |
| `outcome` | episode | text | one | the outcome a record declares for the episode, verbatim; never inferred |
| `part_of_programme` | deployment, fleet | programme | one | the owning programme |
| `rate_declared` | stream | quantity | many | the mean sample rate the source's index declares: (count - 1) over its first-to-last span, in Hz, on a clock with a stated resolution |
| `rate_observed` | stream | quantity | many | the mean sample rate the series holds: (known rows - 1) over its first-to-last known span, in Hz |
| `rated_payload` | machine | quantity | one | rated payload, unit as declared |
| `recorded` | stream | run | many | the stream's series holds samples over this interval: first to last known sample on one clock (the Ledger's series coverage) |
| `recorded_by` | run | machine | one | the machine whose log a run is |
| `recorded_by_candidate` | run | machine | many | ambiguous: the evidence names several machines for the run |
| `runs_model` | machine | model_version | many | a learned model it runs |
| `runs_software` | machine, sensor | software_version | many | installed software |
| `same_as` | any node | same type | many | the same real-world thing: declared identifier, configuration lineage or operator |
| `same_as_candidate` | any node | same type | many | ambiguous: the evidence could mean either; one claim each way |
| `sensor_not_recorded` | run | sensor | many | known absent: a sensor of a configuration bound to the run recorded nothing in it, and the recording covers the run |
| `sensor_presence_unknown` | run | sensor | many | unknown: the evidence does not decide whether a configured sensor recorded in the run |
| `sensor_recorded` | run | sensor | many | a sensor of a configuration bound to the run recorded in it: a run's file declares its identifier |
| `starts_at` | episode | instant | many | where an episode starts, as its records state it: one claim per clock |
| `stated_severity` | event | text, integer | one | the severity a record states, verbatim; never ranked or compared |
| `succeeds` | configuration | configuration | many | took over from the object on a machine's chain; valid while the subject is in force |
| `zone_of` | zone | site | one | the site a zone belongs to |

`EventKind` (`#/$defs/EventKind`) lists the registered event kinds, the only objects of `event_kind`:
`collision`, `emergency_stop`, `failsafe`, `fault`, `incident`, `intervention`, `mode_change`, `near_miss`,
`protective_stop`, `reset`, `safety_field_violation`, `stale`, `warning`. A kind is added with a vocabulary
version and never renamed or removed within a major.

Object value types are `text`, `integer`, `real`, `boolean`, `quantity` (a unit exactly as declared: `Known`,
`Unknown` or `Ambiguous`), `instant` (a `Timestamp` on its own clock), `record`, `delta` and `clock_map` (`#/$defs/ClockMap`:
a mapping's `anchor`, `rate` and `residual_bound` exactly as stated, each a `Knowledge` state inheriting the
claim's provenance, with `method` `stated` or `co_sampled`; or a `composed` chain naming its mapping records in
`chain` and the clocks between in `via`, with no parameters of its own). A `delta`
(`#/$defs/Delta`) is `later - earlier`, component by component, between two calibration records (`earlier`,
`later`): a parameter by its declared `name`, or the `translation` or `rotation` of the transforms both bind to one
edge (`parent`, `child`), in the `representation` both declare, with the transforms' own frames and direction
(`transform`). A rotation states its `adjustment`: a quaternion negated when the two point opposite ways
(`later_negated`), Euler angles wrapped into a half turn (`wrapped`), else `none`. Its unit is the one both declare
(`Known`), or `not_applicable` for a form without one (a quaternion, a rotation matrix); it is never converted.

## The claim and the finding

- A **claim** (`#/$defs/Claim`) has these parts: `subject`, `predicate`, `object`, `valid` (`[start, end)` on one
  clock, `end` may be `open`), `recorded_at` and `superseded_at` (Ledger transactions), `assertion_kind`
  (`observed`, `stated` or `inferred`), `confidence` and `provenance`. Provenance holds the evidence, the Ledger
  records, the consolidator id, version and config hash, and the model for inferred claims. `supersedes` lists the
  claims this version replaced. `id` hashes everything except `recorded_at`, `superseded_at` and `supersedes`.
- A **finding** (`#/$defs/ResolutionFinding`) has a `code`, the `claim` and `others` it names, its `provenance`
  (resolver id, version and config hash), `recorded_at` and `superseded_at`. `id` hashes code, claims and
  provenance. The codes are:
  - `clock_mismatch`: two versions of a `one` fact with different objects on different clocks. They are never
    compared, and the finding is active while both are current.
  - `overridden_on_arrival`: winners covered the arriving claim entirely, so no part of it was ever current.
- A **graph document** (`#/$defs/Graph`) is one resolved history: every claim version and every finding, the
  `resolver_config` whose hash is its `generation`, and its `head`: the latest Ledger transaction it covers. The
  head may be later than every `recorded_at`, because a transaction can produce no claim.

## Reading: `MemoryReader`

| Method | Returns |
|---|---|
| `graph_schema_version`, `generation`, `head` | the major, the resolver configuration hash, the latest transaction |
| `node(node, as_of, *, include_inferred=True)` | `Known(NodeView)`: claims about the node and claims pointing at it, plus their findings; otherwise `NotCovered` |
| `claims(subject, predicate, as_of, during=None, *, include_inferred=True)` | `ClaimsResult`: `claims`, `other_clocks` and `findings` |
| `neighbours(node, hops, as_of, *, include_inferred=True)` | `NeighboursResult`: each node within `hops` edges in either direction, at its shortest depth, with a `via` path of claims; findings naming any edge between two result nodes |
| `episodes(filter)` | **provisional (G3)**: `NotCovered` in v1 |
| `spatial(site, frame, as_of)` | **provisional (G3)**: `NotCovered` in v1 |

Identity is followed, never merged, with `schema.traverse.same_as_closure(reader, node, as_of, *, depth=8,
include_candidates=False, include_inferred=True)`: every node `same_as` reaches in either direction within `depth`
hops, each once at its shortest depth with the claims of one shortest path. It follows `same_as_candidate` only
when asked, works over any `MemoryReader`, and compares no clocks.

Clocks are related, never coerced, with `schema.clocks.convert(reader, ticks, from_clock, to_clock, as_of, *,
include_inferred=True, max_hops=8)`: exact ticks of `to_clock` (a `Fraction`) with the error bound the mappings
state, through `clock_map` claims that hold at that instant, forward or inverted, along every route of every
length. Declared mappings are tried first, estimated ones only if the declared ones decide nothing. The result is
`Known` (every route agrees), `Ambiguous` (routes or mappings that hold disagree) or `Unknown` with a
`MissingHop`: nothing arrives, or some branch is undecided (a mapping in force without parameters, a conflict a
later hop's validity would settle, too many readings); it names the clocks reached, the mappings that do not
apply, and the readings that did arrive (ADR 0011 §5).

Each result has a `to_json` and a JSON Schema definition (`#/$defs/NodeResult`, `ClaimsResult`,
`NeighboursResult`, `EpisodesResult`, `SpatialResult`). A `Knowledge`-wrapped result is
`{"knowledge": "known", "value": …}` or `{"knowledge": "not_covered"}`. Goldens `result.*.json` pin the shapes.
`schema.reference.ReferenceReader` defines the expected answers. A consumer can test any reader with the suite:

```python
from neptune_memory.contract.suite import CHECKS, load_golden

GOLDEN = load_golden(REPO / "contracts/graph-schema/v1.8.0/golden/graph.json")

@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_graph_schema_contract(check):
    check(my_reader_factory, GOLDEN)  # factory: GraphDocument -> MemoryReader
```

## Guarantees

1. **Provenance always.** Every claim cites at least one `EvidenceRef`, its Ledger records and its transform.
   An inferred claim names its model, and an observed or stated claim never does. Confidence is `not_applicable`
   for deterministic claims.
2. **Deterministic.** The same Ledger snapshot, consolidators, versions and configs give byte-identical claims and
   graph documents (ADR 0003 §4). `resolve` is order-free and idempotent (ADR 0005, P1–P8).
3. **Nothing deleted.** Superseding sets `superseded_at` and adds closure versions. Content never changes.
4. **As-of is a true snapshot.** A result at `as_of = tx` holds exactly the claims and findings active at `tx`
   (`recorded_at ≤ tx < superseded_at`). Each is presented as known then, with `superseded_at` masked to `open`, so
   no later knowledge leaks in. This equals resolving only what was recorded by `tx`.
5. **Superseding is visible exactly.** A version superseded at `s` is returned for `as_of < s`, if it was recorded
   by then, and never for `as_of ≥ s`. The claim that displaced it lists it in `supersedes`.
6. **Inference filter.** `include_inferred=False` returns exactly the observed and stated claims. A finding whose
   own claim is inferred comes back only if it also names a returned claim.
7. **Findings travel with claims.** A finding active at `as_of` comes back with any query that returns a claim it
   names. It also comes back with any query its own `claim` matches, even when that claim is no longer, or never
   was, current: an `overridden_on_arrival` stays visible after its winners are superseded.
8. **Clocks are never coerced.** `during` filters on its own clock. Claims on other clocks are returned in
   `other_clocks`, never dropped and never compared.
9. **One answer per `as_of`.** An `as_of` later than `head` raises `AsOfBeyondHeadError`.
10. **Missingness is explicit.** A node the graph never names is `NotCovered`. Provisional queries answer
    `NotCovered`, never an empty result.
11. **Identity is never merged.** `same_as` is an edge that queries traverse (`schema.traverse.same_as_closure`).
    It is grounded only by `memory.identity`, never by inference: a compiler `IdentityLink` with a `Known` right
    side, configuration lineage, or a person's `same_identity` assertion that no effective `retract` withdraws
    ([ADR 0008](adr/0008-identity-consolidator-on-compiler-identity-links-and-assertions.md)).
    `same_as_candidate` is pairwise: every candidate of an `Ambiguous` link, and threads citing one source. People are named only by declared
    identifiers: never blank, never padded with whitespace.
    An `Ambiguous` validity window, window bound or assertion `authored_at` is never read as unstated: the
    statement becomes `same_as_candidate` pairs, one per reading of its window, each citing that reading (more
    than 64 readings: `identity.untimeable_window`, no claim). A statement that states no start holds from its
    subject's first thread record, and the claim lists that thread in `provenance.records`, so a conventional
    start is told from a stated one.
    A `retract` whose `retracts` is `Ambiguous`, or that names one candidate of a target's `Ambiguous` `identifier`,
    only possibly names that target. Retraction is labelled over certain and possible retractions together: an
    assertion is retracted only by a certain retract that stands, and effective only when every retract that may
    name it is retracted. One that certain retractions alone would settle but a possible one leaves open is
    reported as `identity.retraction_ambiguous`. A `same_identity` of that kind becomes `same_as_candidate` pairs
    that cite the retracts leaving it in doubt, and a `distinct_identity` of that kind suppresses nothing. A
    `same_identity` whose own `identifier` is `Ambiguous` is always candidates, never `same_as`.
12. **Configuration is never guessed.** `memory.configuration` ([ADR 0010](adr/0010-configuration-lineage-consolidator.md))
    places configurations on machines only from lifecycle records, on each record's own clock, and on runs only from
    the compiler's snapshot bindings. Where the evidence states none, the claim is `configuration_unknown`, never the
    nearest configuration in time; where records disagree, every reading is a `configuration_candidate`. No
    `succeeds` is claimed across a gap. `not_covered_by_authorisation` is an observation about the Ledger's envelopes,
    made only over windows whose bounds are stated and only where they compare on one clock; an unstated bound or
    envelope end is never read as open.
13. **Runs are threads, never merged** ([ADR 0009](adr/0009-run-threads-and-cross-package-continuation.md)). A run
    node is a compiler `Run`'s declared logical id, else `record:<run record id>`. Every claim about a run holds over
    its stated `[first, last]` on its own clock, and again on a civil clock only where a `timestamp_domain` or a
    stated `clock_mapping` puts it there. `continues` links the parts of one run its `RunAssembly` states, in time
    order on one clock; parts whose clocks cannot be compared are `continues_candidate` both ways. `recorded_by`
    and `at_site` are `Known` only when every ground names one id, and `executes_task` holds every task stated;
    otherwise each reading is a `*_candidate` claim. `consolidate.runs.involvement` reads a role back as `Known`,
    `Ambiguous`, `Unknown` or `NotCovered`.
14. **No clock mapping is invented.** `maps_to` and `clock_map` rest only on a compiler `ClockMapping` (declared:
    `observed` or `stated`, from `memory.time`) or a compiler estimate (`inferred`, from `memory.time_estimates`),
    or on a chain of them; Memory never estimates an offset, never assumes an unstated validity open, and never
    re-times a claim. A later-starting mapping of one clock pair holds from its start, and the earlier one's
    claims end there ([ADR 0011](adr/0011-time-domain-registry-clocks-mappings-and-chains-never-estimated.md)).
15. **Episodes are stated attempts, never inferred** ([ADR 0012](adr/0012-episodes-from-stated-task-evidence.md)).
    A run with stated task evidence holds one episode (`episode_of`, its `executes_task`), bounded on each clock by
    the span its records state (`starts_at`, `ends_at`, only on a clock the records state it on, never on a
    mapping's projection); a run with none, or with no time placement, holds no episode. A stated stop
    (`incident_record`) inside the episode makes the end `ends_at_candidate` readings. `intervened` names an
    `Intervention` that names the run, or names its machine and surely overlaps it on one clock; an overlap that
    holds only within a projection's error is `intervened_candidate`. No record declares an outcome yet, so
    `outcome` reads `Unknown`. `consolidate.episodes.episodes_of`, `boundary_of` and `outcome_of` read them back
    as `Known`, `Ambiguous`, `Unknown` or `NotCovered`.
16. **Events are what one record states, and co-occurrence is never cause**
    ([ADR 0013](adr/0013-event-index-evidence-linked-event-claims-and-co-occurrence.md)). An event node is
    `record:<rec id>` (a timeline entry `record:<rec id>/timeline/<i>`). Each event comes from an
    `incident_record`, an `intervention`, or a row of a table the event consolidator's config declares. Every
    claim about an event holds over its time as declared (an instant is `[t, t + 1 tick)`), and again on each
    clock a stated `clock_mapping` reaches directly, citing that mapping. `event_kind` is set only through a
    declared vendor mapping. `co_occurs_within` links two events from different sources, one claim each way, and
    its valid interval is the window. Events on clocks no mapping relates are never compared. A mapping that is
    too coarse to decide, or that states no residual bound, gives a finding, never a claim. An end that is
    declared but not stated (blank or ambiguous) leaves the event open and is never an instant.
17. **Calibration is never converted or judged.** `memory.calibration` ([ADR 0014](adr/0014-calibration-history-and-drift-consolidator.md))
    places a calibration on a sensor only through its declared machine and subject and the hardware configurations
    the machine declares or its chain places; several readings are `calibration_candidate`s, and so is a calibration
    whose frame binding contradicts its sensor's configuration graph. A `calibrated_with` starts at a stated
    `valid_from` only, and its unstated end is the next calibration's stated start only where no calibration whose
    start or placement is in doubt may come first; otherwise it is candidates or no interval, and no drift is
    claimed across the doubt. `drift` exists only between equal declared units (or forms without one) and equal declared
    interpretations; anything else is a finding, never a converted value, and no threshold is applied.
18. **Coverage is never inferred** ([ADR 0015](adr/0015-coverage-and-health-consolidator.md)). `recorded` spans and
    `rate_observed` come from the Ledger's series coverage; `gap` only where the stream's declared first or last
    instant predicts samples the series does not reach, and never while a sample lacks a tick on that clock.
    `rate_declared` and `rate_observed` stand side by side; nothing judges a tolerance. `integrity_finding` carries
    the compiler's severity verbatim. A configured sensor is `sensor_not_recorded` only when nothing in the run could
    be its data and the recording is closed and unflagged; otherwise `sensor_presence_unknown`, never absent.

## Caveat: a resolver configuration is a store generation

Every closure id and finding id covers the resolver configuration hash: priorities, vocabulary and vocabulary
version. Adding a consolidator, changing a priority or extending the vocabulary therefore re-ids every closure and
finding. The configuration's hash is the **generation** (`MemoryReader.generation`, `Graph.generation`).

- ADR 0003 §3(a) ("`as_of` at an earlier transaction answers as before") holds only within one generation.
- An ADR 0004 append-only store starts a new generation on any configuration change. It re-resolves from the
  assertions and never patches the old history.
- Never compare ids or answers across generations.

## Not in v1

- The `episodes` and `spatial` queries (G3). Episode claims exist from 1.5.0; `MemoryReader.episodes` still
  answers `NotCovered`.
- Cross-clock comparison inside the resolver: claims on two clocks are still never compared there
  (`clock_mismatch`). A consumer relates them with `schema.clocks.convert`; no claim is ever re-timed.
- A Postgres-backed `MemoryReader`. `MemoryStore` stays provisional (ADR 0004 §5); G2 maps its rows to `Claim`,
  masks `superseded_at`, joins findings and runs this suite.
- Withdrawal ([ADR 0007](adr/0007-g1-gate-withdrawal-names-evidence-status-and-the-final-store.md) §5, MVL-132).
  Until it lands, a claim a consolidator stops emitting stays current: an operator cannot retract a `many`
  fact such as `same_as`, and an upgrade that emits nothing retires nothing. A `one` fact is corrected by a new
  stated claim, which supersedes it. The identity consolidator already stops emitting a retracted `same_as`
  (ADR 0008 §3); withdrawal makes that end it. Likewise a revised clock mapping: the build after the revision
  emits the old mapping's claims closed at the revision, but the version emitted open before stays current
  beside them until withdrawal ends it (ADR 0011 §5).
- Evidence status beside claims (ADR 0007 §6, MVL-132): a `Knowledge[EvidenceStatus]` for every cited source,
  `NotCovered` until the Ledger catalog emits a retention signal. v1 results carry no status map, which never
  means "available".

Each lands as a minor version: the shapes above do not change.
