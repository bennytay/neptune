# The Neptune manifest

Status: MVL-14, ADR 0047. Optional. Most folders need none: `neptune ingest` discovers, probes and
groups on its own. A manifest is for what discovery cannot settle: two adapters that claim a file
equally, session readings grouping cannot choose between, and what no file says (which robot,
site, task or software a run involved).

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
| `sources` adapter, one of a tie | selects it; `neptune.manifest.adapter_pinned` (info) replaces the probe's `ambiguous` | — |
| `sources` adapter, accepted but ranked lower | selects it; `pin_overrides_probe` (warning) | — |
| `sources` adapter its probe declines | **not applied**; `pin_refused` (warning); the probe's selection stands | |
| `sources` rule matching nothing | `rule_unmatched` (warning) | |
| `sources` rules disagreeing on one file's copies | none applied; `rules_conflict` (warning) | |
| `machines`, `sites`, `tasks`, `software` | recorded, stated, in the `neptune.manifest` transform's config; runs reference them | |

The last rule matching a path applies to it. Rule `options` are resolved per rule, so a rule with
options is a new transform for what it matches. `adapters` options join the SDK's
`JobOptions.config`; setting one key in both is an error.

## Lineage

A package made under a manifest holds the `neptune.manifest` transform: its config is the
manifest's location, its content id and every declaration. The manifest file is also a listed
source, read by whichever adapter claims it. So the package id changes exactly when the manifest's
bytes change, and is byte-identical when they do not. The grouping transform names the manifest
transform as its upstream.

## Limits and refusals

A manifest is untrusted input and is used whole or not at all: any problem is exit 6
(`invalid_configuration`) before anything is read, with the line and JSON pointer.

- At most 256 KiB, 16 levels deep, 20 000 values, 4096 characters per value; UTF-8, no control
  characters.
- YAML: a strict subset. No anchors or aliases (`&`, `*`), tags (`!`), directives, several
  documents, block scalars (`|`, `>`), complex keys (`?`), tabs in indentation, duplicate keys, or
  plain values continued on the next line. Quote anything unusual. Plain scalars keep their text:
  `version: 1.10` is `"1.10"`.
- JSON: no duplicate keys, no `NaN`/`Infinity`.
- Unknown keys anywhere, unknown adapters, bad options, references to undeclared ids, repeated ids
  or run names, absolute paths, `.`/`..` components, `!` globs, a symlinked manifest, and a
  manifest that an ignore rule hides are all refused.

## Not yet

- Canonical `Machine`, `Site` and `Run` records from declarations, and a task record kind (needs a
  package-schema change).
- Manifests outside a read-only folder (needs a package-level source kind for out-of-root files).
