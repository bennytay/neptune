# Ingest receipt

Receipt `rec:sha256:399f5a0588cf02c6799c5b8299e5b0e97e5ffd69b4047c5f0af3982c305d18b0`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 15 seen, 9 read, 6 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 5 warnings, 12 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bags/pick_place_2026-08-20/metadata.yaml` | 1480 | `sha256:6af83e864da1` | deploy_lifecycle_map 0.1.0 |
| `bags/pick_place_2026-08-20/pick_place_2026-08-20_0.mcap` | 22273 | `sha256:a6eff07ae440` | not read |
| `calibration/CAL-ARM3A-0226.yaml` | 313 | `sha256:9cdd3c1c5b0e` | not read |
| `calibration/CAL-ARM3A-0415.yaml` | 326 | `sha256:1436918ee317` | not read |
| `calibration/CAL-ARM3A-0624.yaml` | 334 | `sha256:81e9b4dc7a44` | not read |
| `calibration/CAL-ARM3A-0819.yaml` | 334 | `sha256:4f1dc1c19791` | not read |
| `changes/servicenow_changes.csv` | 626 | `sha256:7efd961f9db9` | deploy_lifecycle_map 0.1.0 |
| `cmms/work_orders.csv` | 811 | `sha256:c36d3fa05fe5` | deploy_lifecycle_map 0.1.0 |
| `documents/commissioning_CR-C3-2026-02.pdf` | 12165 | `sha256:dd50aee23949` | deploy_document_map 0.1.0 |
| `documents/risk_assessment_CELL3-RA-009.pdf` | 11475 | `sha256:9929112c0ec5` | deploy_document_map 0.1.0 |
| `documents/sop_CELL-014_finger_set.pdf` | 6551 | `sha256:962616825f67` | deploy_document_map 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | not read |
| `requalification/requalification_tests.csv` | 693 | `sha256:e1c5c8f68720` | deploy_lifecycle_map 0.1.0 |
| `tickets/near_miss_export.json` | 1046 | `sha256:85481ff19cb5` | deploy_lifecycle_map 0.1.0 |
| `urdf/arm6.urdf` | 1767 | `sha256:7f79c49b41ef` | deploy_document_map 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `deploy_document_map` | `0.1.0` | `sha256:4642212d79f0` | none | `rec:342bbdbd3c05` |
| `deploy_document_map` | `0.1.0` | `sha256:9cc0afb1f455` | none | `rec:36a6cb31270a` |
| `deploy_document_map` | `0.1.0` | `sha256:0335a5162086` | none | `rec:e5636008fad7` |
| `deploy_document_map` | `0.1.0` | `sha256:efed6020c8dd` | none | `rec:fa1cfc53fedd` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:fd08be926bf4` | none | `rec:62b29c423ff5` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:b9d1bdad2833` | none | `rec:7306efa75de1` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:5b40022a07ce` | none | `rec:8f4101acf872` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:b8b8bba704f9` | none | `rec:b723df118ac5` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:c524e996c481` | none | `rec:fff3c0bfe772` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `rosbag2` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:bf370f1dddcb` |
| `tabular` | `0.1.0` | `sha256:15487b4cb47e` | pyarrow 25.0.1 | `rec:b4754c46f7fb` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `change_record` | 2 |
| `commissioning_baseline` | 1 |
| `incident_record` | 1 |
| `ingest_finding` | 17 |
| `maintenance_event` | 4 |
| `requalification_record` | 2 |
| `risk_assessment` | 1 |
| `source_artifact` | 15 |
| `source_revision` | 15 |
| `timestamp_domain` | 12 |
| `transform_record` | 13 |

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

- **warning** `deploy_document_map.column_unmapped` (unsupported): columns of a table the template reads that no field reads and the template does not ignore; their cells stay in the base package only · `rec:823de0b63897`
- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:58a02acc05c8`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:5cef56551fe9`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:9a7376f01973`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:3bc21b224480`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:4fd43c6b975f`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:1ded32c8ec81`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:52b6f40ab3ac`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:e19c2295ad64`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:3fbd91a29410`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:4d98d8ac0623`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:4415b289f645`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:90ee6e70d251`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:0525828139cb`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:739e59d24cfd`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:8012b7e6aeb9`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:d83071ca55be`

## Ambiguous fields

None.
