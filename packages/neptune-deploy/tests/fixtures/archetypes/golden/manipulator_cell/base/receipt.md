# Ingest receipt

Receipt `rec:sha256:3e5bb8b750b54c402a812ac3da48d986f963c4275d2a389adb19fbf607c54104`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 15 seen, 15 read, 0 not read, 0 gone
- Runs: 2; streams: 2; entities: 0
- Findings: 0 errors, 0 warnings, 5 info; ambiguous fields: 4

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bags/pick_place_2026-08-20/metadata.yaml` | 1480 | `sha256:6af83e864da1` | rosbag2 0.1.0 |
| `bags/pick_place_2026-08-20/pick_place_2026-08-20_0.mcap` | 22273 | `sha256:a6eff07ae440` | mcap 0.1.0 |
| `calibration/CAL-ARM3A-0226.yaml` | 313 | `sha256:9cdd3c1c5b0e` | config 0.1.0 |
| `calibration/CAL-ARM3A-0415.yaml` | 326 | `sha256:1436918ee317` | config 0.1.0 |
| `calibration/CAL-ARM3A-0624.yaml` | 334 | `sha256:81e9b4dc7a44` | config 0.1.0 |
| `calibration/CAL-ARM3A-0819.yaml` | 334 | `sha256:4f1dc1c19791` | config 0.1.0 |
| `changes/servicenow_changes.csv` | 626 | `sha256:7efd961f9db9` | tabular 0.1.0 |
| `cmms/work_orders.csv` | 811 | `sha256:c36d3fa05fe5` | tabular 0.1.0 |
| `documents/commissioning_CR-C3-2026-02.pdf` | 12165 | `sha256:dd50aee23949` | pdf 0.1.0 |
| `documents/risk_assessment_CELL3-RA-009.pdf` | 11475 | `sha256:9929112c0ec5` | pdf 0.1.0 |
| `documents/sop_CELL-014_finger_set.pdf` | 6551 | `sha256:962616825f67` | pdf 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |
| `requalification/requalification_tests.csv` | 693 | `sha256:e1c5c8f68720` | tabular 0.1.0 |
| `tickets/near_miss_export.json` | 1046 | `sha256:85481ff19cb5` | tabular 0.1.0 |
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
| `structured_record` | 43 |
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
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:5d47fbc866cb`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:6e450bc05e9e`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:8afb133d6173`

## Ambiguous fields

- `rec:0a624b8c8bf8` `/key_tag`
- `rec:15bfb6860f77` `/key_tag`
- `rec:4fdad65a74d6` `/key_tag`
- `rec:805820c2358b` `/key_tag`
