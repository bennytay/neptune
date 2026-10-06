# Ingest receipt

Receipt `rec:sha256:a747fe95abacd8f88c41857469a18d25ba854ac9e164ff448c071760a98e9ca8`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 6 seen, 6 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 10
- Findings: 0 errors, 2 warnings, 3 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `neptune.yaml` | 67 | `sha256:f4b6ca124fd7` | config 0.1.0 |
| `sites/PLANT-2/cell3/incidents/INC-C3-0011.pdf` | 7915 | `sha256:bfc3c2e8e1d2` | pdf 0.1.0 |
| `sites/PLANT-2/cell3/requalification/requalification_tests.csv` | 987 | `sha256:30be864d249a` | tabular 0.2.0 |
| `sites/PLANT-2/cmms/work_orders.csv` | 2044 | `sha256:f4e37e7d8863` | neptune.declared 0.1.0, tabular 0.2.0 |
| `sites/S-007/incidents/INC-0007.pdf` | 6897 | `sha256:b793183fb43a` | pdf 0.1.0 |
| `sites/S-007/requalification/requalification_tests.csv` | 495 | `sha256:e1dd2870994f` | tabular 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `neptune.declared` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:25b9ad81b0e9` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:49c7aad296c5` |
| `neptune.manifest` | `0.1.0` | `sha256:08a107411f7d` | none | `rec:37ab0dc03f73` |
| `neptune.validate` | `0.2.0` | `sha256:1b5b07bc3453` | none | `rec:8fb79f219a03` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |

## Records

| Kind | Records |
|---|---|
| `asset` | 10 |
| `configuration_snapshot` | 1 |
| `configuration_value` | 6 |
| `document_block` | 32 |
| `document_record` | 2 |
| `ingest_finding` | 5 |
| `source_artifact` | 6 |
| `source_revision` | 6 |
| `structured_record` | 23 |
| `structured_table` | 5 |
| `transform_record` | 7 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|

## Entities

| Kind | Record | Stated ids |
|---|---|---|
| asset | `rec:18b04ed7a30a` | `asset:LEG-01` |
| asset | `rec:1bf4cba9cce5` | `asset:ARM-3A` |
| asset | `rec:3cd9db92496a` | `asset:ARM-3A` |
| asset | `rec:4153b9ddf6a4` | `asset:ARM-3A` |
| asset | `rec:4c5eee0c7699` | `asset:LEG-01` |
| asset | `rec:4ce14e691e2c` | `asset:ARM-3A` |
| asset | `rec:77d59af34ed0` | `asset:ARM-3A` |
| asset | `rec:82c56ab7dc61` | `asset:ARM-3A` |
| asset | `rec:8418cd43fcab` | `asset:ARM-3A` |
| asset | `rec:bae69a509f59` | `asset:ARM-3A` |

## Findings

- **warning** `neptune.validate.duplicate_id` (ambiguous): one source states asset id asset:ARM-3A for 8 records · `rec:36f8d6788f70`
- **warning** `neptune.validate.duplicate_id` (ambiguous): one source states asset id asset:LEG-01 for 2 records · `rec:e7b3e3c4386b`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:250acb73c5ee`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:fb65effe3f68`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:fe90dc8ecf30`

## Ambiguous fields

None.
