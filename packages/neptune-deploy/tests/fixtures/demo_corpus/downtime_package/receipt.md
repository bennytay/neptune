# Ingest receipt

Receipt `rec:sha256:9c5a9bcf1751f2e4c018684083a2c9eb6a5c1744a6f1ed1605f2b16fec04eb81`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 3
- Findings: 0 errors, 1 warnings, 1 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `neptune.yaml` | 67 | `sha256:f4b6ca124fd7` | config 0.1.0 |
| `sites/PLANT-2/cmms/downtime_log.csv` | 545 | `sha256:3dc72a6d8664` | neptune.declared 0.1.0, tabular 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `neptune.declared` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:25b9ad81b0e9` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:82c8176f7cc5` |
| `neptune.manifest` | `0.1.0` | `sha256:08a107411f7d` | none | `rec:37ab0dc03f73` |
| `neptune.validate` | `0.2.0` | `sha256:1b5b07bc3453` | none | `rec:8fb79f219a03` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |

## Records

| Kind | Records |
|---|---|
| `asset` | 3 |
| `configuration_snapshot` | 1 |
| `configuration_value` | 6 |
| `ingest_finding` | 2 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `structured_record` | 3 |
| `structured_table` | 1 |
| `transform_record` | 6 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|

## Entities

| Kind | Record | Stated ids |
|---|---|---|
| asset | `rec:0daae1fe0605` | `asset:ARM-3A` |
| asset | `rec:8c5e7dade3b2` | `asset:ARM-3A` |
| asset | `rec:b1a301b4cec8` | `asset:ARM-3A` |

## Findings

- **warning** `neptune.validate.duplicate_id` (ambiguous): one source states asset id asset:ARM-3A for 3 records · `rec:7bf93045f919`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:6b301366d049`

## Ambiguous fields

None.
