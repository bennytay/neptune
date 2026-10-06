# Ingest receipt

Receipt `rec:sha256:3fcb119030a41dd00abb8dc12fbc62815ac1e2c9daf37f4c8f934a8860cdf047`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 15 seen, 8 read, 7 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 4 warnings, 11 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bags/pick_place_2026-08-20/metadata.yaml` | 1480 | `sha256:6af83e864da1` | not read |
| `bags/pick_place_2026-08-20/pick_place_2026-08-20_0.mcap` | 22273 | `sha256:a6eff07ae440` | not read |
| `calibration/CAL-ARM3A-0226.yaml` | 313 | `sha256:fecc413f15fd` | not read |
| `calibration/CAL-ARM3A-0415.yaml` | 326 | `sha256:7734a681a3f1` | not read |
| `calibration/CAL-ARM3A-0623.yaml` | 334 | `sha256:530ef05aaa16` | not read |
| `calibration/CAL-ARM3A-0818.yaml` | 334 | `sha256:a2d19ecfbcda` | not read |
| `changes/servicenow_changes.csv` | 626 | `sha256:fdca33396278` | deploy_lifecycle_map 0.3.0 |
| `cmms/work_orders.csv` | 950 | `sha256:96d3810cd8c7` | deploy_lifecycle_map 0.3.0 |
| `documents/commissioning_CR-C3-2026-02.pdf` | 12618 | `sha256:5e9fde08afd8` | deploy_document_map 0.3.0 |
| `documents/risk_assessment_CELL3-RA-009.pdf` | 11475 | `sha256:9929112c0ec5` | deploy_document_map 0.3.0 |
| `documents/sop_CELL-014_finger_set.pdf` | 6551 | `sha256:962616825f67` | deploy_document_map 0.3.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | not read |
| `requalification/requalification_tests.csv` | 987 | `sha256:30be864d249a` | deploy_lifecycle_map 0.3.0 |
| `tickets/near_miss_export.json` | 1046 | `sha256:a48873e408ee` | deploy_lifecycle_map 0.3.0 |
| `urdf/arm6.urdf` | 1767 | `sha256:7f79c49b41ef` | deploy_document_map 0.3.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `deploy_document_map` | `0.3.0` | `sha256:66f0d68e6646` | none | `rec:3b5208a5c964` |
| `deploy_document_map` | `0.3.0` | `sha256:10d2e7853316` | none | `rec:42454900a18f` |
| `deploy_document_map` | `0.3.0` | `sha256:67ef41692d04` | none | `rec:503ee237a682` |
| `deploy_document_map` | `0.3.0` | `sha256:c629d0ba9c9c` | none | `rec:b47e3a5791f5` |
| `deploy_lifecycle_map` | `0.3.0` | `sha256:05451e1127ea` | none | `rec:5059c0d9fd5b` |
| `deploy_lifecycle_map` | `0.3.0` | `sha256:fccc9e52e714` | none | `rec:6ffcc8656950` |
| `deploy_lifecycle_map` | `0.3.0` | `sha256:1963812978dd` | none | `rec:c2c894361c2a` |
| `deploy_lifecycle_map` | `0.3.0` | `sha256:c16992b5ced4` | none | `rec:ed05a4098e01` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `change_record` | 2 |
| `civil_time_zone` | 11 |
| `commissioning_baseline` | 1 |
| `incident_record` | 1 |
| `ingest_finding` | 15 |
| `maintenance_event` | 6 |
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

- **warning** `deploy_document_map.column_unmapped` (unsupported): columns of a table the template reads that no field reads and the template does not ignore; their cells stay in the base package only · `rec:c32873ddb5e2`
- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:5c82463e3a63`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:44e0ef88e5f6`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:bd2c1416ac07`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:d7c4463a75c5`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:1f1c5bc6ca63`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:8903674ab7c2`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:e47e99df3901`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:4e56f9521250`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:f598390a0b7b`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value or list is not covered, never read as none · `rec:3c295fb611c7`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value or list is not covered, never read as none · `rec:66d5661d2b9f`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value or list is not covered, never read as none · `rec:7403cce17009`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:25f39ac02037`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:8223a9d6f07d`

## Ambiguous fields

None.
