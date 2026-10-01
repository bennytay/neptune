# 0047 — The optional manifest: a source in the folder, stated declarations set against the evidence, generated as commented choices

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-14
- Extends: ADR 0036 §6 (the manifest fills `GroupingConfig.sessions`; the grouping transform's
  upstream names the manifest), ADR 0035 §8 (pinning an adapter is the manifest's) and §6 (manifest
  errors are `invalid_configuration`), ADR 0043 §6–§7 (found at the root and read like
  `.neptune-ignore`)

## Context

Discovery cannot settle everything. Two adapters tie on a file (the probe engine reports it and
chooses none, ADR 0027); grouping contests a folder's readings (ADR 0036 §4); no file says which
robot, site or task a run was. MVL-14 asks for an escape hatch: a YAML/JSON manifest for run,
machine, site, task, software and source declarations, overrides and aliases that never copy
source content, an editor JSON Schema, and `neptune init-manifest` from discovered metadata. Its
acceptance is that a developer turns an ambiguous folder into deterministic ingestion with a small
manifest and no code.

The constraints: a declaration is the user's statement, so `stated`, never `observed`, and it may
not silently override evidence (non-negotiables 2–4); the package must change exactly when the
manifest does (non-negotiable 5–6); a manifest is user-written input, so hostile (non-negotiable
9). The package schema is contended (`contracts/package-schema`, #37's 2.0.0 bump): no new record
kind and no change to the package layout.

## Decision

1. **Schema version 1** (`neptune.manifest.schema`, `docs/schema/manifest.schema.json`):
   `neptune: 1` (required); `machines`, `sites`, `tasks`, `software` (entities by declared `id`,
   with `name`, `description`, and `aliases`: other identifiers the evidence uses for it, by
   namespace, the only way two names become one entity since Neptune never merges identities);
   `runs` (a `name`, root-relative `paths`, and the `machine`, `site`, `task`, `software` it
   involved); `sources` (a `path` or `glob` choosing an `adapter` and its `options`); `adapters`
   (options for every file an adapter reads); `grouping` (`gap_seconds`). Unknown keys, wrong types,
   dangling references, repeated ids and bad paths are `ManifestError`s naming the JSON pointer
   and line. `Manifest.to_json()` is canonical and is itself a valid manifest. Plain YAML scalars
   and JSON numbers keep their literal, so a text field reads `version: 1.10` as `"1.10"`; the
   editor schema therefore accepts a number or boolean wherever text goes ("read as text").
2. **Hostile input, refused whole.** At most 256 KiB, 16 levels, 20 000 values, 4096-character
   scalars; strict UTF-8, no control characters. JSON (a `.json` name) without duplicate keys or
   `NaN`. Otherwise a strict YAML subset (block and flow collections, plain and quoted scalars,
   comments) that refuses anchors and aliases (no alias bombs), tags, directives, several
   documents, block scalars, complex keys, tabs in indentation, duplicate keys and multi-line plain
   values. YAML 1.2 core-schema forms it does not resolve (`.inf`, `.nan`, `0o17`, `0x1F`) and
   numbers no float holds (`1e999`) are refused with a request to quote them, so a YAML 1.2 parser
   reads what it accepts as the same tree. Lone surrogates in JSON escapes are refused. A seeded
   mutation fuzz checks that every input ends in a manifest or a `ManifestError`. Paths are root-relative with no `.`/`..`; globs are the ignore-rule syntax anchored at
   the root, no negation. Any problem is a `ManifestError` → `ConfigurationError`
   (`invalid_configuration`, exit 6) before anything is walked: never half-applied (as ADR 0043 §7).
3. **The manifest is a file in the folder it describes.** `neptune.yaml`, `neptune.yml` or
   `neptune.json` at the root is found automatically (two of them is an error); `manifest=<path>`
   (`--manifest`) names another file, which must resolve inside the root; `manifest=False`
   (`--no-manifest`) uses none. It is opened through the walk's safe `open` (no symlinks, regular
   files only). A single-file source has none. Being inside the root, it is walked, hashed, probed
   and listed like any file: a source with its own content id that the package holds, which
   `export` can materialise. The job checks that the scan hashed the same bytes at the same place
   (else the job fails: a manifest edited mid-job, or one an ignore rule hides, is never applied).
4. **Its lineage.** The `neptune.manifest` transform (0.1.0) has config `{location, source
   (content id), declarations}` and is in every package made under a manifest. So the package id
   changes when the manifest's bytes change (new revision, new transform) and is identical when they
   do not; a manifest that declares nothing still names itself.
5. **Runs are declared sessions** (ADR 0036 §6, unchanged): stated proposals at 1.0, set against
   the rules' readings, resolving what they hold whole and contesting what they cut
   (`declared_contradicts_layout`). The grouping transform's `upstream` is the manifest
   transform, so every declared proposal leads back to the manifest. With a manifest,
   `JobOptions.grouping` must be default.
6. **Source rules choose among what the probe observed, never past it.** The last rule matching a
   location applies; all locations of one artifact must get the same rule (else
   `neptune.manifest.rules_conflict`, ambiguous, warning, nothing applied). The rule's adapter:
   ranked first by the probe → selected, no finding; one of a tie → selected, and
   `adapter_pinned` (ambiguous, info) replaces the probe's `ambiguous` finding (its candidates are
   in the details); accepted but ranked lower → selected with `pin_overrides_probe`
   (inconsistent, warning); declined by its own probe → **not applied**, `pin_refused`
   (inconsistent, warning), the probe's selection stands. A rule that matches no file is
   `rule_unmatched` (missing, warning). Each finding is the manifest transform's and cites the rule
   in the manifest's bytes by JSON pointer (`related`, or the subject). Rule options resolve per
   rule (`configure`), so they are a new transform; `adapters` options join `JobOptions.config`,
   and a key set in both is an error.
7. **`neptune init-manifest <folder>`** (`neptune.manifest.generate`) writes a manifest from one
   SDK dry run (probes sandboxed, no manifest applied) and the folder's layout grouped by the job's
   grouper: ties as one commented line per adapter, contested readings as commented blocks, then
   uncontested proposals, probe winners and entity templates. **As written it declares nothing**: a
   guess is never written as a declaration; the user uncomments what is true and so states it.
   Deterministic text (sorted, no clock, no host paths); validated by the reader before it is
   written; written atomically; never over an existing file without `--force` (exit 5).
8. **`--explain` shows the manifest's choice** (extends ADR 0044 §2). A pinned source's
   explanation carries `pin` (adapter, manifest location and content id, rule pointer); its
   adapter's verdict is `pinned`, and the other verdicts stay the probe's (`tied`, `outranked`,
   `declined`) with a `why` naming the rule that chose.
9. **Machines, sites, tasks and software are stated in the manifest transform's config** and
   referenced by runs; this version emits no canonical `Machine`, `Site` or `Run` record from them
   and none for tasks (there is no task record kind, and adding one changes the package schema).

## Alternatives considered

- **PyYAML (`safe_load`).** Not a runtime dependency today; it accepts anchors and aliases (an
  alias bomb is a traversal blow-up even when memory is shared), tags of its own, and YAML 1.1
  scalars (`no` → false, `1.10` → 1.1). A small strict subset is fewer surprises and exactly what
  a manifest needs.
- **A manifest anywhere (`--manifest /etc/...`).** Its bytes would not be in the package, so no
  record could cite it (`export` fails on a cited source no location holds) and a package would
  depend on a file outside what it lists. Read-only folders need a package-level source kind for
  out-of-root files: a package-schema change, so a follow-up.
- **Pins that always win.** A declared adapter whose own probe declines the bytes would parse
  garbage under a stated label; refusing it with a finding keeps the observation authoritative.
- **Pins that never override a ranking.** A user who knows a `.txt` is a table could not say so;
  an accepting adapter is a reading the evidence supports, and the warning records the override.
- **Keep the probe's `ambiguous` (error) finding after a pin resolves the tie.** It would claim the
  source produced no output when it did; ADR 0036 drops a resolved contest's finding the same way.
- **Generated declarations active by default** (winners pinned, proposals declared). It turns
  inferences into statements nobody made, and freezes today's adapter choices into the folder.
- **Canonical `Machine`/`Site`/`Run` records from declarations now.** The right end state, but it
  needs the record shapes for declared entities (and a task kind) agreed with the contended package
  schema; the declarations are already stated and in the lineage.
- **A separate grouping override file, or `JobOptions` fields for every section.** Two
  configuration models; the manifest fills the runtime's existing ones.

## Consequences

- A folder becomes deterministic with a few uncommented lines and no code; the integration test
  proves it over a manipulator and a quadruped.
- Packages made without a manifest are unchanged; packages made with one carry the manifest
  transform, the manifest's revision and (when its rules fire) `neptune.manifest.*` findings.
- `JobOptions` gains `manifest`; every SDK call gains `manifest=`; the CLI gains `--manifest`,
  `--no-manifest` and `init-manifest`. Exit codes are unchanged.
- `init-manifest` hashes the folder twice (the dry run, then the layout); MVL-15's explanation can
  replace the second pass once merged.
- Revisit when manifests must live outside a read-only folder, when declared entities should
  become canonical records (and a task kind exists), or when users need rules beyond last-match.
