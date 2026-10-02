# Ingest receipt

Receipt `rec:sha256:6d4b6a9045ba8da8355b37d29763daceecd7b92e61b18474f97521fbbf1f5b7a`. Every id below is shortened; `receipt.json` has them whole.

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
| `deploy_document_map` | `0.1.0` | `sha256:48607c6eca80` | none | `rec:219f17ec9a5d` |
| `deploy_document_map` | `0.1.0` | `sha256:a432eac4ad29` | none | `rec:432102f7e25f` |
| `deploy_document_map` | `0.1.0` | `sha256:d27273d8c7c8` | none | `rec:91234fe81d6c` |
| `deploy_document_map` | `0.1.0` | `sha256:39766076659d` | none | `rec:b24b42479b8b` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:cd26a0af5998` | none | `rec:7809a8616776` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:12a7f7147663` | none | `rec:7cffc174eb8d` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:771e01f984be` | none | `rec:7f1025805b37` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:f3fae0fec062` | none | `rec:83d825c6318d` |
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

- **warning** `deploy_document_map.column_unmapped` (unsupported): columns of a table the template reads that no field reads and the template does not ignore; their cells stay in the base package only · `rec:f4441aa02d00`
- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:13155927a1ee`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:431a37ad9c9e`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:53d34f26db49`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:025fa208d11c`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:f1630009a01b`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:4688bbb806e3`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:4e555150e5d6`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:d26f27564187`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:6645d374da79`
- **info** `deploy_document_map.text_unread` (unsupported): text and tables of a matched document that no field of the template reads; they stay in the base package only · `rec:969c94422cfc`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:1f9b752a923f`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:8ba348945a91`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:a02cc3bb14ca`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:3fb3582d190f`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:71652f82ea3b`

## Ambiguous fields

None.
