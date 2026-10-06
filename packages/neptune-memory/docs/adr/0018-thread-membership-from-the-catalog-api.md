# 0018 — Thread membership from the catalog API

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191

## Context

The configuration lineage and calibration consolidators decide which node a record names. Today they
learn which ids have nodes, and which node an anchored record belongs to, only from `ledger_thread`
stand-in records (ADR 0003 §1, ADR 0010 §1). No real Ledger export contains those. On the acceptance
corpus that means Deploy's lifecycle records (5 `change_record`, 15 `maintenance_event`,
2 `authorisation_envelope`) are all `configuration.unthreaded_id`, and no run gets a node. So the graph
cannot say "what changed since the last good run".

Since catalog-api 1.7.0, the Ledger answers `threads_of(record_id)`: each thread the record belongs to,
in each registering package, with its roles (Ledger ADR 0003 §1.4). These facts constrain the design.
The acceptance corpus was registered in a real catalog to check them:

- **Lifecycle kinds join no thread.** The Ledger's thread table (Ledger ADR 0003 §2) has no row for
  `commissioning_baseline`, `maintenance_event`, `change_record`, `requalification_record` or
  `authorisation_envelope` (Ledger ADR 0010, Consequences). The catalog answers `found` with no
  memberships for all 22 of the corpus's lifecycle records. A Memory that only accepts ids a Ledger
  thread declares can never place a lifecycle record, however faithfully it reads the catalog.
- **Configuration and run threads are anchored.** A `HardwareConfiguration`, `SoftwareConfiguration` or
  `Calibration` opens a thread keyed by its record-level evidence. That key is not the configuration id a
  lifecycle record declares. A `Run` without a `Known` `logical_id` is anchored too.
- **Answers carry bookkeeping.** Every answer carries `as_of`, whose `tx_time` is the catalog's wall clock.
  Memory output must not depend on it.

## Decision

### 1. Reading

1. Memory pins catalog-api **1.7.0** (`pins.CATALOG_API_VERSION`, `contracts/lock.toml`). `LedgerReader`
   gains `threads_of(record_id) -> ThreadsOf | None`. `None` means the reader answers no thread queries,
   which is "not covered", never "in no thread". `ThreadsOf`, `Membership`, `UnresolvedMembership` and
   `ThreadKey` mirror the published `$defs`. `threads_of_from_json` parses them strictly from the wire
   form. Memory imports no Ledger code.
2. The Ledger export (ADR 0016 §4) gains an optional `threads`. It holds one answer per record id at the
   export's head, sorted by record id, without `api_version`, `as_of` and `findings`. Those say when and
   by which version a question was answered, and the export's `head` and `catalog_api_version` already
   state that. The answers must cover every record id the packages hold: a missing one would read as
   "not in the Ledger", so the export is refused instead. `LedgerExport.at(snapshot)` drops
   memberships from packages registered later. A record that only later packages hold becomes
   `unknown_record`.
3. The acceptance snapshot's export is made by a real catalog. `PostgresCatalog` runs on a throwaway
   PostgreSQL 16 from the `pgserver` wheel, registers both packages at transactions 1 and 2, and is asked
   `threads_of` for every record id.

### 2. Which node a record names

1. **Memberships open nodes.**
   - A declared key `(kind, LogicalId)`, in any role, is `node_ref(type, id)`. A `cites` membership
     (`Calibration.machine`) says the thread exists. It is the node a stand-in thread declaring that id
     keys. Node ids are the same function of the declared id from either source, so nothing is keyed
     twice.
   - An anchored key names a node only for the record that is its `subject`, and only that record looks
     it up.
   - An anchored run is `record:<run record id>`, the runs consolidator's node (ADR 0009 §2). Lineage
     siblings that cite one anchor stay two nodes, as there.
   - Any other anchored key is `thread:<thread id>`. The thread id is derived from the evidence anchor
     alone, so a parser upgrade that cites the same evidence keeps the node.
   - An anchored `cites` or `part_of` membership opens nothing.
   - Declared ids in the reserved namespaces `record` and `thread` name nothing, so they cannot forge one
     of these nodes.
2. **Lifecycle records declare their own ids.** For a record of a lifecycle kind above, a `Known`
   machine, configuration or site id names `node_ref(type, id)` when the catalog says it holds the record
   (`found`). The type comes from the field, as the Ledger types `Run.machine`. This supersedes ADR 0010
   §1's "only if a Ledger thread declares it" for these kinds. Equal values in different namespaces stay
   different nodes: `cmms.asset:ARM-3A`, `servicenow.ci:ARM-3A` and the Ledger's `asset:ARM-3A` thread
   are three nodes until an identity ground (ADR 0003 §1.2) joins them.
3. **A record whose thread is unknown stays unknown.** When the catalog answers `unknown_record`, the
   record places nothing (`configuration.uncatalogued_record`). When the reader answers no thread
   queries, ADR 0010's stand-in rule applies unchanged (`configuration.unthreaded_id`).

### 3. Gaps a catalog thread cannot fill

1. A catalog thread states no start. A run that states no `first` therefore has no stand-in convention
   (ADR 0008 §2) to fall back on. It is `configuration.untimeable_window`, and nothing about its
   configuration is placed.
2. Envelopes name declared configuration ids. A configuration known only by its anchor is never one of
   them, by construction. So whenever the Ledger holds any envelope, whether one covers such a
   configuration is `configuration.authorisation_undecided`, never `not_covered_by_authorisation`. That
   includes an envelope that places nothing. With no envelope in the Ledger at all, "no envelope covers
   it" is still a fact and is claimed.

### 4. What stays

- The `ledger_thread` stand-in stays. The archetype goldens (`contract/golden.py`) and the unit tests use
  it. A claim cites its stand-in record ids, so removing them would change published golden claim ids.
  Both sources may feed one build. A node declared by either is the same node.
- Identity (`memory.identity`) still keys its nodes from stand-ins. The corpus has no `identity_link` or
  `assertion` record, so there is nothing for it to ground on yet. Feeding it catalog nodes is the step
  that would join `cmms.asset:ARM-3A` to the run's machine once a link or an operator assertion states
  it.
- Calibration reads catalog nodes for its machine (a `cites` membership) and for its configuration
  (anchored). Its `cites` evidence lookups and `calibrated_by` still match only through stand-ins. The
  corpus has one calibration, and it names no machine (MVL-207, then #129).
- Consolidator versions do not change. For a reader without thread answers the output is the same bytes
  as before. Thread answers are new input, not a new rule over old input.

## Alternatives considered

- **Read only catalog memberships, as ADR 0010 §1 literally says.** The Ledger threads no lifecycle
  kind, so no chain could ever form. The demo would keep asking a question that the evidence states and
  the graph cannot answer.
- **Match lifecycle ids to the Ledger's `asset:ARM-3A` thread by value.** That is a silent identity merge
  across namespaces, the exact failure ADR 0003 forbids.
- **Import `neptune_ledger.threads.membership` and compute membership in Memory.** That would use the
  Ledger's internal module rather than its published contract. It also pulls Ledger's storage
  dependencies into Memory, and lets the two drift silently.
- **Keep `as_of` in the export.** Its wall-clock `tx_time` would make the export differ on every
  regeneration, though no claim depends on it.
- **Key anchored configurations by snapshot record id.** Record ids change with a parser upgrade (ADR
  0010, alternatives). The Ledger's anchor-derived thread id does not.

## Consequences

- On the acceptance corpus all 15 maintenance events land on machine chains. There is one `succeeds`
  (AMR-07, firmware 4.2.0 → 4.3.1 at WO-26-0414). The change records become `configuration_unknown`
  spans with `chain_gap`, because their mapped configuration is `NotCovered`. Every run with a stated
  `first` gets `configuration_unknown(run → run record)`. What is still missing is in `docs/contracts.md`.
- If the Ledger ever threads lifecycle kinds (a superseding Ledger ADR 0003 row), §2.2 becomes redundant.
  Node ids and claims do not change, because both sources key `node_ref(type, id)`.
- Memory's snapshot generator now needs `neptune-ledger` and `pgserver`. `make setup` installs both
  (`--all-packages --all-groups`). A Ledger change that alters `threads_of` makes the committed snapshot
  stale, and the snapshot test reports it.
- Revisit when identity reads catalog nodes, or when catalog-api 2.0.0 changes `ThreadKey`.
