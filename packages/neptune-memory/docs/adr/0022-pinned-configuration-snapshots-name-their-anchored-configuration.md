# 0022 — Pinned configuration snapshots name their anchored configuration

- Status: Accepted (transitional, until the Ledger threads `configuration_snapshot`)
- Date: 2026-10-06
- Issue: MVL-191

## Context

A run sheet pins the files a run was launched with (root ADR 0072). The compiler binds each pin with a stated
`SnapshotBinding` to whatever record it read from the file. For a parameter document that record is a
`configuration_snapshot` (root ADR 0037). Platform's corpus 2.1.0 states 12 such bindings: 2 to a `calibration`
and 10 to a `configuration_snapshot`.

ADR 0010 §1 names a bound snapshot's configuration by the Ledger thread anchored on the snapshot's record-level
evidence. ADR 0018 §2 reads that thread from the catalog. The Ledger's thread table (Ledger ADR 0003 §2) opens an
anchored `configuration` thread for `HardwareConfiguration`, `SoftwareConfiguration` and `Calibration`, but has no
row for `configuration_snapshot`. The catalog answers `found` with no memberships for every snapshot. So all 10
pins became `configuration.unthreaded_id` and `configuration_unknown(run → binding)`. The last good arm-cell run
had no configuration, and "what changed since the last good run" could not come from claims.

## Decision

1. **The node.** A bound `configuration_snapshot` that no thread answers (no stand-in cites its anchor, and the
   catalog says it opens none) names `thread:<thread id>`. The thread id is that of the anchored `configuration`
   thread its record-level evidence keys, by the Ledger's published rule (Ledger ADR 0003 §1.3):
   `"sha256:" + hex(sha256(canonical JSON of {"key": anchor, "kind": "configuration"}))`
   (`threads.anchored_node`). This is the node ADR 0018 §2.1 gives a catalog-answered anchored configuration
   thread. If the Ledger ever adds the row, every node and claim stays the same. Memory computes the id from the
   published rule and imports no Ledger code.
2. **Only what the Ledger holds.** When the catalog answers `unknown_record` for the snapshot, nothing is
   placed. The finding is `configuration.uncatalogued_record`, citing the binding and the snapshot, and the window
   stays `configuration_unknown(run → binding)`. A binding naming a snapshot the Ledger does not hold, or holds as
   another kind, is still `configuration.dangling_binding` (ADR 0010 §3). Nothing falls back to another snapshot or
   to the nearest configuration in time.
3. **Everything else is ADR 0010 / 0019.** The claim is `configuration_active_during(run → configuration)` over
   the bound window, with the binding's `assertion_kind`, citing the binding, run and snapshot. Two bindings of one
   kind that name different configurations over overlapping windows are still `configuration.binding_overlap` and
   one `configuration_candidate` each. That includes two parameter documents pinned to one run with unstated
   windows. Like a catalog anchor-only node, this node is never an id an envelope names, so where the Ledger
   holds envelopes its coverage is `authorisation_undecided` (ADR 0018 §3.2). Machine chains are unchanged.
4. `memory.configuration` is version **3**, a new lineage (ADR 0003 §3).

## Alternatives considered

- **Key the node by the snapshot's value digest.** The digest (root ADR 0037) makes two value-equal documents
  one configuration whatever their bytes. That is an identity join no record states, and anchors key every other
  configuration. Comparing digests across runs is a question for a reader, not a node identity.
- **Key the node by the snapshot's record id.** Record ids change with a parser upgrade (ADR 0010 and 0018,
  alternatives). The anchor does not.
- **A Memory-only namespace (`snapshot:<anchor hash>`).** A future Ledger row for snapshots would then give a
  second node for the same evidence and break lineage. The `thread:` id is the one that row would give.
- **Wait for a Ledger ADR adding the row.** The demo question stays unanswerable until then, and the node would
  be the same anyway.

## Consequences

- On corpus 2.1.0, 9 of the 10 runs pinned to a parameter document get `configuration_active_during`. The 10th
  states no first instant (`configuration.untimeable_window`). With the 2 calibration pins, that makes 11 claims
  over 9 runs, up from 2. Runs pinning one file share its configuration node, so a later run on other bytes reads
  as a different configuration.
- Configurations are keyed by evidence: two byte-identical files are one configuration, and a comment-only edit is
  another. Whether two configurations hold equal values is the digest's question and is not answered here.
- **Transitional.** Computing the thread id in Memory is correct per Ledger ADR 0003 §1.3. The proper fix is
  the Ledger threading `configuration_snapshot`: a superseding Ledger ADR 0003 §2 row that makes a
  `ConfigurationSnapshot` the `subject` of an anchored `configuration` thread. Once that release is pinned and the
  acceptance snapshot is regenerated, the catalog answers the same `thread:` node through ADR 0018 §2.1, and
  this path is deleted with no change to node ids or claims (a version bump only if the claims change):
  - in `consolidate/configuration.py`, the `CONFIGURATION_SNAPSHOT` branch of `_snapshot_configurations`, the
    `_View.unthreaded_anchors` field, and its union into `anchor_only` in `_coverage`;
  - `anchored_node` in `consolidate/threads.py`, and the `CONFIGURATION_SNAPSHOT` constant in
    `consolidate/configuration_records.py`.
  `test_the_node_is_the_one_a_ledger_threading_snapshots_would_answer` already checks that the threaded catalog
  gives the same claims. Keep it as the regression test after the deletion.
- Revisit if the Ledger changes its thread-id rule (catalog-api 2.0.0).
