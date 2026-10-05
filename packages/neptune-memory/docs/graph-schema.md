# Graph schema v1

This page states what Context, Deploy and Learn may rely on when they read Memory. The contract is
`contracts/graph-schema/v1.2.0/` (`GRAPH_SCHEMA_VERSION = 1`); [ADR 0006](adr/0006-graph-schema-v1-contract-surface-and-memory-reader.md)
records the decisions behind it. 1.1.0 (minor) adds the `stream` and `document` node types and `has_name`
([ADR 0008](adr/0008-identity-consolidator-on-compiler-identity-links-and-assertions.md) §6). 1.2.0 (minor) adds the
configuration lineage predicates ([ADR 0010](adr/0010-configuration-lineage-consolidator.md) §6). Every earlier
golden still validates and its graph still passes the suite. The code is `neptune_memory.schema`. `tests/test_pins_memory.py` checks that this
page names every node type, predicate and finding code.

## Nodes

A node is `NodeRef(node_type, node_id)` and nothing else; every attribute and every edge is a claim (ADR 0002 §1).
`node_id` is opaque: a Ledger thread's declared logical id `<namespace>:<value>` (ADR 0003 §1).

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
| context | `deployment` | a deployment, with a summary |
| context | `fleet` | a fleet, with a summary |
| context | `programme` | a programme, with a summary |

The Episode tier is the Ledger's records and evidence refs. They are not nodes: a claim points into the tier with
a `LedgerRecordRef` object and `EvidenceRef`s in its provenance.

## Predicates (`CORE_PREDICATES`, `VOCABULARY_VERSION = 4`)

A `one` predicate holds at most one object per subject at any valid instant on one clock, so a different object
over an overlapping interval supersedes. A `many` predicate never contradicts. The vocabulary only widens within a
major version (ADR 0002 §5).

| Predicate | Subject | Object | Cardinality | Meaning |
|---|---|---|---|---|
| `authorised_configuration` | site | configuration | many | an authorisation envelope approves this configuration at the site over the interval |
| `configuration_active_during` | run | configuration | many | a configuration the run ran with, over the bound part of the run (a snapshot binding) |
| `configuration_candidate` | machine, run | configuration | many | ambiguous: the configuration in force could be this one; one claim per reading |
| `configuration_unknown` | machine, run | record | many | no configuration is stated over the interval; the record leaves it open, never filled |
| `deployed_at` | deployment | site | one | where a deployment takes place |
| `episode_of` | episode | run | one | the run an episode segments |
| `evidenced_by` | any node | record | many | a Ledger record about the node (Episode tier, by id) |
| `executes_task` | episode, run | task | many | a task attempted |
| `governed_by` | deployment, fleet, machine, site | policy | many | an operating rule or control policy that applies |
| `has_calibration` | sensor | configuration | one | the calibration in force |
| `has_configuration` | deployment, machine, sensor | configuration | many | a parameter set, description file or other configuration in force |
| `has_name` | any node | text | one | a declared display name, verbatim; never an identifier |
| `has_summary` | deployment, fleet, programme | text | one | a context node's summary |
| `located_at` | asset, machine | site, zone | one | where it is |
| `maintenance_state` | asset, machine, sensor | text | one | serviceability as a record states it, verbatim |
| `member_of_fleet` | machine | fleet | one | the fleet a machine belongs to |
| `mounted_on` | sensor | asset, machine | one | what a sensor is attached to |
| `not_covered_by_authorisation` | run | configuration | many | observed: no authorisation envelope in the Ledger names the configuration then |
| `operated_by` | run | person | many | a declared operator or supervisor |
| `part_of_programme` | deployment, fleet | programme | one | the owning programme |
| `rated_payload` | machine | quantity | one | rated payload, unit as declared |
| `recorded_by` | run | machine | one | the machine whose log a run is |
| `runs_model` | machine | model_version | many | a learned model it runs |
| `runs_software` | machine, sensor | software_version | many | installed software |
| `same_as` | any node | same type | many | the same real-world thing: declared identifier, configuration lineage or operator |
| `same_as_candidate` | any node | same type | many | ambiguous: the evidence could mean either; one claim each way |
| `succeeds` | configuration | configuration | many | took over from the object on a machine's chain; valid while the subject is in force |
| `zone_of` | zone | site | one | the site a zone belongs to |

Object value types are `text`, `integer`, `real`, `boolean`, `quantity` (a unit exactly as declared: `Known`,
`Unknown` or `Ambiguous`), `instant` (a `Timestamp` on its own clock) and `record`.

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

Each result has a `to_json` and a JSON Schema definition (`#/$defs/NodeResult`, `ClaimsResult`,
`NeighboursResult`, `EpisodesResult`, `SpatialResult`). A `Knowledge`-wrapped result is
`{"knowledge": "known", "value": …}` or `{"knowledge": "not_covered"}`. Goldens `result.*.json` pin the shapes.
`schema.reference.ReferenceReader` defines the expected answers. A consumer can test any reader with the suite:

```python
from neptune_memory.contract.suite import CHECKS, load_golden

GOLDEN = load_golden(REPO / "contracts/graph-schema/v1.2.0/golden/graph.json")

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
12. **Configuration is never guessed.** `memory.configuration` ([ADR 0010](adr/0010-configuration-lineage-consolidator.md))
    places configurations on machines only from lifecycle records, on each record's own clock, and on runs only from
    the compiler's snapshot bindings. Where the evidence states none, the claim is `configuration_unknown`, never the
    nearest configuration in time; where records disagree, every reading is a `configuration_candidate`. No
    `succeeds` is claimed across a gap. `not_covered_by_authorisation` is an observation about the Ledger's envelopes,
    made only where the windows compare on one clock.

## Caveat: a resolver configuration is a store generation

Every closure id and finding id covers the resolver configuration hash: priorities, vocabulary and vocabulary
version. Adding a consolidator, changing a priority or extending the vocabulary therefore re-ids every closure and
finding. The configuration's hash is the **generation** (`MemoryReader.generation`, `Graph.generation`).

- ADR 0003 §3(a) ("`as_of` at an earlier transaction answers as before") holds only within one generation.
- An ADR 0004 append-only store starts a new generation on any configuration change. It re-resolves from the
  assertions and never patches the old history.
- Never compare ids or answers across generations.

## Not in v1

- `episodes` and `spatial` structure (G3).
- Cross-clock comparison, which waits for the compiler's `ClockMapping` records (root ADR 0050 §5; Memory MVL-130).
- A Postgres-backed `MemoryReader`. `MemoryStore` stays provisional (ADR 0004 §5); G2 maps its rows to `Claim`,
  masks `superseded_at`, joins findings and runs this suite.
- Withdrawal ([ADR 0007](adr/0007-g1-gate-withdrawal-names-evidence-status-and-the-final-store.md) §5, MVL-132).
  Until it lands, a claim a consolidator stops emitting stays current: an operator cannot retract a `many`
  fact such as `same_as`, and an upgrade that emits nothing retires nothing. A `one` fact is corrected by a new
  stated claim, which supersedes it. The identity consolidator already stops emitting a retracted `same_as`
  (ADR 0008 §3); withdrawal makes that end it.
- Evidence status beside claims (ADR 0007 §6, MVL-132): a `Knowledge[EvidenceStatus]` for every cited source,
  `NotCovered` until the Ledger catalog emits a retention signal. v1 results carry no status map, which never
  means "available".

Each lands as a minor version: the shapes above do not change.
