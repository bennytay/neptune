# Ingest receipt

Receipt `rec:sha256:578723c8fb1c514e02bd1422fb6e88f325f1c67b4e813dc3f3f7a3f3bbf83b64`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 15 seen, 9 read, 6 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 5 warnings, 12 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bags/pick_place_2026-08-20/metadata.yaml` | 1480 | `sha256:6af83e864da1` | deploy_lifecycle_map 0.1.0 |
| `bags/pick_place_2026-08-20/pick_place_2026-08-20_0.mcap` | 22273 | `sha256:a6eff07ae440` | not read |
| `calibration/CAL-ARM3A-0226.yaml` | 313 | `sha256:fecc413f15fd` | not read |
| `calibration/CAL-ARM3A-0415.yaml` | 326 | `sha256:7734a681a3f1` | not read |
| `calibration/CAL-ARM3A-0623.yaml` | 334 | `sha256:530ef05aaa16` | not read |
| `calibration/CAL-ARM3A-0818.yaml` | 334 | `sha256:a2d19ecfbcda` | not read |
| `changes/servicenow_changes.csv` | 626 | `sha256:fdca33396278` | deploy_lifecycle_map 0.1.0 |
| `cmms/work_orders.csv` | 950 | `sha256:96d3810cd8c7` | deploy_lifecycle_map 0.1.0 |
| `documents/commissioning_CR-C3-2026-02.pdf` | 12618 | `sha256:5e9fde08afd8` | deploy_document_map 0.1.0 |
| `documents/risk_assessment_CELL3-RA-009.pdf` | 11475 | `sha256:9929112c0ec5` | deploy_document_map 0.1.0 |
| `documents/sop_CELL-014_finger_set.pdf` | 6551 | `sha256:962616825f67` | deploy_document_map 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | not read |
| `requalification/requalification_tests.csv` | 987 | `sha256:30be864d249a` | deploy_lifecycle_map 0.1.0 |
| `tickets/near_miss_export.json` | 1046 | `sha256:a48873e408ee` | deploy_lifecycle_map 0.1.0 |
| `urdf/arm6.urdf` | 1767 | `sha256:7f79c49b41ef` | deploy_document_map 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `deploy_document_map` | `0.1.0` | `sha256:17384accaf86` | none | `rec:0a6a4ddf7a28` |
| `deploy_document_map` | `0.1.0` | `sha256:6644114fe3ec` | none | `rec:115eeff3078e` |
| `deploy_document_map` | `0.1.0` | `sha256:f86e810ba05a` | none | `rec:207149662093` |
| `deploy_document_map` | `0.1.0` | `sha256:ddc4fe1ec754` | none | `rec:b701a79a4f8d` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:77409b97fb27` | none | `rec:170f79877475` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:aca52033985f` | none | `rec:42a32b4fb001` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:2e8ca5a5bac3` | none | `rec:95182805ae08` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:829fe9065bd3` | none | `rec:9da1ddd521fa` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:e9fba51ce09f` | none | `rec:b40b54108fd0` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `rosbag2` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:bf370f1dddcb` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `change_record` | 2 |
| `commissioning_baseline` | 1 |
| `incident_record` | 1 |
| `ingest_finding` | 17 |
| `maintenance_event` | 5 |
| `requalification_record` | 3 |
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

- **warning** `deploy_document_map.column_unmapped` (unsupported): columns of a table the template reads that no field reads and the template does not ignore; their cells stay in the base package only · `rec:c0279a93612b`
- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:278ceb96e4e2`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:27217af038d8`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:662f4745b004`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:50fa13abe8a5`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:78fdfefa5917`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:a2a23fe23132`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:b1fe8cb044c0`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:f8288101e03a`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:cb2c68de88da`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:e77f72a11d9b`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:16e0adefd65b`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:e0f27cdb1455`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:107e62b94e24`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:6569b9da01db`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:6ee958cc97ca`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:f2fe0d03cfb2`

## Ambiguous fields

None.
