# The Neptune manifest

Status: MVL-14, ADR 0047; declarations as records MVL-205, ADR 0072. Optional. Most folders need
none: `neptune ingest` discovers, probes and groups on its own. A manifest is for what discovery
cannot settle: two adapters that claim a file equally, session readings grouping cannot choose
between, and what no file says (which robot, site, task or software a run involved).

## Start from the folder

```console
$ neptune init-manifest runs/2026-09-30          # writes runs/2026-09-30/neptune.yaml
$ $EDITOR runs/2026-09-30/neptune.yaml            # uncomment what is true
$ neptune ingest runs/2026-09-30 --out packages/2026-09-30
```

The generated file declares nothing until you uncomment a line. It lists, as comments: each file
whose adapters tie (one line per adapter, keep one), each contested set of session readings (keep
the true one), the uncontested proposals, the probe's winners (uncomment to pin) and templates for
machines, sites, tasks and software. `-o -` prints it instead; an existing file is never
overwritten without `--force`.

## Example

```yaml
neptune: 1                                   # schema version, required

machines:
  - id: ur5e-cell-3
    embodiment: manipulator                  # manipulator, mobile_base, legged, humanoid, aerial, marine, ...
    aliases: {serial: "20235400123", ros_namespace: /ur_left}
  - {id: anymal-c-03, embodiment: legged}
sites:
  - {id: lab-a, name: "Lab A"}
tasks:
  - {id: pick-place, description: "Pick parts from the tray"}
software:
  - {id: driver, name: ur_robot_driver, version: "2.10.1"}

runs:                                        # declared sessions
  - name: pick
    paths: [arm/pick_1.mcap, arm/pick_2.mcap]  # files, or directories meaning all below
    machine: ur5e-cell-3
    site: lab-a
    task: pick-place
    software: [driver]
    snapshots:                               # what the run ran with: a file, or bytes by content id
      - {path: arm/config/controller.yaml}
      - {content: "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"}
  - {name: trot, paths: [quadruped/], machine: anymal-c-03}

sources:                                     # which adapter reads which files
  - {path: arm/joint_notes.txt, adapter: markdown}
  - glob: "logs/**/*.csv"                    # ignore-rule glob syntax, anchored at the root
    adapter: tabular
    options: {csv_delimiter: ";"}

adapters:                                    # options for every file an adapter reads
  tabular: {options: {csv_delimiter: ","}}

grouping: {gap_seconds: 30}
```

`neptune.json` takes the same document as JSON. Editors can validate against
[`schema/manifest.schema.json`](schema/manifest.schema.json); `neptune` checks more (references
between entries, repeated ids, globs) and is the authority.

## Where it lives

`neptune.yaml`, `neptune.yml` or `neptune.json` at the folder's root is applied automatically (two
of them is an error). `--manifest FILE` (SDK `manifest=path`) names another file, which must be
inside the folder; `--no-manifest` (`manifest=False`) applies none. A manifest outside the folder
is refused: it is evidence about the folder, the package must hold it, and editing it must change
the package. A single-file source has no manifest.

## What a declaration does

Everything a manifest declares is `stated`, with the manifest (a source of the package, by JSON
pointer) as its provenance. Nothing it declares overrides the evidence silently.

| Declaration | Effect | When the evidence disagrees |
|---|---|---|
| `runs` | a declared session (ADR 0036 §6): stated, confidence 1.0; resolves the readings it holds whole | it cuts a reading: both stand, contested, `neptune.grouping.declared_contradicts_layout`; nothing matched: `declaration_unmatched` |
| (any pin) | `--explain` shows it: the source's `pin`, its adapter's verdict `pinned`, the probe's other verdicts beside it | |
| `sources` adapter, one of a tie | selects it; `neptune.manifest.adapter_pinned` (info) replaces the probe's `ambiguous` | — |
| `sources` adapter, accepted but ranked lower | selects it; `pin_overrides_probe` (warning) | — |
| `sources` adapter its probe declines | **not applied**; `pin_refused` (warning); the probe's selection stands | |
| `sources` rule matching nothing | `rule_unmatched` (warning) | |
| `sources` rules disagreeing on one file's copies | none applied; `rules_conflict` (warning) | |
| `machines`, `sites` | a stated `machine` / `site` record each, identified as `("manifest", id)` plus its aliases, every id citing where it is written (ADR 0072) | an alias namespace that is not a record namespace (a lowercase letter, then lowercase letters, digits and `. _ -`; version 1 still accepts `Serial` or `px4:uuid`): left off the record, `alias_namespace_unrepresentable` |
| a run's `machine`, `site`, `task` | a stated `run_declaration` for every run record the files in its `paths` declare | a run record a second entry also covers: both stand, `run_declared_twice`; paths holding files but no recording: `run_unrecorded`; a run stating its machine in a namespace the declared machine has aliases in, as none of them: `machine_contradicts_run` |
| a run's `snapshots` | a stated `snapshot_binding` from every run record it covers to every snapshot record of the pinned bytes | nothing at the path (missing, a directory, a symlink), no file holding the content: `pin_unresolved`; bytes that hold no snapshot: `pin_not_a_snapshot`; a snapshot an earlier pin already binds to the run: bound once, `pin_repeated` (info) |
| `tasks`, `software`, a machine's `name` and `embodiment` | recorded, stated, in the `neptune.manifest` transform's config | |

The last rule matching a path applies to it. Rule `options` are resolved per rule, so a rule with
options is a new transform for what it matches. `adapters` options join the SDK's
`JobOptions.config`; setting one key in both is an error.

## Lineage

A package made under a manifest holds the `neptune.manifest` transform: its config is the
manifest's location, its content id and every declaration. The manifest file is also a listed
source, read by whichever adapter claims it. So the package id changes exactly when the manifest's
bytes change, and is byte-identical when they do not. The grouping transform names the manifest
transform as its upstream. The manifest's own records (machines, sites, run declarations, pins) are
made at assembly from the records the adapters committed, under `neptune.manifest` 0.2.0, citing the
manifest by JSON pointer; one entry covering several recordings gives one record per recording, its
citation made finer by a step naming the run (`neptune.manifest:run`, `neptune.manifest:binding`).
Run declarations and pins read the adapters' runs and snapshots, so their transform (same id,
version and config) names those adapters' transforms as its upstream.
A package holding a run declaration is schema version 9.

## Limits and refusals

A manifest is untrusted input and is used whole or not at all: any problem is exit 6
(`invalid_configuration`) before anything is read, with the line and JSON pointer.

- At most 256 KiB, 16 levels deep, 20 000 values, 4096 characters per value; UTF-8, no control
  characters.
- YAML: a strict subset. No anchors or aliases (`&`, `*`), tags (`!`), directives, several
  documents, block scalars (`|`, `>`), complex keys (`?`), tabs in indentation, duplicate keys, or
  plain values continued on the next line. Quote anything unusual. Plain scalars keep their text:
  `version: 1.10` is `"1.10"` (the editor schema accepts numbers in text fields for that reason).
  `.inf`, `.nan`, `0o17`, `0x1F` and `1e999` are refused unquoted: parsers disagree on them.
- JSON: no duplicate keys, no `NaN`/`Infinity`, no lone-surrogate escapes; numbers keep their
  literal as in YAML.
- Unknown keys anywhere, unknown adapters, bad options, references to undeclared ids, repeated ids
  or run names, absolute paths, `.`/`..` components, `!` globs, a symlinked manifest, and a
  manifest that an ignore rule hides are all refused.

## Not yet

- Records for tasks and software entries, and machine fields beyond ids (embodiment, model): manifest
  version 2 and a task identity kind.
- Validity windows on pins (a manifest `start` / `end`): a pin says which snapshot, not when.
- Manifests outside a read-only folder (needs a package-level source kind for out-of-root files).
