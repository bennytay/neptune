# 0024 — Snapshot configurations come from the Ledger's threads

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191

## Context

ADR 0022 computed the anchored configuration node of a pinned `configuration_snapshot`, because the Ledger did not
thread it. Ledger ADR 0017 (#157) now threads every snapshot on its own evidence, with byte-identical thread ids.

## Decision

Memory reads a snapshot's configuration node only from its thread, as for every other snapshot kind (ADR 0010 §1,
ADR 0018 §2). ADR 0022's computed node, and its `uncatalogued_record` finding for snapshots, are removed. A
snapshot with no thread is `configuration.unthreaded_id`, the same as any other kind. `memory.configuration` stays
version 3. Every catalog a current Ledger serves answers the same node, so the same Ledger snapshot gives the same
claims (the acceptance snapshot is byte-identical). An export made before Ledger ADR 0017 must be made again,
just as Ledger migration 0012 rebuilds such a catalog.

## Alternatives considered

- **Keep the computed fallback.** That would be two sources for one node, and the Ledger is now the owner.
- **Bump to version 4.** No claim changes for any input a current Ledger produces. A bump would only re-key claims.

## Consequences

Supersedes ADR 0022. Ledger thread membership is again the only source of configuration nodes.

ADR 0018 §2.3's `configuration.uncatalogued_record` is for records that declare their own ids (lifecycle kinds).
A bound snapshot is found by its anchored thread, so a snapshot the catalog answers `unknown_record` for has no
thread and is `configuration.unthreaded_id`, as before ADR 0022.
