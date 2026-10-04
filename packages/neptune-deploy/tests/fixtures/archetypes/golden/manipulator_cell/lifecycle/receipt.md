# Ingest receipt

Receipt `rec:sha256:716d0d3fee0439eef72b86376585be4f8da5e1492e0f07484b416f3498f7d5ba`. Every id below is shortened; `receipt.json` has them whole.

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
| `deploy_document_map` | `0.1.0` | `sha256:1bef46efb69b` | none | `rec:2f2bbb22ec87` |
| `deploy_document_map` | `0.1.0` | `sha256:64f87ddca364` | none | `rec:be8a0a553fd3` |
| `deploy_document_map` | `0.1.0` | `sha256:82620e22ad64` | none | `rec:c09789f91f8b` |
| `deploy_document_map` | `0.1.0` | `sha256:9757dfc564da` | none | `rec:ce771174155b` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:ad0f4dcb93b8` | none | `rec:14f08a840396` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:35be0085fecb` | none | `rec:167b843c9570` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:7c9d196fd1df` | none | `rec:2cbb9786da14` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:c920b699cec1` | none | `rec:c026d9026675` |
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

- **warning** `deploy_document_map.column_unmapped` (unsupported): columns of a table the template reads that no field reads and the template does not ignore; their cells stay in the base package only · `rec:3c588a0fb541`
- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:cfb7545ea0fd`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:7f64bb245ec2`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:e8eb57cdfc5d`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:d70e1376a256`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:9dfcfe8d1002`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:14d1e0b88dc2`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:7d5c6365a9be`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:af699cb1a375`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:16ad665764fb`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:ac64c9b8308b`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:bc25e8972db3`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:e061b383efe1`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:e9940e6551c0`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:370a42109b19`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:b45e402090c7`

## Ambiguous fields

None.
