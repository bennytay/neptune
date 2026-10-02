# Ingest receipt

Receipt `rec:sha256:c4048ec20214090a4f2764d236aa829ce846b8831eb483d9d6e9edc382f810e2`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 15 seen, 15 read, 0 not read, 0 gone
- Runs: 2; streams: 2; entities: 0
- Findings: 0 errors, 0 warnings, 5 info; ambiguous fields: 4

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bags/pick_place_2026-08-20/metadata.yaml` | 1480 | `sha256:6af83e864da1` | rosbag2 0.1.0 |
| `bags/pick_place_2026-08-20/pick_place_2026-08-20_0.mcap` | 22273 | `sha256:a6eff07ae440` | mcap 0.1.0 |
| `calibration/CAL-ARM3A-0226.yaml` | 313 | `sha256:fecc413f15fd` | config 0.1.0 |
| `calibration/CAL-ARM3A-0415.yaml` | 326 | `sha256:7734a681a3f1` | config 0.1.0 |
| `calibration/CAL-ARM3A-0623.yaml` | 334 | `sha256:530ef05aaa16` | config 0.1.0 |
| `calibration/CAL-ARM3A-0818.yaml` | 334 | `sha256:a2d19ecfbcda` | config 0.1.0 |
| `changes/servicenow_changes.csv` | 626 | `sha256:fdca33396278` | tabular 0.1.0 |
| `cmms/work_orders.csv` | 950 | `sha256:96d3810cd8c7` | tabular 0.1.0 |
| `documents/commissioning_CR-C3-2026-02.pdf` | 12618 | `sha256:5e9fde08afd8` | pdf 0.1.0 |
| `documents/risk_assessment_CELL3-RA-009.pdf` | 11475 | `sha256:9929112c0ec5` | pdf 0.1.0 |
| `documents/sop_CELL-014_finger_set.pdf` | 6551 | `sha256:962616825f67` | pdf 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |
| `requalification/requalification_tests.csv` | 987 | `sha256:30be864d249a` | tabular 0.1.0 |
| `tickets/near_miss_export.json` | 1046 | `sha256:a48873e408ee` | tabular 0.1.0 |
| `urdf/arm6.urdf` | 1767 | `sha256:7f79c49b41ef` | text 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `mcap` | `0.1.0` | `sha256:f42c37ac73d6` | lz4 4.4.5, zstandard 0.25.0 | `rec:89b0c5c3630b` |
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:d268e329090f` |
| `neptune.manifest` | `0.1.0` | `sha256:3bedd387c711` | none | `rec:8a63ca51c7d3` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `rosbag2` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:bf370f1dddcb` |
| `tabular` | `0.1.0` | `sha256:15487b4cb47e` | pyarrow 25.0.1 | `rec:b4754c46f7fb` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `configuration_snapshot` | 5 |
| `configuration_value` | 66 |
| `document_block` | 43 |
| `document_record` | 4 |
| `ingest_finding` | 5 |
| `run` | 2 |
| `source_artifact` | 15 |
| `source_revision` | 15 |
| `stream` | 2 |
| `structured_record` | 46 |
| `structured_table` | 15 |
| `timestamp_domain` | 4 |
| `transform_record` | 8 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:7e8a582df9fd` | unknown | unknown | 1787205600000000000 on `log_time` | 1787205605900000000 on `log_time` | 2 |
| `rec:d343ad3ffdb1` | unknown | unknown | 1787205600000000000 on `starting_time.nanoseconds_since_epoch` | 1787205605900000000 on `starting_time.nanoseconds_since_epoch` | 0 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:cdfaa801adc6` | `rec:7e8a582df9fd` | `/status` | `log_time`, `publish_time (/status)` | `3` | unknown | unknown |
| `rec:d5df76b7237d` | `rec:7e8a582df9fd` | `/joint_states` | `log_time`, `publish_time (/joint_states)` | `60` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:09e5a12ecc04`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:9d5585a08e16`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:d7e9625a4618`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:eafdcc15527a`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:f31833fd5408`

## Ambiguous fields

- `rec:0a54ce22c73e` `/key_tag`
- `rec:0f01183b1bfd` `/key_tag`
- `rec:b614ff5f0721` `/key_tag`
- `rec:c50b3f437fc3` `/key_tag`
