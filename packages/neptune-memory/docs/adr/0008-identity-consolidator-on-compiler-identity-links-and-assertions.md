# 0008 — The identity consolidator on compiler identity links and assertions

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-126
- Amends: ADR 0003 §1 (the consumed record shapes: `identity_link` and `operator_assertion` give way to the
  compiler's kinds); ADR 0007 §5.4 (the retraction record is the compiler's `assertion` of type `retract`, not
  an `operator_retraction`); ADR 0007 §2 is carried out (`has_name`)

## Context

ADR 0003 fixed the identity policy against record shapes Memory wrote ahead of the compiler. The compiler has
since frozen its own: `IdentityLink` (root ADR 0050, package-schema 3) and the human `Assertion` (root ADR 0062,
package-schema 5). They differ from ADR 0003's stand-ins in ways the policy has to absorb:

- `IdentityLink.right` and `identifier` are `Knowledge`: `Known`, or `Ambiguous` with every id the evidence
  could mean. A `co_declared` link has no `evidence` of its own; its citation is `provenance`.
- Validity is a window `[start, end)` on a named clock, often `NotCovered` (a register states no dates).
- A person's assertion has a `scope` (record ids and logical ids), an `authored_at` on a clock of its own, and
  is withdrawn by a later `retract` that names its *declared identifier*, never its record id.

The G1 gate (ADR 0007) left MVL-126 the retraction record and `has_name`. The Ledger's thread kinds include
`stream` and `document`, which Memory's node types lack. Consumers need a sanctioned way to follow identity
without a merge. Getting any of this wrong either merges two robots built from one URDF or loses a declared
identity because a field was not `Known`.

## Decision

### 1. What identity reads, and how it is parsed

Parsing (`consolidate/identity_records.py`) is separate from the policy (`consolidate/identity.py`). Compiler
kinds are read with the compiler's own strict readers (`identity_link_from_json`, `assertion_from_json`,
`timestamp_domain_from_json`); a record they refuse, or with a blank or padded id (ADR 0006 §9), is an
`identity.malformed_record` finding. An `identity_link` with inferred provenance (a `derived/` record) is an
`identity.inferred_link` finding and never a ground. `ledger_thread` and `configuration_lineage` stay ADR 0003's
stand-ins until Memory reads the catalog API (MVL-85) and the compiler emits lineage (MVL-38). The consolidator
is version `2`: its claims are a new lineage (ADR 0003 §3).

### 2. `same_as` and `same_as_candidate`

- **Declared-identifier equality** is honoured twice and re-derived by Memory never: equal logical ids in any
  number of packages are one thread, so one node (ADR 0003 §1.1), and equal identifiers under different keys
  arrive as the compiler's `IdentityLink` (`shared_identifier`, or `co_declared` for one declaration naming
  both). The compiler owns that join (root ADR 0050 §2).
- **`same_as`** from an `IdentityLink` whose `right` is `Known` and whose `identifier` is not `Ambiguous`
  (`assertion_kind` as the link's provenance states), from configuration lineage (`observed`), and from a
  `same_identity` assertion that stands (§3; `stated`). Evidence: the link's provenance, its `evidence`, and the
  right side's own citation. A scope of n ids joins each to the lowest (canonical JSON order).
- **`same_as_candidate`**, one claim each way, for every candidate of a link whose `right` or `identifier` is
  `Ambiguous`, each citing the link and that candidate's own place; plus ADR 0003 §1.3's shared-evidence pairs.
  Candidates are never collapsed, and never emitted between nodes already joined by `same_as` or asserted
  distinct. Like `same_as`, a candidate pair is emitted once per ground, so two grounds give two pairs, each
  with its own evidence.
- **Valid time.** A link's window where its start is `Known`; an end that is not `Known` is `OPEN`, valid until
  further notice, as for a run whose last instant is not stated. An assertion holds from its `authored_at`. A statement
  that states no start holds from its subject's first thread record's `valid_from` (by record id), open-ended:
  a convention, not a lifetime (ADR 0007 §3). A stated end its start cannot be placed before is
  `identity.untimeable_window` and no claim. An instant on a `TimestampDomain` that declares a civil
  timescale, an absolute epoch and its resolution is placed on that `CivilClock` (ADR 0002 §3); otherwise it
  stays on its own clock. Clock records are admitted like every other record: one id with two definitions is
  `identity.record_conflict` and places nothing.

### 3. Assertions and retraction

An assertion of type `retract` names another by its declared `identifier`, and withdraws every assertion that
carries it, whenever authored: authored times are on clocks of their own and are not compared, so a re-issued
assertion takes a new id. An assertion is **retracted** when an effective `retract` names its identifier, and
**effective** when every `retract` naming it is retracted (or none does), so a retraction of a retraction
restores it with its original claim id. What neither settles, a loop (a `retract` naming its own id, or
retractions naming each other) and whatever rests on one, is **undecided**: no claim,
`identity.retraction_undecided`. This is the grounded labelling, computed with a worklist, so a chain of any
length resolves. A
`retract` naming an id no assertion carries is `identity.retraction_unmatched` and retracts nothing until one
arrives. The build at the retraction's transaction emits no claim resting on it; ADR 0007 §5's build
withdrawal (MVL-132) then supersedes the earlier claim at that transaction, so its interval closes in
transaction time and `as_of` before it still shows it. Nothing is deleted or edited.

`distinct_identity` suppresses candidates between its ids. Where `same_as` joins two ids a standing
`distinct_identity` declares distinct, directly or through a chain, every `same_as` is still emitted and
`identity.contested` names the declaration and any direct ground: evidence is never dropped for a contrary
statement. An assertion whose type is not `Known` is
`identity.assertion_unread`; a `same_identity` or `distinct_identity` with fewer than two logical ids in a
`Known` scope is `identity.assertion_scope`. Record ids in a scope name evidence, not things, and are not read.

### 4. No anchoring claim

A node is materialised by the first claim that names it (ADR 0002 §1). `nodes()` and `node_ref()` are the keying
function every consolidator uses; identity emits identity claims only, never a "this thread exists" claim,
which would duplicate the Ledger's catalog.

### 5. Traversal

`schema.traverse.same_as_closure(reader, node, as_of, *, depth=8, include_candidates=False,
include_inferred=True)` walks `same_as` in both directions over any `MemoryReader`, returning each node once at
its shortest depth with one shortest path of claims (`Neighbour`). It follows `same_as_candidate` only when
asked, and filters nothing by valid time (clocks are never compared).

### 6. Vocabulary and contract

`has_name` joins `CORE_PREDICATES` in ADR 0007 §2's shape; node types gain `stream` and `document` (the
catalog's thread kinds), which widens `evidenced_by`, `same_as` and `same_as_candidate` (each to version 2).
`VOCABULARY_VERSION = 3`, so the resolver generation changes (ADR 0006 §7). graph-schema **1.1.0** is a minor
release: every 1.0.0 golden validates and its graph still loads and passes the suite. The golden graph gains the
fleet register worked example (root ADR 0050 §10), a new example so the four platforms' package-schema and
catalog goldens are unchanged.

## Alternatives considered

- **Re-derive identifier equality in Memory** from threads' declared ids: duplicates the compiler's join, gives
  one fact two grounds, and needs Memory to read raw identifiers.
- **Refuse a statement with no stated start**: a register row states no dates, so every register identity would
  be lost to a missing field; the subject's thread start is the earliest instant the graph already uses for it.
- **Retract by record id** (ADR 0007 §5.4's `operator_retraction`): record ids change with a parser upgrade
  (root ADR 0062 §5), so the retraction would silently stop naming its target.
- **Treat a retraction loop as retracted** (or as effective): either picks a winner the evidence does not.
- **Drop `same_as` when a `distinct_identity` contradicts it**: lets a later statement silently erase evidence;
  the contest is reported instead and resolved by a retraction.
- **An anchoring `evidenced_by` per thread**: every node would be named, but by a claim that asserts nothing
  beyond the catalog and depends on the stand-in thread record.
- **Change the drone worked example** (as root ADR 0050 §10 first suggested): it would rewrite published
  package-schema and catalog-api goldens of other packages; a fleet register example states the same link.

## Consequences

- Memory reads only compiler-shaped identity records; ADR 0003's `identity_link` and `operator_assertion`
  stand-ins are gone, and tests build records with the compiler's types.
- Retraction is complete on Memory's side; the strict `xfail` in `test_g1_stress_retraction.py` flips when
  MVL-132 lands build withdrawal.
- Consumers move to graph-schema 1.1.0 at their pace (a minor lag only warns); a store must start a new
  generation for vocabulary 3.
- Revisit when Memory adopts the catalog API (threads become `ThreadKey`s and their memberships), when the
  compiler emits configuration lineage, or if identity needs a time per candidate.
