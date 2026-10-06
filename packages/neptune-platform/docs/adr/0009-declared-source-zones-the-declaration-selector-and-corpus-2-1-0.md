# 0009 — Declared source zones in the Deploy declaration, the declaration selector, and corpus 2.1.0

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-191
- Amends: ADR 0008 §2, §3 and §5 (a `sources` key, its check, the CI plan); ADR 0007 §5 and §6 (a
  new selector)

## Context

The acceptance corpus's CMMS downtime log and syslog export write plant local wall time without a
zone (`2026-09-14 14:32:38`). Only a comment in `generate.py` said that the zone is PLANT-2's, so Q7's
"32 s apart" claim rested on something no consumer could cite. Deploy's `cmms_downtime` preset (Deploy
PR #145) and its upcoming `syslog_csv` mapping read these two tables. MVL-191 triage, 2026-10-06. Forces:

- **Root ADR 0061 §3.** When a source states no zone, the reading transform's configuration may state
  it. The `civil_time_zone` companion then cites the table and is `stated`. A `timezone` column would
  change what the export is: a real collector's CSV has none.
- **A preset is generic.** `cmms_downtime` reads any CMMS's downtime log, so its zone is `unstated`.
  The zone belongs to one export at one site, so it is declared per source, beside the preset that
  reads it, not in the preset.
- **No green on nothing** (ADR 0008 §3). A declared zone that Deploy drops must turn the stage red.
- **Gold must cite it.** The declaration is not in the compiled package. It lives in `deploy.json`,
  next to `gold.json` and outside the generated tree (ADR 0008 §2).
- **Memory joins two stops on a stated assertion.** That only works if the assertion names each stop
  exactly as Deploy's records identify it. Deploy uses generic namespaces (`cmms.work_order`,
  `cmms.downtime`, `syslog`), and the corpus said `plant-2.cmms.downtime` and `plant-2.syslog.log-p2`.
- **Memory's acceptance snapshot is built from the harness's packages.** It reads the compiled and
  the mapped package, so an adapter, a corpus or a deploy stage change can move it. The CI plan ran
  Memory's job on none of these unless the compiler core changed.

## Decision

1. **`sources` in `deploy.json`** (optional; `deploy_format` stays 1, and a declaration without the
   key reads as before):
   `"sources": [{"preset": <a name in presets>, "source": <corpus path>, "civil_time_zone": <IANA name>}]`.
   The stage refuses the whole declaration when:
   - an entry has other keys or an empty value;
   - its preset is not declared in `presets`;
   - its source is not a plain relative corpus path (no empty, `.` or `..` part, no `\` or NUL);
   - its zone is not spelled as an IANA name (root ADR 0061 §1: syntax only, never looked up);
   - one preset's source is declared twice.

   Entries are kept sorted by preset and source. When `presets` itself does not read, membership
   is not judged, so its own problem is the one reported.
2. **The run** (amends ADR 0008 §3). Each entry is passed to the map as
   `--source-zone PRESET SOURCE ZONE`, for presets Deploy ships (an unshipped one is already the
   case's problem, and Deploy refuses a zone for a preset the run does not map). That is Deploy's
   per-source zone option (Deploy ADR 0017 §2): the mapper reads the
   source's zone-less times in that zone and writes `civil_time_zone` `Known(zone)`, which is
   `stated` and cites the table. After the map, the case is red when, for an entry:
   - the compiled package holds no source at its path;
   - or the mapped package holds no `civil_time_zone` record that cites that source and was made by
     that preset's transform (attributed through `mapping_sha256`, as in ADR 0008 §3);
   - or any such record does not state the declared zone.

   The report row carries the entries under `zones`.
3. **The `declaration` selector** (extends ADR 0007 §5 and §6).
   - It is written `{kind: "declaration", path, preset, field, equals?}`. It selects the `sources`
     entry of `deploy.json` for that preset and corpus path that holds `field` (today
     `civil_time_zone`), and whose value equals `equals` when given. The path must still be a source
     of the package.
   - Its record id is `declaration:sha256:<sha256 of the entry's canonical JSON>`: content-addressed,
     so reordering the list keeps it and changing the value changes it.
   - Its locator is `{"declaration": "harness/acceptance/deploy.json", "pointer": "/sources/<i>/<field>"}`.
   - `resolve.supports` is unchanged. `gold_format` stays 1 because the addition is additive.
4. **Corpus 2.1.0** (minor, ADR 0007 §3: no answer changes meaning, no cited evidence moves).
   - The syslog export gains the sender's RFC 5424 `MsgID` column (`PGM_START`, `PSTOP`, `ESTOP`,
     `LOTO`), one code per event type for a mapping to key on.
   - The same-event assertion's scope becomes `{cmms.downtime, DT-26-0914-01}` (the Downtime ID, as
     `cmms_downtime` identifies it) and `{syslog, 4182}` (the Seq, as `syslog_csv` identifies it).
   - Its ticket becomes `{cmms.work_order, WO-26-0915}` (as `cmms_generic` identifies it).
   - The author and assertion ids keep the review console's own namespaces.
   - PLANT-2's zone is `generate.PLANT_ZONE` (`America/New_York`), which the assertion's
     `authored_zone` also uses.
5. **CI** (amends ADR 0008 §5). `ci_plan.py`'s `MEMORY_SNAPSHOT_INPUTS` covers every format adapter
   (`src/neptune/adapters/`), `harness/acceptance/`, the harness modules Memory's snapshot generator
   runs (`stages.py`, `run.py`, `corpus.py`, `contracts.py`) and the deploy stage's inputs. With
   `CORPUS_INPUTS`, a change to any of them also runs `neptune-memory`. It is added after the
   dependency propagation: the snapshot moved, not Memory's code, so Memory's dependents (Context)
   do not run for it. `harness.yml` already covers these paths (`src/neptune/**`, `harness/**`), and
   a workflow test keeps it so.

## Alternatives considered

- **A `Timezone` column in each CSV.** Lost: a collector's export does not carry one, and ADR 0061 §3
  puts a configured zone in the transform's configuration, not in made-up source bytes.
- **The zone in the preset.** Lost: presets are generic across sites, and `cmms_downtime` states
  `unstated` on purpose (Deploy ADR 0016 §1).
- **A per-preset zone (`{preset: zone}`).** Lost: one preset can read two sites' exports in different
  zones. The source path is what the zone describes.
- **Citing the mapped package's `civil_time_zone` record.** Lost: gold resolves against the compiled
  package (ADR 0007 §6), and that record's id changes with Deploy's version. The declaration is the
  stated evidence, and the mapped record is checked by the stage instead.
- **A position-based declaration id (`deploy.json#/sources/0`).** Lost: adding an entry would change
  the ids of the others. The locator keeps the position for people, and the id keeps the content.
- **`plant-2.`-prefixed namespaces kept, with Memory mapping them.** Lost: every consumer would need a
  translation table. One generic spelling, the one Deploy writes, joins without one.
- **A major version for the namespace change.** Lost: ADR 0007 §3 ties major to gold meaning and moved
  evidence, and neither changes. Consumers that match scope namespaces are named in the PR.

## Consequences

- Deploy must ship `--source-zone` on `neptune_deploy map` before `deploy.json` declares `sources`,
  and the corpus's `cmms_downtime` and `syslog_csv` presets with it. Until then the gold's
  `declaration` item and the `sources` entries wait. The PR that wires them is the one that adds the
  presets.
- A Deploy change that ignores a declared zone turns the harness and the platform job red.
- Memory's job runs on adapter and corpus changes, so a snapshot that drifts fails in the PR that
  moved it, not on main.
- Revisit when a source needs another declared field (the `field` key exists for that), or when the
  compiler's tabular adapter takes a zone option itself (the zone would then be in the compiled
  package and citable by record).
