# Ingest receipt

Receipt `rec:sha256:3cb0a0871cbca13cee22381374316429700104364b476b5e2f66539fd4856b7d`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 6 seen, 6 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 1 warnings, 5 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `asset_register.csv` | 458 | `sha256:fc6ca743ac22` | tabular 0.1.0 |
| `linear_issues.csv` | 600 | `sha256:3d868c459df0` | tabular 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |
| `requalification_tests.csv` | 697 | `sha256:932783df2b0a` | tabular 0.1.0 |
| `risk_register.csv` | 499 | `sha256:fd0fd0b9134b` | tabular 0.1.0 |
| `servicenow_changes.csv` | 626 | `sha256:e1e4d0dbb024` | tabular 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:d268e329090f` |
| `neptune.manifest` | `0.1.0` | `sha256:3bedd387c711` | none | `rec:8a63ca51c7d3` |
| `tabular` | `0.1.0` | `sha256:15487b4cb47e` | pyarrow 25.0.1 | `rec:b4754c46f7fb` |

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

- **warning** `tabular.invalid_utf8` (unrepresentable): row 2's cells [14] are not UTF-8; they are Unknown · `rec:081b506852c1`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:2c1f9eb135df`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:32cdaa037c87`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:bae0bd156e0c`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:f31ba0a752f8`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:f8a4943e1435`

## Ambiguous fields

None.
