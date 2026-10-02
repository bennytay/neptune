# Ingest receipt

Receipt `rec:sha256:474368b892cf44e591b2afd33741555d584799000f909ac1c7db337c2f661b8c`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 15 seen, 8 read, 7 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 5 warnings, 11 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bags/pick_place_2026-08-20/metadata.yaml` | 1480 | `sha256:6af83e864da1` | not read |
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
| `deploy_document_map` | `0.1.0` | `sha256:9d2db01c581e` | none | `rec:134ec9dfa255` |
| `deploy_document_map` | `0.1.0` | `sha256:2f0436f85a92` | none | `rec:7b31a0d2c1f2` |
| `deploy_document_map` | `0.1.0` | `sha256:b927c13138ba` | none | `rec:c16db17f5fd5` |
| `deploy_document_map` | `0.1.0` | `sha256:f4935d879ab5` | none | `rec:fbc96e54f2d0` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:86bc0dde6d4a` | none | `rec:243fee9b289c` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:2dfa290f9698` | none | `rec:2ca023f41fce` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:2d9ea03c98ab` | none | `rec:41b576515b3f` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:15c3e9f784cd` | none | `rec:85a4d45c4da1` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `change_record` | 2 |
| `commissioning_baseline` | 1 |
| `incident_record` | 1 |
| `ingest_finding` | 16 |
| `maintenance_event` | 5 |
| `requalification_record` | 3 |
| `risk_assessment` | 1 |
| `source_artifact` | 15 |
| `source_revision` | 15 |
| `timestamp_domain` | 12 |
| `transform_record` | 11 |

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

- **warning** `deploy_document_map.column_unmapped` (unsupported): columns of a table the template reads that no field reads and the template does not ignore; their cells stay in the base package only · `rec:d43e3069cb4d`
- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:8a815d8da013`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:1562cc42605d`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:9067e31f018e`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:1dc13ba7e508`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:46d3eb126571`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:33e6adc96562`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:6bdf9de23cb7`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:73b6c7493ab3`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:1aac24caa7af`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:a720e83a2649`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:28e43b1b8abf`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:886b70204977`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:df3051298411`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:7a61fab23a56`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:ce3b8cf0ebb4`

## Ambiguous fields

None.
