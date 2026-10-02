# Ingest receipt

Receipt `rec:sha256:839e4ad3e3c23103cd2e386bbeba170e8204a7238fb7e60021a40221d24ab3d6`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 4 seen, 4 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 2 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `cmms_work_orders.csv` | 1029 | `sha256:d48d507967c9` | tabular 0.1.0 |
| `jira_incidents.json` | 1177 | `sha256:99b072271d56` | tabular 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |
| `zone_register.csv` | 500 | `sha256:0d069fc8cce8` | tabular 0.1.0 |

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
| `ingest_finding` | 2 |
| `source_artifact` | 4 |
| `source_revision` | 4 |
| `structured_record` | 11 |
| `structured_table` | 3 |
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

- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:89f5df415151`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:bba0885bbe7c`

## Ambiguous fields

None.
