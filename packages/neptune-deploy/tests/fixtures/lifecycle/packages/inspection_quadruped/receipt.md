# Ingest receipt

Receipt `rec:sha256:67dae8c0d908bf8a34b83ee0f39864921ebd921beec40192f82de08402bec62c`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 1 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `maximo_workorders.csv` | 423 | `sha256:8e01bbee4e11` | tabular 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |

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
| `ingest_finding` | 1 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `structured_record` | 2 |
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

- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:81103b605fe9`

## Ambiguous fields

None.
