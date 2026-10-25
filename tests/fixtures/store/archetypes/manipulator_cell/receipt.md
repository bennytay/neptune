# Ingest receipt

Receipt `rec:sha256:a42a843aef284da07943e6560298173d89578fb2df35b098ed79633078839d1c`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 15 seen, 15 read, 0 not read, 0 gone
- Runs: 2; streams: 2; entities: 0
- Findings: 0 errors, 4 warnings, 9 info; ambiguous fields: 4

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bags/pick_place_2026-08-20/metadata.yaml` | 1480 | `sha256:6af83e864da1` | rosbag2 0.2.0, neptune.grouping 0.2.0 |
| `bags/pick_place_2026-08-20/pick_place_2026-08-20_0.mcap` | 22273 | `sha256:a6eff07ae440` | mcap 0.2.0 |
| `calibration/CAL-ARM3A-0226.yaml` | 313 | `sha256:fecc413f15fd` | config 0.1.0 |
| `calibration/CAL-ARM3A-0415.yaml` | 326 | `sha256:7734a681a3f1` | config 0.1.0 |
| `calibration/CAL-ARM3A-0623.yaml` | 334 | `sha256:530ef05aaa16` | config 0.1.0 |
| `calibration/CAL-ARM3A-0818.yaml` | 334 | `sha256:a2d19ecfbcda` | config 0.1.0 |
| `changes/servicenow_changes.csv` | 626 | `sha256:fdca33396278` | tabular 0.2.0 |
| `cmms/work_orders.csv` | 950 | `sha256:96d3810cd8c7` | tabular 0.2.0 |
| `documents/commissioning_CR-C3-2026-02.pdf` | 12618 | `sha256:5e9fde08afd8` | pdf 0.1.0 |
| `documents/risk_assessment_CELL3-RA-009.pdf` | 11475 | `sha256:9929112c0ec5` | pdf 0.1.0 |
| `documents/sop_CELL-014_finger_set.pdf` | 6551 | `sha256:962616825f67` | pdf 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |
| `requalification/requalification_tests.csv` | 987 | `sha256:30be864d249a` | tabular 0.2.0 |
| `tickets/near_miss_export.json` | 1046 | `sha256:a48873e408ee` | tabular 0.2.0 |
| `urdf/arm6.urdf` | 1767 | `sha256:7f79c49b41ef` | text 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `mcap` | `0.2.0` | `sha256:2a8c92ad6f72` | lz4 4.4.5, zstandard 0.25.0 | `rec:020b1b8799e7` |
| `neptune.bindings` | `0.1.0` | `sha256:dbfdd73f0250` | none | `rec:eeeae4b50647` |
| `neptune.clocks` | `0.1.0` | `sha256:8abe788c243b` | none | `rec:813f84ae2b74` |
| `neptune.frames` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:ebb96c0ca1d5` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:5a4dc4d9d1f4` |
| `neptune.introspection` | `0.1.0` | `sha256:c9bc334a3ccc` | none | `rec:a4c22d1acf45` |
| `neptune.manifest` | `0.1.0` | `sha256:3bedd387c711` | none | `rec:8a63ca51c7d3` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `rosbag2` | `0.2.0` | `sha256:6407455961dc` | none | `rec:4df8cd116009` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `configuration_snapshot` | 5 |
| `configuration_value` | 66 |
| `document_block` | 43 |
| `document_record` | 4 |
| `ingest_finding` | 13 |
| `run` | 2 |
| `run_assembly` | 1 |
| `source_artifact` | 15 |
| `source_revision` | 15 |
| `stream` | 2 |
| `structured_record` | 46 |
| `structured_table` | 15 |
| `timestamp_domain` | 5 |
| `transform_record` | 12 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:8a9d3280dbcc` | unknown | unknown | 1787205600000000000 on `starting_time.nanoseconds_since_epoch` | 1787205605900000000 on `starting_time.nanoseconds_since_epoch` | 0 |
| `rec:a8f8b9365722` | unknown | unknown | 1787205600000000000 on `log_time` | 1787205605900000000 on `log_time` | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:2eff2d727014` | `rec:a8f8b9365722` | `/status` | `log_time`, `publish_time (/status)` | `3` | unknown | unknown |
| `rec:fb4c2e164774` | `rec:a8f8b9365722` | `/joint_states` | `log_time`, `publish_time (/joint_states)`, `header.stamp (/joint_states)` | `60` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:9673a984e5d7`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:ba2ff09bf5f6`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:3f6c23ebe497`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:664008adf4d6`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:53d68d01e0c9`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:cab2549c8d46`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:dc4552190c10`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:ef9a66ddeec5`
- **info** `neptune.clocks.latency_unbounded` (missing): 3 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:f78839ae7718`
- **info** `neptune.clocks.unsynchronised` (missing): the package's clocks form 2 groups that no clock mapping joins; times in different groups cannot be compared · `rec:c3402a3f8f6d`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:182da7bcdc08`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:250acb73c5ee`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:ba08f3bb74a9`

## Ambiguous fields

- `rec:0a54ce22c73e` `/key_tag`
- `rec:0f01183b1bfd` `/key_tag`
- `rec:b614ff5f0721` `/key_tag`
- `rec:c50b3f437fc3` `/key_tag`
