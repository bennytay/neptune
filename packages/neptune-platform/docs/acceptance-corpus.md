# The acceptance corpus

The programme's one acceptance fixture and the Demo v1 data: a messy hand-over of two sites and three robot
types, with one incident to reconstruct and gold answers to score against. Decision records:
[ADR 0007](adr/0007-acceptance-corpus-layout-versioning-and-gold-answers.md) (layout, versions, gold answers) and
[ADR 0008](adr/0008-harness-deploy-stage-corpus-declared-mappings-and-the-assertion-selector.md) (the Deploy
declaration, the `assertion` selector, 2.0.0) and
[ADR 0009](adr/0009-declared-source-zones-the-declaration-selector-and-corpus-2-1-0.md) (declared source zones,
the `declaration` selector, 2.1.0). Code: `harness/acceptance/`.

## Use it

```bash
uv run --all-packages python -m harness.acceptance build /tmp/corpus     # write it (56 files, 0.7 MB)
uv run --all-packages neptune ingest /tmp/corpus --out /tmp/corpus-pkg  # one package; one error finding
uv run --all-packages python -m harness.acceptance resolve /tmp/corpus-pkg   # gold evidence -> record ids
uv run --all-packages python -m neptune_deploy map /tmp/corpus-pkg -p cmms_downtime -p cmms_generic \
    -p jira_json -p register_zone -p requalification_csv -p servicenow_csv -p syslog_csv \
    -t packages/neptune-deploy/src/neptune_deploy/lifecycle/presets/templates/incident_report.json \
    --source-zone cmms_downtime sites/PLANT-2/cmms/downtime_log.csv America/New_York \
    --source-zone syslog_csv sites/PLANT-2/cell3/logs/syslog_LOG-P2_2026-09-14.csv America/New_York \
    -o /tmp/corpus-deploy                                               # what the deploy stage runs
uv run --all-packages python -m harness.acceptance check                # build == corpus.lock.json?
make harness                                                            # the harness ingests it by default
```

A gate quotes the version and tree id from the report's corpus line: `acceptance 2.1.0 (tree sha256:…)`.

## What is in it

| Where | What |
|---|---|
| `neptune.yaml` | the run sheet: machines (AMR-05..07 `mobile_base`, ARM-3A `manipulator`, LEG-01 `legged`) with their `cmms.asset` and `servicenow.ci` aliases, sites S-007 and PLANT-2, tasks, software versions, and declared runs: the bags and patrols, each with its task, software and pinned snapshots, and each hand-eye calibration as a session of ARM-3A (easy_handeye's file names no robot) |
| `records/asset_register.csv` | both sites' assets; stale for GRP-3A's finger set and LEG-01's firmware |
| `sites/S-007/` | the Deploy D1 AMR fleet narrowed to S-007: URDFs, nav configs (firmware 4.3.1 after the 4.2.0 rollout), zone map and register, CMMS, changes, requalification, MCAP runs, incident INC-0007 |
| `sites/PLANT-2/cmms`, `changes` | the plant's CMMS work orders (D1 cell rows plus WO-26-0911..0916), its downtime log (DT-26-0914-01: the INC-C3-0011 stop, entered by hand at 14:33:10) and change records (none after 2026-08-18) |
| `sites/PLANT-2/authorisation` | the plant's envelopes in S-007's register layout: ENV-P2-01 (ARM-3A, CELL-3, depends on requalification after any tool change), ENV-P2-02 and -03 (LEG-01, AISLE-C3 and PLC-ROOM) |
| `sites/PLANT-2/cell3/` | ARM-3A: URDF; five hand-eye calibrations as easy_handeye writes them (`CAL-ARM3A-*.yaml`: D1's four, rewritten, plus 0911) and the vision team's `handeye_calibration_log.csv` (errors, limits, procedure); the vision PC's OpenCV hand-eye export; the stale `config/cell_config.yaml`; D1 documents; SOP-CELL-021 revisions B and C; incident INC-C3-0011 and `INC-C3-0011.assertions.json` (A. Novak: the CMMS stop `cmms.downtime` DT-26-0914-01 and the syslog stop `syslog` 4182 are the same stop); `logs/syslog_LOG-P2_2026-09-14.csv` (`Seq,Timestamp,Host,Facility,Severity,Tag,MsgID,Message`: the controller's PSTOP at 14:32:38, the PLC's ESTOP, with RFC 5424 MsgIDs PGM_START, PSTOP, ESTOP, LOTO); three ROS 2 bags (MCAP storage); and `shared/for_vendor/cell3_reference_run.mcap`, the 2026-09-09 run under a new name |
| `sites/PLANT-2/legged/` | LEG-01: URDF, patrol configs on firmware 3.1.4 and 3.2.0, a good MCAP patrol (2026-09-12) and the 2026-09-14 patrol bag, cut inside a chunk |
| `sites/PLANT-2/survey`, `vendor`, `maps` | the OT network and time-sync survey, a vendor bulletin with hidden prompt-injection text, the plant zone map |

## The storyline: INC-C3-0011, 2026-09-14, CELL-3

| When (local) | Evidence | What it shows |
|---|---|---|
| 2026-08-18 | CHG0030013, RQ-2026-006 | the previous finger change had a change record and a requalification |
| 2026-08-28 / 09-02 | site survey | the cell PC (CELL3-IPC) lost NTP; +94.1 s on 09-02, gaining 0.2 s a day |
| 2026-09-09 14:00 | `pallet_2026-09-09` bag | last good run: FS-0291, TCP z 145.5 mm, CAL-ARM3A-0818, residual at most 0.8 mm |
| 2026-09-10 16:20 | WO-26-0911 (CMMS only) | long finger set FS-0340, camera bracket refitted, TCP z 151.5 mm on the pendant, recalibration deferred |
| 2026-09-11 | SOP-CELL-021 rev C, CAL-ARM3A-0911, the calibration log, WO-26-0912 | the limit raised from 0.8 to 2.0 px without a change record; recalibration at 1.86 px passes; the camera's z is 0.0702 m, 4.3 mm below CAL-ARM3A-0818's |
| 2026-09-13 | WO-26-0913, LEG-01 config | LEG-01 firmware 3.2.0; recorder cache flush 5 s to 30 s |
| 2026-09-14 14:28-14:32 | `pallet_2026-09-14` bag | vision WARN residual up to 4.1 mm; joint 5 41.7 Nm over 35.0 Nm; protective stop; E-stop at OP-2 |
| 2026-09-14 14:31 | `patrol_2026-09-14` bag | LEG-01 reaches the cell aisle; the bag ends inside a chunk before the collision |
| 2026-09-14 | INC-C3-0011 | HMI times 14:32:38 and 14:32:41; the bag's times are 96.7 s later (the cell PC's clock) |
| 2026-09-14 14:32:38 / 14:33:10 | syslog 4182, DT-26-0914-01 | the controller's protective stop and the CMMS's hand-entered stop: one stop, 32 s apart |
| 2026-09-15 09:05 | `INC-C3-0011.assertions.json` | A. Novak states the two are the same stop (`same_identity`, payload `same_event`; scope `{cmms.downtime, DT-26-0914-01}` and `{syslog, 4182}`, as Deploy's `cmms_downtime` and `syslog_csv` identify them): stated evidence Memory may join them on |

Every time in the CMMS, downtime log, syslog export and HMI timeline is PLANT-2 local wall time without a zone,
as each source writes it. The sources state no zone; `deploy.json`'s `sources` declare `America/New_York`
(`generate.PLANT_ZONE`) for the downtime log and the syslog export, the mapping's stated configuration
(root ADR 0061 §3). The 32 s compares two times on one declared civil clock.

The managed export `cell_config.yaml` (2026-09-01) still holds the old tool and calibration: the stale config.

The run sheet (`neptune.yaml`, root ADR 0072) pins what each run ran with, as 12 stated `snapshot_binding`
records. It never pins the stale config, and it leaves the runs whose configuration is not in the hand-over
unpinned:

| Run | Snapshots pinned | Software |
|---|---|---|
| cell3-2026-08-20 | CAL-ARM3A-0818 | controller 5.6.0 |
| cell3-2026-09-09 (last good) | CAL-ARM3A-0818 | controller 5.6.0, PALLET_C3 1.4.0 |
| cell3-2026-09-14 (INC-C3-0011) | CAL-ARM3A-0911, `vision/wrist_camera_handeye.yml` | controller 5.6.0, PALLET_C3 1.4.0 |
| leg01-2026-09-12 | `config/2026-09-01/LEG-01_patrol.yaml` | firmware 3.1.4 |
| leg01-2026-09-14 | `config/2026-09-13/LEG-01_patrol.yaml` | firmware 3.2.0 |
| amr05-, amr06-2026-03-03; amr07-2026-04-02 (INC-0007) | none: the only navigation export is for 4.3.1 | firmware 4.2.0 |
| amr07-2026-04-15 | `config/AMR-07/nav2_params.yaml` (revision 12) | firmware 4.3.1 |
| arm3a-handeye-* (5) | none (they record no run); task SOP-CELL-021 | |

The calibration files are what easy_handeye saves (`yaml.dump` of `parameters` and `transformation`; a test
round-trips each through PyYAML). The compiler has no easy_handeye reader until MVL-207, so today they land as
configuration snapshots and the gold answers cite them by pointer (`/transformation/z`).

## Gold answers

`harness/acceptance/gold.json`: eight questions (Q1 why, Q2 what changed since the last good run, Q3 which
configuration was active, Q4 what is unknown, Q5 the timeline and the clocks, Q6 the S-007 AMR incident, Q7 the
32 s CMMS-versus-syslog stop and the assertion that joins them, Q8 the authorisation envelopes), each a
reference answer plus claims that cite evidence ids; `traps` list what an answer must not misread. Evidence is a
corpus path and a selector, resolved per package by `harness.acceptance.resolve` (the selectors are documented
there). Each resolved item lists citations (record id, path, locator); `resolve.supports` is the scoring rule, and
a message is met only by a row. Deploy D3 resolves against the base package and matches on path and locator.
Scoring: ADR 0007 §6; the `assertion` selector (an assertion by its declared id, located by its entry's JSON
pointer) is ADR 0008 §6.

`harness/acceptance/answers.json` (ADR 0011) is the gold questions asked through Context's MCP tools over the
graph Memory builds from this corpus: per question, the tool calls; per gold claim, its class with a reason:
`supported` (a cited statement carries the fact), `co_cited` (statements cite its evidence but state something
else) or a gap. For 2.1.0: 6 supported, 18 co-cited, 16 gaps of 40, asked at the Claude Code skill's budget. `cmms.WO-26-0911.work` and `.firmware` name single cells
of WO-26-0911's row (the `table_row` selector's `column`), next to the whole-row `cmms.WO-26-0911`. Changing the corpus moves the graph, so the
same PR runs `make demo-pin` and classifies what moved ([harness](harness.md#the-pinned-answers)).

## The Deploy declaration

`harness/acceptance/deploy.json` lists the Deploy presets (by name) and document templates (by repository path)
that the harness's deploy stage maps the compiled corpus with. `at_least` gives the lifecycle record counts the
mapped package must reach. It declares `cmms_downtime`, `cmms_generic`, `jira_json`, `register_zone`,
`requalification_csv`, `servicenow_csv` and `syslog_csv` (an event-log preset: a typed `syslog events`
table), and Deploy's `incident_report` template. `at_least` pins what they map: 3 `incident_record`, 3
`intervention`, 16 `maintenance_event`, 4 `requalification_record`, 5 `change_record`, 5
`authorisation_envelope` and 4 `structured_record` (the syslog events). It declares both `sources` zones and
`require_assertion_scopes`. The stage is red when a declaration maps nothing
([harness](harness.md#read-the-report), ADR 0008).

### Declared source zones

`sources` (ADR 0009) states, per preset and corpus path, what the reading transform's configuration declares
about a source that states nothing itself. Today the only field is the civil time zone:

```json
"sources": [
  {"preset": "cmms_downtime", "source": "sites/PLANT-2/cmms/downtime_log.csv", "civil_time_zone": "America/New_York"},
  {"preset": "syslog_csv", "source": "sites/PLANT-2/cell3/logs/syslog_LOG-P2_2026-09-14.csv", "civil_time_zone": "America/New_York"}
]
```

The preset must be in `presets`, the source a plain corpus path, the zone an IANA name (spelling only). The
stage passes each entry as `--source-zone PRESET SOURCE ZONE`, and is red unless the mapped package holds that
preset's `civil_time_zone` for that source stating that zone. Gold cites an entry with the `declaration`
selector `{path, preset, field: "civil_time_zone", equals}`. Gold Q7
cites both (`zone.downtime`, `zone.syslog`).

### No dangling same-event links

`"require_assertion_scopes": true` makes the stage check that every scope entry of every stated same-event
assertion (`INC-C3-0011.assertions.json`) is an identifier the mapped package declares, compared as a
`(namespace, value)` pair. `{cmms.downtime, DT-26-0914-01}` is declared by `cmms_downtime`'s intervention, and
`{syslog, 4182}` by `syslog_csv`'s `@id:syslog` column (the raw Seq cell: never pad or reformat it in
`generate.py`). The acceptance corpus sets it.

## Change it

1. Edit `harness/acceptance/generate.py` (or `gold.json`, `deploy.json`). Keep every file under 512 KB and every
   value invented.
2. Bump `VERSION` in `harness/acceptance/__init__.py` by ADR 0007 §3, and `corpus_version` in `gold.json` and
   `deploy.json`.
3. `uv run --all-packages python -m harness.acceptance lock`, then `make check PKG=neptune-platform`: the
   ingest test resolves every evidence item and checks the expected findings. Regenerate Memory's snapshot
   (its `acceptance_corpus_snapshot.py`) and re-pin the answers (`make demo-pin`), reviewing both diffs.
4. Say in the PR what changed for consumers (an answer, an evidence id, a new question).

2.1.0 (MVL-191) added the syslog export's `MsgID` column and renamed the assertion's scope to Deploy's generic
namespaces (`cmms.downtime`, `syslog`) and its ticket to `cmms.work_order`. It added evidence `zone.downtime` and
`zone.syslog` (the `declaration` selector), cited by Q7.C4; the run sheet's pins, aliases, tasks and software,
with evidence `pin.0909.calibration`, `pin.0914.calibration`, `pin.0914.vision`, `pin.leg0914.config` (the
`pin` selector; Q2.C3, Q3.C2, Q3.C6) and the gap claim Q6.C3 (`s007.manifest.amr07-0402`,
`s007.nav2.amr07.firmware`, `s007.WO-26-0414`). No existing evidence id or answer changed meaning; a
consumer that matches the scope's namespaces matches the new spelling.

2.0.0 (MVL-191) moved `cal.0818.z`, `cal.0911.z` (now `/transformation/z`) and `cal.0818.error`, `cal.0911.error`
(now calibration-log rows). It added the PLANT-2 envelopes, the downtime log, the syslog export, the assertion,
the calibration log, Q7 and Q8, and evidence ids `cmms.stop.DT-26-0914-01`, `syslog.pstop`, `syslog.estop`,
`assert.same-stop`, `survey.controller-sync` and `env.ENV-P2-01..03`.
