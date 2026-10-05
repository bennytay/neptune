# Memory's guarantees

What Memory promises about the graph as a whole, how each promise is checked, and where it stops.
What a reader can rely on per claim and per query is in [`graph-schema.md`](graph-schema.md#guarantees).

## 1. Same Ledger snapshot, same consolidator set: the same graph

**Statement.** Take one Ledger snapshot and one set of registered consolidators, with the same versions
and resolved configs. Consolidating that snapshot from scratch always gives the same results:

- the same `MemorySnapshot`, with equal `id` and `claims_hash`;
- a byte-identical graph document (`graph.json`);
- a byte-identical `memory dump`.

This holds whatever order the consolidators were registered in, and on any host, process, hash seed or
time zone. Consolidating the packages as they are registered, one snapshot after another, gives the same
results at the last snapshot as a rebuild from scratch:

- the same `MemorySnapshot`;
- the same current claims;
- every claim the later builds no longer emit is withdrawn, not deleted, so `as_of` an earlier snapshot
  still answers as it did then.

**Why it holds.** These decisions make it hold:

- Consolidators are pure functions of the snapshot, the claims they read, and their config. They read no
  clock, randomness, network or files ([ADR 0003](adr/0003-identity-policy-consolidator-contract-and-lineage.md)).
- They run in dependency order with ties broken by id, and each one sees only the claims of what it reads
  ([ADR 0016](adr/0016-memory-snapshots-rebuild-cli-and-build-withdrawal.md) §3).
- The runner sorts every output. A build is a complete statement of its lineage, so whatever a later build
  does not emit is withdrawn at that build (ADR 0016 §2, [ADR 0007](adr/0007-g1-gate-withdrawal-names-evidence-status-and-the-final-store.md) §5).
- Every file Memory writes is canonical JSON.

**How it is checked.** `tests/test_rebuild_determinism_memory.py` runs in every `make check` and in CI.
It uses the archetype Ledger (`tests/memory_archetype_ledger.py`):

- aerial, legged, manipulator, mobile, humanoid and marine robots;
- three registration transactions;
- all eight G2 consolidators emitting;
- a retraction, a clock re-sync, a run continued by a later upload, cut-short missions and a closing
  recalibration, so later builds withdraw claims.

The tests check these cases:

- three pairs of shuffled registration orders give byte-identical documents and snapshots;
- two `memory rebuild` processes, under different `PYTHONHASHSEED` and `TZ`, dump byte-identical claims,
  graphs and snapshot records;
- incremental `memory consolidate` at snapshots 1, 2 and 3 records the same `MemorySnapshot` as
  `memory rebuild` at 3, and dumps the same claims as of the head;
- consolidating the head again writes nothing;
- what disappears between snapshots stays in the history, superseded at the build that dropped it.

The resolver's own properties back this up. P1 to P10 are in `tests/test_supersede_properties_memory.py`
and `tests/test_supersede_withdrawal_memory.py`.

**Reproduce it.**

```sh
memory --graphs /tmp/g --tenant a rebuild --ledger ledger.json --snapshot 3
memory --graphs /tmp/g --tenant b consolidate --ledger ledger.json --snapshot 1
memory --graphs /tmp/g --tenant b consolidate --ledger ledger.json --snapshot 2
memory --graphs /tmp/g --tenant b consolidate --ledger ledger.json --snapshot 3
memory --graphs /tmp/g --tenant a dump --as-of 3 --out a.jsonl
memory --graphs /tmp/g --tenant b dump --as-of 3 --out b.jsonl
```

The two snapshot records at `snapshots/3.json` hold the same `snapshot.id`. Apart from `recorded_at` and
`supersedes`, the two dumps hold the same claims. A claim the incremental graph learned earlier keeps that
earlier recording.

**Where it stops.**

- **The input is fixed.** "The same snapshot" means the same packages and records. Changing a
  consolidator's version or config, or adding one, changes the `MemorySnapshot`. Adding a consolidator
  also changes the generation. Removing one takes a rebuild: `consolidate` refuses it.
- **Contested facts can split differently.** An incremental graph and a rebuild hold the same claim set.
  How they split a contested `one` fact follows arrival order (ADR 0005), and a rebuild flattens that to a
  single transaction. Where standing claims contest a fact that no withdrawal touched, the two can split
  it differently:
  - a full tie goes to the later arrival;
  - a part one claim took stays taken.

  The archetype has no such contest, and the test pins exact equality there. The `MemorySnapshot` and
  the claim set are equal in every case.
- **The CLI reads a Ledger export.** It reads a Ledger export (`neptune_memory.ledger.LedgerExport`),
  not the catalog API, until Memory adopts it.
- **The graph is stored on disk.** It lives in a directory per tenant (`neptune_memory.store.graphs`)
  until G3 moves it onto PostgreSQL behind the same tests.
- **Rebuild time is not yet measured at scale.** The 10^8-claim rebuild budget (ADR 0007 §7) is still
  unmeasured on production-class hardware.
