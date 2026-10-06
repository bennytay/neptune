# 0019 — Events in identity, and configuration changes scoped to a machine

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191 (follow-ups recorded on MVL-137)
- Supersedes: ADR 0008 §3's last sentence ("Record ids in a scope name evidence, not things, and are not read") and
  its §2 rule that a scope's ids are logical ids; ADR 0010 §2's `succeeds` "at each decided-to-decided change" and
  §6's reading of `succeeds`

## Context

Two Demo v1 answers rest on claims that are wrong today.

**Q7: "do the CMMS and the controller log agree?"** One protective stop of INC-C3-0011 is stated twice: syslog line
4182 (an event-table row) and CMMS downtime entry DT-26-0914-01. Deploy's `cmms.downtime` preset maps the downtime
row to an `intervention` that declares `cmms.downtime:DT-26-0914-01`. `memory.events` keys each statement by its
record (`record:<rec id>`, ADR 0013) and never relates two of them. A. Novak's `same_identity` assertion is the
stated ground for joining them. Identity cannot read it: its nodes come only from Ledger threads, which hold no
events, and it skips record ids in a scope. The assertion ends as an `assertion_scope` or `dangling_link` finding.

**Q2: "what changed?"** Configuration nodes are threads keyed by declared id, so they are shared across machines
(ADR 0010 §1). AMR-05, -06 and -07 all name `firmware:4.2.0`. `succeeds(firmware:4.3.1 → firmware:4.2.0)`, cited
to AMR-07's work orders, therefore reads as a fleet-wide upgrade. Deploy's packs follow `has_configuration` from
AMR-05 to `firmware:4.2.0` and pick up that claim as a change on AMR-05.

## Decision

### 1. Identity joins event nodes an assertion names

1. **Order.** The identity consolidator is registered with `after=("memory.events",)` and reads only that
   consolidator's claims from `previous`. There is no cycle:
   - Events reads no claims (its `previous` is unused) and depends on no consolidator.
   - Configuration and calibration call `identity.node_threads`. That is a function of the Ledger, unchanged here,
     not of identity's claims.
   - Nothing that events reads reads identity.
   Events must never read identity's claims: an event joined by `same_as` is still two statements (ADR 0013). If a
   consolidator ever needs both, it runs after both.
2. **Event nodes.** Each `evidenced_by(record:<r> → r)` claim of `memory.events` adds the event node `record:<r>` to
   identity's node view. A timeline entry (`record:<r>/timeline/<i>`) has no record or id of its own and is never
   in a scope. Where a Ledger thread already holds the key, the thread's node is kept. An event node's
   conventional start (ADR 0008 §2, used when an assertion states no `authored_at`) is its own placement on its
   record's clock. That is the `evidenced_by` claim citing the fewest records, since a projection adds the
   mapping. The claim lists those records.
3. **Scope entries.** A logical id names its Ledger thread's node. Failing that, it names the events whose
   `incident_record` or `intervention` declares it in `identifiers`, read with the compiler's strict readers.
   - One record that certainly declares it gives that node.
   - Several records, or an `Ambiguous` item or list that only possibly declares it, give every such node, and
     the entry is uncertain.
   - If neither a thread nor an event names it, it stays as written and is reported as `identity.dangling_link`.

   A record id names its event node if `memory.events` keyed one by it. Otherwise it names evidence and is not
   read. Two entries that name the same nodes (a record and the id it declares) are one entry. Ids are matched
   verbatim: `plant-2.cmms.downtime` and `cmms.downtime` are different namespaces, never normalised.
4. **Claims.**
   - When every entry is certain, the statement is ADR 0008's: `same_as` joins each entry to the lowest, is
     `stated`, cites the assertion, and lists the event records it joins in `provenance.records`.
   - When an entry is uncertain, the result is `identity.scope_ambiguous`. Each node one entry may name gets a
     `same_as_candidate` pair (one claim each way) with each node another entry may name, and there is never a
     `same_as`. A `distinct_identity` suppresses candidates only between its certain entries.
   - Retraction, `Ambiguous` identifier, `retracts` and `authored_at`, contest and type rules are ADR 0008's,
     unchanged. An event is never joined to a machine (`identity.type_mismatch`).
   - Fewer than two entries is `identity.assertion_scope`, whose `unread_records` counts the scope's record ids
     that name no event.
5. Identity is version **3**: same Ledger, new rule, new lineage (ADR 0003 §3).

### 2. A configuration change is the machine's own spans

Configuration lineage is version **2** and claims no `succeeds` from a machine's chain. Its `has_configuration`
spans are already the per-machine transition record. A decided span ends exactly where the chain's next span
begins (ADR 0010 §2), so a change on machine `m` at `t` is two `memory.configuration` claims about `m`:
`has_configuration(m → A)` ending at `t` and `has_configuration(m → B)` starting at `t`, with `A ≠ B` and one
`Timestamp` (so one clock). An unknown or candidate span between them breaks the adjacency, so nothing is read across a
gap, a tie or two clocks, as before. `consolidate.configuration.transitions(claims, machine)` reads this back for
Memory's own callers. Graph-schema rule 12 states the same rule, for consumers that read the contract.

`succeeds` stays in the vocabulary, narrowed to a statement about two configurations themselves (a release note
saying 4.3.1 replaces 4.2.0, or compiler configuration lineage once MVL-38 lands). It holds wherever both nodes
appear. No consolidator claims it today. Its description changes, so the vocabulary goes to **11**.

### 3. Contract: graph-schema 2.0.0, a major release

`GRAPH_SCHEMA_VERSION = 2`. Every 1.x golden still validates against its own schema, but `succeeds` changes
meaning. Consumers' change sections (Deploy's @1 templates, Context's what-changed view) select it, and against a
2.x graph they would silently fall empty. ADR 0006 §2 calls narrowing a predicate a major change, and a meaning
change is one even when no shape changes.

- **Release in the document.** A graph document names the full release it was written to: `graph_schema`
  (`"2.0.0"`, `schema.GRAPH_SCHEMA_RELEASE`), required in 2.x beside `graph_schema_version`. A consumer can then
  tell minors apart from the file alone. Any minor of major 2 reads. A later minor raises the constant with its
  goldens.
- **1.x documents.** `graph_from_json` still reads a 1.x document as written. It is labelled major 1 (`release`
  `None`; a reader reports `graph_schema_version` 1) and is written back unchanged, never relabelled 2.x. In-repo
  consumers on 1.x keep working until they move. Any other major is refused.
- **Migration.** `graph-schema.md` § Migrating from 1.x to 2.0.0. Consumers' locks are raised by their own
  coordinators. Until then they are a major behind, and `contracts.py check` says so.
- **Not in 2.0.0.** The claim-id derivation vectors (MVL-137) come later in an additive 2.x.

The golden graph changes only by vocabulary 11 (a new resolver generation), identity's version (re-ided
`same_as` claims) and `graph_schema`.

## Alternatives considered

- **Machine-configuration pair as `succeeds`' subject.** Needs a new node type and a different subject type for an
  existing predicate (a breaking change). Deploy's `has_configuration` paths would no longer reach it.
- **A machine qualifier on the claim.** Claims have no qualifiers. Adding one changes the claim model, its id and
  the codec for every consumer, to say what two existing claims already say.
- **Publish the narrowing as a 1.x minor.** Every golden still validates, but a consumer pinned to 1.x would get
  an empty change section with no signal. A major tells it.
- **Keep `succeeds` and document it as fleet-wide.** The claim cites one machine's work orders and is false for the
  others. Documentation would not stop a pack placing it on AMR-05.
- **Map a record id in a scope to every claim citing that record.** That would join a run, an episode and an event
  that share a record. Only `memory.events` keys a node by a record, so only its nodes are read.
- **Read an Ambiguous scope mapping as its first or only certain candidate.** That would pick a winner the
  evidence does not.
- **Re-parse event tables in identity to find a row's declared id.** That duplicates the event config. A row has no
  declared id. To name it, an assertion uses its record id, or a future event-table id column has `memory.events`
  carry it.

## Consequences

- Q7 joins the two stops once both scope entries name events. Corpus 2.0.0's assertion names the CMMS stop as
  `plant-2.cmms.downtime:DT-26-0914-01` but the preset declares `cmms.downtime:DT-26-0914-01`. It names the syslog
  line as `plant-2.syslog.log-p2:4182`, which nothing declares. Both entries stay dangling until the corpus names
  the ids the records declare (or the syslog row's record id), or an event-table id column lands.
- Deploy's `configuration-traceability@1`, `configuration-lineage@1` and `incident-timeline@1` select `succeeds` for
  their change sections. Against a v2 configuration build those sections are empty (`NotCovered`), never wrong. A
  new template version reads rule 12's adjacency instead. Context's what-changed rendering does the same.
- The acceptance snapshot changes: identity and configuration versions, `after`, and the joined stop once the corpus
  names declared ids. The coordinator regenerates it.
- Revisit if the compiler gives event-table rows declared ids or a stop kind (Deploy ADR 0016 §7), or if the claim
  model ever gains qualifiers.
