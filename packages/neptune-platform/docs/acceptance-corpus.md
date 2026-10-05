# The acceptance corpus

The programme's one acceptance fixture and the Demo v1 data: a messy hand-over of two sites and three robot
types, with one incident to reconstruct and gold answers to score against. Decision record:
[ADR 0006](adr/0006-acceptance-corpus-layout-versioning-and-gold-answers.md). Code: `harness/acceptance/`.

## Use it

```bash
uv run --all-packages python -m harness.acceptance build /tmp/corpus     # write it (about 50 files, 0.7 MB)
uv run --all-packages neptune ingest /tmp/corpus --out /tmp/corpus-pkg  # one package; one error finding
uv run --all-packages python -m harness.acceptance resolve /tmp/corpus-pkg   # gold evidence -> record ids
uv run --all-packages python -m harness.acceptance check                # build == corpus.lock.json?
make harness                                                            # the harness ingests it by default
```

A gate quotes the version and tree id from the report's corpus line: `acceptance 1.0.0 (tree sha256:…)`.

## What is in it

| Where | What |
|---|---|
| `neptune.yaml` | machines (AMR-05..07 `mobile_base`, ARM-3A `manipulator`, LEG-01 `legged`), sites S-007 and PLANT-2, declared runs |
| `records/asset_register.csv` | both sites' assets; stale for GRP-3A's finger set and LEG-01's firmware |
| `sites/S-007/` | the Deploy D1 AMR fleet narrowed to S-007: URDFs, nav configs (firmware 4.3.1 after the 4.2.0 rollout), zone map and register, CMMS, changes, requalification, MCAP runs, incident INC-0007 |
| `sites/PLANT-2/cmms`, `changes` | the plant's CMMS export (D1 cell rows plus WO-26-0911..0916) and change records (none after 2026-08-18) |
| `sites/PLANT-2/cell3/` | ARM-3A: URDF, calibration history (D1's four plus CAL-ARM3A-0911), the vision PC's OpenCV hand-eye export, the stale `config/cell_config.yaml`, D1 documents, SOP-CELL-021 revisions B and C, incident INC-C3-0011, three ROS 2 bags (MCAP storage), and `shared/for_vendor/cell3_reference_run.mcap`, the 2026-09-09 run under a new name |
| `sites/PLANT-2/legged/` | LEG-01: URDF, patrol configs on firmware 3.1.4 and 3.2.0, a good MCAP patrol (2026-09-12) and the 2026-09-14 patrol bag, cut inside a chunk |
| `sites/PLANT-2/survey`, `vendor`, `maps` | the OT network and time-sync survey, a vendor bulletin with hidden prompt-injection text, the plant zone map |

## The storyline: INC-C3-0011, 2026-09-14, CELL-3

| When (local) | Evidence | What it shows |
|---|---|---|
| 2026-08-18 | CHG0030013, RQ-2026-006 | the previous finger change had a change record and a requalification |
| 2026-08-28 / 09-02 | site survey | the cell PC (CELL3-IPC) lost NTP; +94.1 s on 09-02, gaining 0.2 s a day |
| 2026-09-09 14:00 | `pallet_2026-09-09` bag | last good run: FS-0291, TCP z 145.5 mm, CAL-ARM3A-0818, residual at most 0.8 mm |
| 2026-09-10 16:20 | WO-26-0911 (CMMS only) | long finger set FS-0340, camera bracket refitted, TCP z 151.5 mm on the pendant, recalibration deferred |
| 2026-09-11 | SOP-CELL-021 rev C, CAL-ARM3A-0911, WO-26-0912 | the limit raised from 0.8 to 2.0 px without a change record; recalibration at 1.86 px passes |
| 2026-09-13 | WO-26-0913, LEG-01 config | LEG-01 firmware 3.2.0; recorder cache flush 5 s to 30 s |
| 2026-09-14 14:28-14:32 | `pallet_2026-09-14` bag | vision WARN residual up to 4.1 mm; joint 5 41.7 Nm over 35.0 Nm; protective stop; E-stop at OP-2 |
| 2026-09-14 14:31 | `patrol_2026-09-14` bag | LEG-01 reaches the cell aisle; the bag ends inside a chunk before the collision |
| 2026-09-14 | INC-C3-0011 | HMI times 14:32:38 and 14:32:41; the bag's times are 96.7 s later (the cell PC's clock) |

The managed export `cell_config.yaml` (2026-09-01) still holds the old tool and calibration: the stale config.

## Gold answers

`harness/acceptance/gold.json`: six questions (Q1 why, Q2 what changed since the last good run, Q3 which
configuration was active, Q4 what is unknown, Q5 the timeline and the clocks, Q6 the S-007 AMR incident), each a
reference answer plus claims that cite evidence ids; `traps` list what an answer must not misread. Evidence is a
corpus path and a selector, resolved per package by `harness.acceptance.resolve` (the selectors are documented
there). Scoring: ADR 0006 §6.

## Change it

1. Edit `harness/acceptance/generate.py` (or `gold.json`). Keep every file under 512 KB and every value invented.
2. Bump `VERSION` in `harness/acceptance/__init__.py` by ADR 0006 §3, and `corpus_version` in `gold.json`.
3. `uv run --all-packages python -m harness.acceptance lock`, then `make check PKG=neptune-platform`: the
   ingest test resolves every evidence item and checks the expected findings.
4. Say in the PR what changed for consumers (an answer, an evidence id, a new question).
