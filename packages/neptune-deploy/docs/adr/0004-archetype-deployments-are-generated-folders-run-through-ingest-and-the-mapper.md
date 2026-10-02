# 0004 — The archetype deployments are generated folders, run through ingest and the mapper, with golden receipts

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-115

## Context

Every later layer needs the same two realistic deployments to test against: a warehouse AMR fleet and a
manipulator cell. The compiler already has two worked examples of those names (root ADR 0051, MVL-83). They
are hand-written lifecycle records over one `records.json` each, with no adapters involved, so they pin the
model and not the pipeline. What is missing is the pipeline's input: a folder a team would actually hand
over, with logs, bags, URDFs, configs, maps, exports and PDFs, some of them damaged, and the evidence that
`neptune ingest` plus the Deploy mapper (ADR 0002, ADR 0003) turn it into a package and a lifecycle lineage
without a failed job. Platform's harness has a marked hook (`harness/corpus.py` `ARCHETYPES`) waiting for
this corpus, and the D1 gate (MVL-116) stress-tests lifecycle records on both.

## Decision

1. **Two generated folders.** `tests/fixtures/archetypes/make_archetypes.py` writes `sources/warehouse_amr_fleet`
   and `sources/manipulator_cell`; each is an ingest root with its own `neptune.yaml`. The files are
   committed (so a reviewer and Platform read real bytes) and a fast test checks they are exactly what the
   generator writes, so the corpus grows by editing the generator. Every file is under 512 KiB (the largest source is about 21 KiB and the largest golden table 46 KiB) and the generator uses no clock, randomness, network or third-party writer.
2. **Faithful to the compiler's examples, and to every robot.** The fleet reuses the worked example's
   identifiers (site S-007, AMR-07, INC-0007, zones DOCK-1 and PICK-A) and the cell reuses CELL-3 and ARM-3A.
   The fleet is six AMRs of two models (tug and lift) at two sites, with no drone or flight log; the cell is
   one arm. Contents:
   - Fleet: seven MCAP runs (one per AMR, AMR-07 twice around its firmware change, and AMR-08's ROS 2 bag),
     two URDFs, six `nav2_params.yaml` configs, two GeoJSON zone maps, a zone authorisation register, a
     CMMS work-order export, a ServiceNow firmware change export, a requalification sheet and two incident
     report PDFs.
   - Cell: a ROS 2 bag of joint states, a URDF, four calibration files (commissioning and three
     recalibrations), a CMMS export (a controller update, a scheduled recalibration, a joint drive
     replacement with a recalibration, a tool change with a recalibration, and an inspection after the near
     miss), a ServiceNow export (controller update, tool offset), a requalification sheet (three rows), a
     Jira near-miss export, and a risk assessment, a commissioning report and a tool-change SOP as PDFs.
3. **The pipeline is the one ADR 0002 draws, split where the root rule requires.** `neptune ingest` builds
   each base package and the Deploy mapper builds the lifecycle lineage from it with the deployment's
   declared files. A member may not import the compiler's runtime or SDK (root
   `tests/unit/test_merge_freshness.py`: only `neptune-platform` ingests), so the generator runs the
   command line as a subprocess (`--isolation in_process --job archetype`, so the receipt does not depend
   on the host's sandbox level and the envelope is stable) and commits the result as
   `packages/<name>/`, without `volatile/`, as ADR 0002 does for its fixtures. Deploy's tests run only the
   mapper over those packages (`neptune_deploy.lifecycle.map_package`). The declared files are shipped
   presets where one fits (`cmms_generic`, `servicenow_csv`, `register_zone`, `jira_json`), MVL-114's risk
   and commissioning templates unchanged, and four files written here (`declared/`: the fleet's
   requalification mapping and incident template, the cell's requalification mapping, which is MVL-113's
   with the plant's zone, and its SOP template). No plugin entry point is loaded: the compiler does not
   load them yet (MVL-200), and the mapper is outside the ABI (ADR 0002 §2).
4. **What is golden, and what catches what.** `golden/<name>/lifecycle` is the mapper's package whole but for
   its empty tables (about 220 KiB for the fleet; the manifest lists those with their hashes), and a fast
   test in Deploy's job checks that mapping the committed base package gives exactly it. A mapper, mapping
   or template change moves it. The base packages are committed ingest output: the receipts of
   `packages/<name>/` are the compiler's receipts for these sources. A test checks that each base package
   holds exactly these sources by content hash, so a source edit without a re-ingest fails, but Deploy's
   job does not re-ingest and so does not see a compiler adapter change. That drift (an adapter change
   altering what ingest writes for these sources) is caught where ingestion is allowed: the Platform
   harness, which ingests the corpus (X4 and MVL-181). Regenerating the packages after an adapter change is
   a deliberate PR with the diff explained (root AGENTS.md).
5. **Damage is a finding, never a failed job.**
   - The corrupt bag is AMR-08's rosbag2 bag from the day of INC-0013: the MCAP storage file is cut in the
     middle of its last chunk (an interrupted copy) and the metadata still claims every message. The base
     receipt has `mcap.truncated` (error) and `mcap.chunk_truncated` (warning), the whole records before
     the cut are read, and the other six runs are unaffected.
   - The stale config is AMR-09's `nav2_params.yaml`: it declares `config_revision: 11` and
     `firmware_compat: 4.2.0` where its five siblings declare 12 and 4.3.1, and it keeps a duplicated
     `max_vel_x` from a hand merge. The base receipt has `config.duplicate_key` on it. The change export
     states AMR-09 went to 4.3.1. Both facts are in the packages, `stated` or `observed`, and nothing
     decides that the config is stale: that is a later layer's comparison, not Deploy's (package AGENTS.md,
     non-negotiable 2).
   - The exports carry rows no mapping reads (an inspection work order, a task ticket), a blank completion
     date, a date written in another format than its column's and a requalification with no decision time. The mapper
     reports `row_unmatched`, `value_blank`, `value_unreadable` and `list_cell_blank`; those records keep
     `Unknown` fields.
6. **Clocks stay as declared, and the stories agree.** Logs are POSIX nanoseconds (UTC); incident, CMMS and
   requalification times are wall-clock text. The fleet's mapping and template zones are all `unstated`,
   because two sites in two zones share one export and one form. The cell's PDF templates (risk,
   commissioning, SOP) and its requalification mapping declare `America/Detroit`; the shipped CMMS,
   ServiceNow and Jira presets it also uses declare `unstated`, and stay so rather than guess. Its
   calibration files and near-miss ticket state offsets of the plant's zone (-05:00 in February, -04:00 in
   summer). Each calibration precedes the work order and requalification that cite it, every requalification
   cites an earlier work order, and every CMMS row after the controller update (2026-03-10) states the
   5.6.0 it left. A contradiction in a golden lifecycle package is therefore a finding about the pipeline or
   a deliberate case (the stale AMR-09 config), never an accident of the fixtures. Nothing is moved to UTC.
7. **Formats the compiler reads today.** ROS 2 bags use MCAP storage with uncompressed chunks, because a
   SQLite file's header carries the library version and a compression library changes bytes; the MCAP and
   the bag were read once with the official `mcap` and `rosbags` readers (never dependencies) to check them,
   and the cut bag fails in them as intended. The compiler reads URDF and GeoJSON as plain text only, and
   does not decode MCAP or bag payloads (`mcap.payload_not_decoded`, 21 info findings in the fleet); those
   are recorded as findings, not worked around.
8. **Handing the corpus to Platform X4.** The corpus is `tests/fixtures/archetypes/sources/`, one folder per
   archetype, which is the shape `harness/corpus.py` expects. X4 sets
   `ARCHETYPES = REPO / "packages" / "neptune-deploy" / "tests" / "fixtures" / "archetypes" / "sources"` (the
   hook currently names a path this package does not use). `packages/<name>` is what a harness
   run's compiler stage should reproduce (its drift check, MVL-181); the lifecycle stage reads
   `golden/<name>/lifecycle`.

## Alternatives considered

- **The compiler's `warehouse_amr` and `manipulator_cell` records as the corpus.** They are one JSON export
  each and skip every adapter, so no log, config or document is exercised. Kept as the model's golden, not
  used here.
- **Running the real ingest inside Deploy's tests (the first version of this PR).** It tests the compiler's
  output end to end, but it imports the compiler's runtime, which the root rule forbids members (adapter-only
  compiler changes are routed past member jobs, so such a test would go red on main unseen). Lost; the
  harness owns re-ingest.
- **Writing the bags and PDFs with `mcap`, `rosbags` or `reportlab`.** Each would be a new dependency, and
  `reportlab` embeds a clock. Lost; the compiler's own fixture writers do the work.
- **SQLite-storage bags.** Their bytes follow the host's SQLite. Lost.
- **Mapping the GeoJSON zone maps into lifecycle records.** A zone map is a map, not a lifecycle event, and
  the zone limits an envelope states come from the register. The maps stay evidence in the base package.

## Consequences

- The committed base packages record the versions of the libraries the adapters used (`pypdf`, `pyarrow`,
  `pyyaml`, `lz4`, `zstandard`) and the Parquet bytes `pyarrow` wrote, like the compiler's own golden
  packages. A dependency bump does not fail Deploy's job; it shows up when the packages are regenerated.
- Committed base packages mean the lifecycle goldens cannot drift because of an adapter change, and neither
  can they notice one. The Platform harness is where an adapter change shows up (MVL-181 wires the check);
  until then regenerating after an adapter change is a manual step this ADR records.
- When MVL-200 lands, the same corpus drives the pipeline through the loaded plugin; a real URDF or GeoJSON
  adapter and payload decoding will change the base receipts, and the corpus needs no edit.
- MVL-181 (the acceptance corpus) can extend these folders rather than start again.
- Revisit if the corpus passes a few hundred KiB per folder, or if a third archetype (a legged or marine
  deployment) is wanted: it is a new function in the generator and a new entry in `PIPELINES`.
