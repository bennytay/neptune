# 0021 — A machine record's declared ids are same_as

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191

## Context

Package-schema 9 (root ADR 0072 §1) emits a `stated` `Machine` record for each manifest machine:
its `identifiers` are `("manifest", <id>)` and every alias as written (`cmms.asset:ARM-3A`,
`servicenow.ci:…`, `serial:…`, a flight log's `px4.sys_uuid:…`), each citing its pointer. One
declaration giving one machine several ids is stated evidence that they name one thing. Identity
(ADR 0008) read only `identity_link`, assertions and lineage, and the compiler emits no
`identity_link` for these, so a CMMS work order on `cmms.asset:ARM-3A` and the manifest's runs on
`manifest:ARM-3A` never met, and a log's own `sys_uuid` stayed a second machine (ADR 0020).

## Decision

1. **Ground.** The identity consolidator reads `machine` with the compiler's `machine_from_json`
   (an `inferred` one is ignored: a `derived/` record). A record's `Known` ids are joined to its
   lowest id in canonical order (ADR 0008 §2's rule for a scope) by `same_as`, with the record's
   `assertion_kind` (`stated` for a manifest, `observed` for a log), citing the record's
   provenance, the hub's and each side's own citation. Each `Ambiguous` identifier gives
   `same_as_candidate` between the hub and each of its candidates, both ways, never `same_as`; a
   record with no `Known` id is `identity.machine_undecided`. `Unknown` ids never reach a record.
   Equal strings in different namespaces are never joined without a record declaring both.
2. **Nodes and time.** A declared id is a `machine` node even with no Ledger thread; an id a
   thread keys as another node type is `identity.type_mismatch` and is left out. A record states
   no time, so its links hold, open-ended, from the machine's first placement: the first thread
   record of any of its ids (ADR 0008 §2), else the first `Run` (by record id) that names one, by
   `Run.machine` or by a `run_declaration`. The claim cites that run (and declaration). Neither:
   `identity.machine_unplaced` (info) and no claim until one arrives. A convention, not a lifetime.
3. **Conflicts are never merges.** Records joined through shared ids form a group. The group is in
   conflict when two of its records cite one document (a manifest lists them as two machines), or
   when the join gives one namespace two ids no single record declares together (`manifest:ARM-3A`
   and `manifest:ARM-9` through one `cmms.asset` alias). Every record in a conflicting group gives
   candidates instead of `same_as`, and one `identity.machine_conflict` names the records and ids.
   A person resolves it with a `same_identity` or `distinct_identity` assertion (ADR 0008 §3).
4. **What Memory cannot key.** An id that is blank or padded (ADR 0006 §9), or in the `record`
   namespace (Memory's record-keyed run, stream and event nodes), is
   `identity.machine_identifier_unrepresentable`; the record's other ids still count. An alias the
   compiler cannot represent never reaches the record: its `alias_namespace_unrepresentable`
   finding is the compiler's.
5. **Withdrawal** is as for every stated link: an edited manifest is a new record, and a build
   without the old one withdraws its claims (ADR 0016); a `distinct_identity` across a joined pair
   is `identity.contested`, never a dropped claim.
6. The identity consolidator's version becomes `3`: its claims are a new lineage.

## Alternatives considered

- **Have the compiler emit `co_declared` `identity_link`s.** Root ADR 0050 allows it, but no
  adapter does, and a package-schema change is the compiler's; reading the record Memory already
  receives needs no contract change. Revisit if the compiler starts emitting them: both grounds
  would then cite one record twice.
- **Join every pair, or join to the `manifest` id.** All pairs is n² claims for one fact; a
  preferred namespace would make one source special. The lowest-id star gives the same closure.
- **Emit `same_as` and a `contested` finding on a conflict, as for a person's distinctness.** A
  conflict here is between two declarations of equal standing, not a person overruling evidence,
  and a closure through it would silently treat two listed machines as one.

## Consequences

- Manifest runs, CMMS work orders and ServiceNow incidents on one machine meet in
  `same_as_closure`; a log's own id and the manifest id stop being two machines.
- Identity now also reads `run` and `run_declaration`, only to time machine links.
- The graph-schema golden and the acceptance snapshot carry identity version `3` once regenerated.
- Revisit when Memory reads thread membership from the catalog (ADR 0018), which may place a
  machine's ids directly.
