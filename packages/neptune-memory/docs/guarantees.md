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
- the same *head projection*, the sorted multiset over every current version of three things:
  - the assertion it is a version of, following `supersedes` past resolver versions;
  - its valid interval;
  - its object.

  So the same claims hold the same intervals with the same objects. Version ids, resolver provenance
  and evidence order may differ, because an incremental graph restates where a rebuild records the
  claim itself;
- every claim the later builds no longer emit is withdrawn, not deleted, so `as_of` an earlier snapshot
  still answers as it did then.

**Why it holds.** These decisions make it hold:

- Consolidators are pure functions of the snapshot, the claims they read, and their config. They read no
  clock, randomness, network or files ([ADR 0003](adr/0003-identity-policy-consolidator-contract-and-lineage.md)).
- They run in dependency order with ties broken by id, and each one sees only the claims of what it reads
  ([ADR 0016](adr/0016-memory-snapshots-rebuild-cli-and-build-withdrawal.md) §3).
- The runner sorts every output. A build is a complete statement of its lineage, so whatever a later build
  does not emit is withdrawn at that build (ADR 0016 §2, [ADR 0007](adr/0007-g1-gate-withdrawal-names-evidence-status-and-the-final-store.md) §5).
- With builds, each `one` fact is placed from the claims still standing, order-free: a claim holds its
  interval minus that of every stronger contradicting claim. Strength is `(rank, valid_from, priority,
  id)`, so a full tie goes the same way whichever claim came first (ADR 0016 §2.4).
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
  `memory rebuild` at 3, and the same head projection;
- the contested Ledger (`tests/memory_contest_ledger.py`) holds the same head projection, incrementally
  and rebuilt, at each of its four snapshots. It covers:
  - a contest;
  - a withdrawn winner that frees a loser;
  - a re-emitted claim;
  - full ties within and across packages;
  - a three-way chain;
- seeded random histories with withdrawals, re-emissions and ties do the same at the resolver level;
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

The two snapshot records at `snapshots/3.json` hold the same `snapshot.id`. The two graphs have the same
head projection: each current version of `b`, followed back to its assertion, matches one of `a` with the
same interval and object. A claim the incremental graph learned earlier keeps that earlier recording.

**Where it stops.**

- **The input is fixed.** "The same snapshot" means the same packages and records. Changing a
  consolidator's version or config, or adding one, changes the `MemorySnapshot`. Adding a consolidator
  also changes the generation. Removing one takes a rebuild: `consolidate` refuses it.
- **Builds are required.** The order-free placement applies to graphs resolved with builds, which every
  graph `memory` writes is. A history resolved without builds keeps ADR 0005's arrival order.
- **A consolidator that fails at the head.** If it crashes or returns bad output at the head, or reads one
  that did, it records no build. Incrementally, its earlier claims stay current. After a rebuild it has
  none. The `MemorySnapshot`s still match, and both record the failure.
- **The CLI reads a Ledger export.** It reads a Ledger export (`neptune_memory.ledger.LedgerExport`),
  not the catalog API, until Memory adopts it.
- **The graph is stored on disk.** It lives in a directory per tenant (`neptune_memory.store.graphs`)
  until G3 moves it onto PostgreSQL behind the same tests.
- **Rebuild time is not yet measured at scale.** The 10^8-claim rebuild budget (ADR 0007 §7) is still
  unmeasured on production-class hardware.
