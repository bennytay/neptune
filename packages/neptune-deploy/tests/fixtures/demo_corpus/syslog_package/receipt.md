# Ingest receipt

Receipt `rec:sha256:36df4075a33eaec28de2feddb46462d498f73a22989ecdee66a6f46ad7f6731f`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 1 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `neptune.yaml` | 67 | `sha256:f4b6ca124fd7` | config 0.1.0 |
| `sites/PLANT-2/cell3/logs/syslog_LOG-P2_2026-09-14.csv` | 470 | `sha256:aa7fa9222733` | tabular 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:82c8176f7cc5` |
| `neptune.manifest` | `0.1.0` | `sha256:08a107411f7d` | none | `rec:37ab0dc03f73` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |

## Records

| Kind | Records |
|---|---|
| `configuration_snapshot` | 1 |
| `configuration_value` | 6 |
| `ingest_finding` | 1 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `structured_record` | 4 |
| `structured_table` | 1 |
| `transform_record` | 4 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:fa608d069b27`

## Ambiguous fields

None.
