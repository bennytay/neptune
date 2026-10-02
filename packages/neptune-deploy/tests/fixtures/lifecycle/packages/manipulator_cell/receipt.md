# Ingest receipt

Receipt `rec:sha256:5798f08e6f63cf501f1934c34f9489beb3e1cb15c9b82a786b30bb57e2334cc8`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 6 seen, 6 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 1 warnings, 5 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `asset_register.csv` | 458 | `sha256:fc6ca743ac22` | tabular 0.2.0 |
| `linear_issues.csv` | 600 | `sha256:3d868c459df0` | tabular 0.2.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |
| `requalification_tests.csv` | 697 | `sha256:932783df2b0a` | tabular 0.2.0 |
| `risk_register.csv` | 499 | `sha256:fd0fd0b9134b` | tabular 0.2.0 |
| `servicenow_changes.csv` | 626 | `sha256:e1e4d0dbb024` | tabular 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:c91fc242cc67` |
| `neptune.manifest` | `0.1.0` | `sha256:3bedd387c711` | none | `rec:8a63ca51c7d3` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |

## Records

| Kind | Records |
|---|---|
| `configuration_snapshot` | 1 |
| `configuration_value` | 6 |
| `ingest_finding` | 6 |
| `source_artifact` | 6 |
| `source_revision` | 6 |
| `structured_record` | 9 |
| `structured_table` | 5 |
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

- **warning** `tabular.invalid_utf8` (unrepresentable): row 2's cells [14] are not UTF-8; they are Unknown · `rec:e02e9b6167e7`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:46408b1131fa`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:48b60d19d6d9`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:8884c05cd6ff`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:96c8db0c1fec`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:fd9e26592ddb`

## Ambiguous fields

None.
