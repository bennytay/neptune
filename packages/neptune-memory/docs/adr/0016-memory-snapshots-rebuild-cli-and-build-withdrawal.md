# 0016 — Memory snapshots, the rebuild CLI and build withdrawal

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-132

## Context

ADR 0003 §4 makes Memory a deterministic function of (Ledger snapshot, ordered consolidator set,
versions, configs), and G2 closes by proving it: the same snapshot and set must give the same graph,
byte for byte, and consolidating packages as they arrive must give the graph a rebuild gives. ADR 0007
§5 fixed the shape of withdrawal and assigned the build to this issue: without it a claim a consolidator
stops emitting (a retracted `same_as`, a revised clock mapping, everything an empty upgrade drops) stays
current, and incremental consolidation drifts from a rebuild. Four strict xfails from the G1 gate,
MVL-126 and MVL-130 pin that gap.

Three things were missing: a record of what one consolidation produced, a way to run the eight G2
consolidators in an order that does not depend on how they were registered, and a command that a CI job
and an operator can both run.

## Decision

### 1. A `MemorySnapshot` records one consolidation

`consolidate/snapshot.py`. `consolidate(ledger, registrations, snapshot)` runs the plan (§3) over the
Ledger as of `snapshot` and returns every `Consolidation` and a `MemorySnapshot`:

- `ledger_snapshot`: the Ledger transaction (catalog `tx_seq`), which is also every claim's `recorded_at`;
- `packages`: every package the snapshot lists, with its schema version;
- `consolidators`, in plan order: each one's transform (id, version, config hash, model if any), `after`,
  `priority`, and its claim and finding counts;
- `claim_count` and `claims_hash`: the sha256 of the canonical JSON list `[{"claim": content_json, "id"}]`
  over every claim, in id order;
- `finding_count` and `findings_hash`, the same over `[{"consolidator_id", "finding"}]` in (consolidator,
  finding id) order;
- `generation`, the resolver configuration hash, and `graph_schema_version`.

Its `id` is the sha256 of all of that. Two consolidations are the same exactly when their ids are equal.

### 2. Withdrawal as built

ADR 0007 §5 stands, with these details:

1. **A `Build` carries the ids it emitted.** It is `(consolidator_id, version, config_hash, recorded_at,
   claims)`. `claims` is every claim id the run emitted, possibly none. `resolve` keeps only the earliest
   recording of an id, so without the ids a build is not checkable after a round trip.
   `Consolidation.build` produces it. `resolve(..., builds)` refuses builds that disagree with the claims:
   two builds of one consolidator at one transaction, a build naming a claim of another lineage or not yet
   recorded, or a recording of a built consolidator that no build at its transaction emitted. Builds join
   the lineage checks, so an empty build cannot bring a replaced lineage back. A consolidator that did not
   run to completion records **no** build. That covers `consolidate.failed` and `consolidate.bad_output`,
   and also `consolidate.dependency_failed`, where something it reads failed and it was not run. Its
   output is no statement of its lineage, so what it held stands until a later build of it completes.
2. **Builds land before arrivals.** At transaction `t`, every build at `t` is applied, then the
   assertions first recorded at `t` arrive in ADR 0005 order. A build of a new lineage retires the
   consolidator's other lineages. Every build withdraws the current versions of its lineage that it did
   not emit, closures included, by setting `superseded_at = t`. An assertion with no current version is
   withdrawn too, so it stands in no later contest.
3. **A claim emitted again is restated.** If a build emits an assertion that an earlier build withdrew,
   the assertion is not reopened, because its transaction interval is closed and ids are unique. A
   *restatement* is recorded at `t` instead. It is a resolver version (`memory.supersede`) of the
   assertion over its valid interval. Its evidence is the assertion's evidence, then the evidence of
   whatever cuts it. It `supersedes` the assertion, and its `config_hash` covers
   `{at: t, resolver, restates: id}`, so no id repeats.
4. **With builds, a `one` fact is placed order-free.** This supersedes ADR 0005 §1.1 (a full tie goes
   to the later arrival) and §1.4 (no resurrection) for histories resolved with builds. Without builds,
   ADR 0005 holds unchanged.
   - Each transaction places every `one` fact it touched: a fact with a new assertion, a withdrawal, a
     retirement or a restatement.
   - The fact is placed from its standing assertions: those not withdrawn and of their consolidator's
     latest lineage.
   - Each standing assertion holds its valid interval minus that of every standing assertion that
     contradicts it and is stronger. Contradicting means another object, an overlapping interval and
     the same clock. Strength is ordered by `(assertion rank, original valid_from, priority, id)`, a
     total order whose tail is a rebuild's arrival order.
   - So the current fact is a function of the standing set alone, whichever transactions brought it.
     A withdrawn or retired winner no longer keeps what it took, and a full tie goes the same way in
     every run.
   - An assertion already placed whose versions hold exactly that, on the same grounds, keeps them. Any
     other has them superseded at `t` and is restated over what it now holds.
   - A new assertion is its own version when nothing cuts it. Otherwise it is recorded as superseded on
     arrival with split closures, or with an `overridden_on_arrival` finding when nothing is left.
5. **Every build is complete over its whole snapshot.** This supersedes ADR 0007 §5.3. The consolidators
   read across packages (run continuation, identity links, configuration chains), so a build over only
   the new packages would miss claims. "Incremental" means consolidating the new snapshot onto the
   existing graph (§4), never consolidating the new packages alone.
6. `RESOLVER_VERSION` stays `"2"`. Without builds the output is byte-identical to before, and builds are
   new input, not a new function of the old input. New properties: **P9**, every current version is of an
   assertion that the latest build of its consolidator emitted. **P10**, every `many` assertion that
   build emitted has a current version. P1, P2, P4 and P5 hold with builds in the input.
7. The graph document gains an optional `builds` list (`#/$defs/Build`), ordered by `(recorded_at,
   consolidator_id)`. A document without builds writes no key. This is graph-schema **1.9.0**, a minor
   release, and the vocabulary stays 10. The golden graph is resolved with its 25 builds, and its claims,
   findings and answers are unchanged.

### 3. Registrations are planned by dependency, never by registration order

A `Registration` is `(consolidator, resolved config, after, priority)`. `after` lists the consolidators
whose claims it reads. `plan` orders registrations topologically, and among those ready it takes the
smallest consolidator id first. It refuses a repeated id, the resolver's id, an unknown `after` and a
cycle (`PlanError`). Each consolidator receives as `previous` only the claims of the consolidators it
reads, directly or transitively, sorted by id. A consolidator that reads nothing sees nothing, so neither
registration order nor plan position can reach its output. `default_registrations()` holds the eight G2
consolidators:

| Consolidator | Reads (`after`) |
|---|---|
| `memory.identity`, `memory.runs`, `memory.time`, `memory.configuration`, `memory.events`, `memory.coverage` | nothing |
| `memory.calibration` | `memory.configuration` |
| `memory.episodes` | `memory.runs` |

Every priority is 0. `memory.events` gets `event_records.resolve_config({})`; every other consolidator
gets `{}`.

### 4. The `memory` CLI and the tenant graph directory

`memory --graphs ROOT --tenant T`, with one of these commands:

- **`consolidate --ledger EXPORT --snapshot N`**: `consolidate` followed by `extend`. `extend` takes the
  tenant graph's assertions and builds, adds the run's, and resolves them under the run's priorities.
  It refuses (exit 1) a snapshot that does not follow the graph's head, and a consolidator that built
  the graph but is no longer registered: its claims would stay current forever, so that takes a rebuild.
  It prints the `MemorySnapshot`. Consolidating the head again with an identical result writes nothing.
- **`rebuild`**: drops the tenant's graph and consolidates from scratch. The run and its graph are
  computed first and written over the old graph; only then do the other snapshot records go. A refused
  rebuild leaves the old graph as it was.
- **`dump [--as-of TX] [--out FILE]`**: every claim version, or the `as_of` view, as canonical JSON Lines
  ordered by claim id.

Exit status is 0 for done, 1 for refused, and 2 for usage errors or input that cannot be read.

`store/graphs.TenantGraphs` keeps one directory per tenant, holding `graph.json` and
`snapshots/<N>.json` (the snapshot and its findings). Files are canonical JSON, written to `*.tmp`,
synced, then renamed, and the directory is synced. A tenant is a token, never a path. A symlink in a
tenant's directory is refused, and a rebuild refuses a directory that holds files Memory did not write. This is the reference persistence until G3
maps `Claim` onto `PostgresStore`, whose row shape stays provisional (ADR 0004, 0007 §7).

Until Memory adopts the catalog API, the CLI reads a **Ledger export**
(`ledger.LedgerExport`, kind `memory.ledger_export`). It holds a head and each package's id, schema
version, registering transaction and records. `at(N)` is the Ledger as of `N`. The export is parsed
strictly, but it need not be canonical JSON. Memory's catalog-api pin stays `pending` (lock 1.1.0): the
CLI uses no catalog-api surface, so the 1.7.0 bump waits for the PR that adopts the API.

### 5. The guarantee and its test

`docs/guarantees.md` states the guarantee. `tests/test_rebuild_determinism_memory.py` holds it over
the archetype Ledger (`tests/memory_archetype_ledger.py`: aerial, legged, manipulator, mobile, humanoid
and marine robots; three registration transactions; all eight consolidators emitting; claims that later builds
withdraw). Three things are checked:

- Shuffled registration orders give byte-identical documents and snapshots.
- Two `memory rebuild` processes under different `PYTHONHASHSEED` and `TZ` dump identical bytes.
- Incremental consolidation (snapshots 1, 2, 3) holds the rebuild's `MemorySnapshot` and, at the head,
  its *head projection*.

The head projection is the sorted multiset, over every current version, of three things:

- the assertion it is a version of, following `supersedes` past resolver versions;
- its valid interval;
- its object.

Incremental and rebuilt graphs have equal claim sets by construction, because the latest builds are pure
functions of the snapshot. Their head projections are equal because §2.4 places each fact from the
standing set alone. Version ids, resolver provenance and evidence order may differ, since an incremental
graph restates where a rebuild records the assertion itself.

The archetype contests no fact. `tests/memory_contest_ledger.py` therefore adds a test consolidator over
a Ledger with these cases:

- a contest;
- a withdrawn winner that frees a loser;
- a re-emitted withdrawn claim;
- full ties within and across transactions;
- a three-way chain.

Its projection is compared at every snapshot. Seeded random histories in
`tests/test_supersede_withdrawal_memory.py` (two consolidators, withdrawals, re-emissions, half crowded
with ties) compare the same projection at the resolver level.

## Alternatives considered

- **Builds without claim ids**, with withdrawal inferred from re-recordings: ADR 0007 already rejected
  this. The evidence is gone after one round trip, and an empty build is invisible.
- **Reopen a withdrawn version when its claim is emitted again**: this breaks the closed transaction
  interval and unique version ids. A restatement is the same move a split closure makes.
- **Keep ADR 0005 "no resurrection" under withdrawal**: a retracted or revised claim would keep shaping the
  graph through what it once cut, and incremental would drift from rebuild on every withdrawn winner.
  Placement is bounded to the facts a transaction touched.
- **Re-place only what a withdrawal touched, in arrival order, and break full ties by (priority, id)**:
  arrival order still decides a three-way chain. If A beats B, B beats C, and A and C share an object,
  then whether C keeps what B took from it depends on which of them arrived first. Seeded histories
  diverged at the assertion level even with no ties at all.
- **Package-scoped builds** (ADR 0007 §5.3): run continuation, identity links and configuration chains span
  packages, so a build over only new packages misses or wrongly withdraws claims.
- **Plan in registration order, or pass every earlier claim as `previous`**: either lets a shuffle
  change a claim, which is exactly what the guarantee forbids.
- **Bump `RESOLVER_VERSION`**: this re-ids every closure in every published golden, for no change in
  output without builds.
- **Write the CLI against `PostgresStore`**: its rows are not `Claim`s until G3. The file store is the
  reference that a database store will be checked against.
- **Require canonical JSON for the Ledger export**: it adds nothing that strict parsing does not, and
  any producer that pretty-prints would be refused.

## Consequences

- The four MVL-132 xfails pass: a retracted `same_as` ends, an empty upgrade retires, and a revised
  mapping's open version ends. Consumers pinned below 1.9.0 read documents without builds unchanged.
- G3 can count on rebuild being deterministic and on incremental consolidation matching a rebuild, and
  can map `TenantGraphs` onto Postgres behind the same tests.
- Still open from ADR 0007, now tracked on MVL-132 for the coordinator to re-home:
  - evidence status (§6), because catalog-api 1.7.0 emits no retention signal;
  - the 10^8 rebuild budget and wide-scope filtered recall (§7), which need production-class hardware.
- Revisit this decision in any of these cases:
  - a consolidator must read only part of a snapshot;
  - re-placement churn on hub facts shows up in store benchmarks;
  - the catalog API gives Memory a snapshot handle and makes the export unnecessary.
