# 0072 — Manifest declarations as stated records: run declarations, entities and snapshot pins

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-205
- Amends: ADR 0047 §1 and §9, ADR 0064 §1 and Consequences. 0047 §9: machines and sites become
  records, and a run's machine, site and task become `run_declaration` records (the manifest
  transform is 0.2.0); 0047 §1: runs gain `snapshots`; 0064: a manifest pin is a stated
  `snapshot_binding`, read by the bindings pass as any canonical binding (its "manifest pin"
  alternative is taken)

## Context

The MVL-181 acceptance corpus declares `machine: ARM-3A` (and four other machines, two sites) for
nine runs in its `neptune.yaml`, yet all 13 `Run` records come out with machine `Unknown`. ADR 0047
§9 kept machines, sites, tasks and software in the manifest transform's config and emitted no
record, so nothing a consumer reads says which robot ran. Memory's run consolidator already reads a
`run_declaration` stand-in (`neptune_memory/consolidate/run_records.py`) for exactly this.

Configuration has the same gap. The corpus's bags name no configuration by content id, and its
cell configuration sits outside every recording unit (ADR 0064 §4), so no binding is stated and
the nearest-session rule rightly binds nothing. ADR 0064 deferred manifest pins.

Constraints: a declaration is the user's statement, so `stated`, citing the manifest's bytes
exactly (non-negotiables 2, 4); a recording's own `Run` stays what the recording says (evidence ≠
interpretation); identities are never merged; one canonical record has one record-level citation
(ADR 0017 §5); ADR 0050's `SnapshotBinding` names one run and one snapshot and is frozen.

## Decision

1. **Machines and sites are records** (amends ADR 0047 §9). Each `machines` entry is a `Machine`
   and each `sites` entry a `Site`, `stated`, citing the entry (`/machines/N`) under the manifest
   transform. `identifiers` are `("manifest", <id>)`, citing `/machines/N/id`, and every alias as
   written (`(namespace, value)`, citing its exact pointer, `/aliases/<ns>` or `/aliases/<ns>/<k>`).
   A machine's `manufacturer` and `model`, and a site's `parent` and `location`, are `NotCovered`:
   manifest version 1 has no place for them. A site's `name` is `Known` (citing `/sites/N/name`)
   or `Unknown`. A machine's `name`, `description` and `embodiment`, and every `tasks` and
   `software` entry, stay in the transform's config only: no field or kind holds them yet (ADR
   0063's task kinds are documents and work, not a task identity). Nothing is merged: the asset
   register's `ARM-3A` and the manifest's are two records, linked only by ids they share.
2. **`run_declaration`, a new kind** (`model/run.py`, family `run`, `since` 9):
   `RunDeclaration {id, provenance, run, logical_id, machine, site, task}`. `run` is the `Run`
   record the declaration is about (a declaration about no run in the package is a finding, §5);
   `logical_id` is the id the declaration gives the run (`("manifest", <name>)`, citing
   `/runs/N/name`); `machine`, `site`, `task` are `Knowledge[LogicalId]` in the `manifest`
   namespace, each citing `/runs/N/<field>`, `Unknown` where the entry has no such key. It is a
   record beside the `Run`, never an edit of it: `Run.machine` is what the recording states.
3. **Which runs an entry declares, and one record per run.** An entry covers the files its
   `paths` name (a file, or a directory and everything below it). These paths are the
   declaration's own values: a manifest is a file in the root it describes, its paths are relative
   to that root by its schema (ADR 0047 §3), so this is not the job's view that ADR 0064 §2 refuses.
   Every `Run` record the bytes of a covered file declare gets one `run_declaration` (identical
   bytes elsewhere are the same run record, ADR 0064 §7). One entry may cover several recordings
   (a rosbag2 directory's metadata and storage are two run records), so the record's evidence is
   the entry made finer by an adapter step naming the run (ADR 0017 §5):
   `[{json_pointer: /runs/N}, {kind: "neptune.manifest:run", run: <run id>}]`.
4. **Snapshot pins** (amends ADR 0047 §1; ADR 0064). A run entry may list `snapshots`, each
   `{path: <root-relative file>}` or `{content: "sha256:<hex>"}`; optional, so every version-1
   manifest stays valid, and an older Neptune refuses the key rather than ignore it. A path
   resolves to the file at exactly that path in this scan; a content id to any file holding those
   bytes. Every snapshot record of those bytes (`configuration_snapshot`, `software_configuration`,
   `hardware_configuration`, `calibration`) is bound to every run record the entry covers by a
   canonical `snapshot_binding`, `stated`, evidence `[{json_pointer: /runs/N/snapshots/K},
   {kind: "neptune.manifest:binding", run, snapshot}]`, validity `Unknown` (the manifest says
   which, not when; ADR 0064 §8). The bindings pass (ADR 0064 §1) reads these as it reads any
   canonical binding: the kind is bound, so no `snapshot_unresolved` for it, and its transform's
   upstream gains the manifest transform. Nearest-session inference is unchanged, so a pinned run
   may also hold an inferred binding of the same slot; consumers prefer the stated one.
5. **What cannot be applied is a finding** of the manifest transform, warning, citing the
   declaration by pointer:

   | Code | Category | When |
   |---|---|---|
   | `neptune.manifest.run_unrecorded` | missing | the entry's paths hold files, none of which declares a run (paths holding nothing are grouping's `declaration_unmatched`) |
   | `neptune.manifest.run_declared_twice` | ambiguous | two entries cover one run record; both declarations stand, none is chosen |
   | `neptune.manifest.machine_contradicts_run` | inconsistent | the run states its machine in a namespace the declared machine has ids in (an alias), and none is that id; both stand |
   | `neptune.manifest.pin_unresolved` | missing | no file the job read is at the pinned path (missing, a directory, a symlink the walk does not follow) or holds the pinned content |
   | `neptune.manifest.pin_not_a_snapshot` | missing | the pinned bytes hold no snapshot record (another kind of file, or one its adapter could not read: that adapter's finding says why) |

   What ADR 0047 §1–§2 refuses stays refused whole, before anything is read (exit 6): an id a run
   names that no entry declares, a run name or a pin given twice, an absolute or `..` pin path, a
   malformed content id. A typo applied anyway would become a stated fact.
6. **Lineage and versions.** The manifest transform is `neptune.manifest` 0.2.0 (its output
   grew; 0.1.0 packages are not rewritten). Its records are made at assembly, after run assembly
   and before snapshot binding, from records only: no source byte is read and no adapter called.
   A package holding a `run_declaration` is schema version 9 and package-schema 9.0.0 (ADR 0037
   §1); packages made without a manifest keep their bytes, and those whose manifest declares no
   machine, site or run change only by the transform's version. Validation's `dangling_reference`
   checks that `run_declaration.run` names a `run`.
7. **Cost and determinism.** One pass over the layout per entry (covered files), dictionary
   lookups for runs and snapshots by content: O(entries × files) at worst, records sorted by kind
   and id, findings by id; the same inputs give the same records in any order.

## Alternatives considered

- **Fill `Run.machine` from the manifest.** It rewrites what a recording states with what a user
  states, under the recording's adapter; the run's own evidence would no longer be its own.
- **A manifest `Run` per entry, with a `RunAssembly` of the files it covers.** A second run node
  per recording that consumers must join to the recording's, and one with no interval: Memory
  places run claims over `[first, last]`, which a manifest entry does not state.
- **One `run_declaration` per entry listing its runs.** Memory's consolidator, like
  `SnapshotBinding`, names one run per record; the finer step gives one rule for both.
- **Undeclared ids and duplicate runs as findings, applied anyway.** A misspelt machine would be
  stated as a new machine with a warning beside it; refusing it with its line and pointer before
  any work costs one edit.
- **Pins settle their slot** (as a run-named binding does in ADR 0064 §4). It changes
  `neptune.bindings` 0.1.0 for every canonical binding an adapter emits; revisit with that pass's
  next version if stated-plus-inferred pairs confuse consumers.
- **Pins by snapshot record id, or directory pins.** Record ids change with adapter versions and
  users cannot write them; a directory pin binds whatever is below it, which no one stated.
- **Edit the MVL-181 corpus to pin its configurations.** The corpus is Platform's, locked, and its
  cell configuration is a deliberate stale trap (gold T2): pinning it would state what the gold
  calls wrong. The end-to-end test pins the legged and AMR configurations on a copy instead.

## Consequences

- On the MVL-181 corpus all 13 runs have a stated `run_declaration` naming their declared
  machine and site; five `machine` and two `site` records cite the manifest.
- **Memory** (its `run_declaration` stand-in, Memory ADR 0009 §1) reads the compiler's shape with
  `run_declaration_from_json`. The delta: the record carries the envelope (`schema_version`) and
  `provenance` (its `evidence` is `(provenance.evidence,)`, the entry with the run step); `run` is
  the `Run` record's id, so the run node is that run's own (its declared logical id, else
  `record:<id>`); `logical_id` is new (the user's run name). `machine`, `site` and `task` keep
  their shape. Pins need no Memory change: they are ordinary canonical `snapshot_binding`s, which
  its configuration consolidator already reads.
- The Ledger indexes the new kind from package-schema 9.0.0 (projection regenerated, no new hot
  column); Deploy and Ledger pins move to 9.0.0.
- Revisit when manifest version 2 adds machine fields (embodiment, model) or a task identity
  kind exists, when a source other than the manifest declares runs this way (a run sheet), or when
  pins need validity windows (a manifest `start`/`end`).
