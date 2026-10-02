# Ingest receipt

Receipt `rec:sha256:5ae0d734d909d6c5013e9d61fc116fbc6b5ca931572016fc35c68b1391cfa70b`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 25 seen, 10 read, 15 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 13 warnings, 13 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `authorisation/zone_register.csv` | 788 | `sha256:4b797207931a` | deploy_lifecycle_map 0.1.0 |
| `changes/servicenow_changes.csv` | 1618 | `sha256:2b7b4b194dd1` | deploy_lifecycle_map 0.1.0 |
| `cmms/work_orders.csv` | 1978 | `sha256:46a7c074ef28` | deploy_lifecycle_map 0.1.0 |
| `config/AMR-05/nav2_params.yaml` | 405 | `sha256:6495d96ee25f` | not read |
| `config/AMR-06/nav2_params.yaml` | 405 | `sha256:7f04720e8e8b` | not read |
| `config/AMR-07/nav2_params.yaml` | 405 | `sha256:9e0b456f5606` | not read |
| `config/AMR-08/nav2_params.yaml` | 405 | `sha256:171f2ff18c14` | not read |
| `config/AMR-09/nav2_params.yaml` | 424 | `sha256:daf6a02ed856` | not read |
| `config/AMR-10/nav2_params.yaml` | 405 | `sha256:0ea560b788ce` | not read |
| `incidents/INC-0007.pdf` | 6897 | `sha256:b793183fb43a` | deploy_document_map 0.1.0 |
| `incidents/INC-0013.pdf` | 6416 | `sha256:c33a7d5acbe3` | deploy_document_map 0.1.0 |
| `maps/S-007_zones.geojson` | 1393 | `sha256:83d1dc3347fe` | deploy_lifecycle_map 0.1.0 |
| `maps/S-012_zones.geojson` | 1394 | `sha256:22a6767e99ac` | deploy_lifecycle_map 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | not read |
| `requalification/requalification_tests.csv` | 785 | `sha256:82e663e574ca` | deploy_lifecycle_map 0.1.0 |
| `runs/S-007/amr-05_2026-03-03.mcap` | 17400 | `sha256:08528ca2ef6f` | not read |
| `runs/S-007/amr-06_2026-03-03.mcap` | 17515 | `sha256:b80fb68ef024` | not read |
| `runs/S-007/amr-07_2026-04-02.mcap` | 17516 | `sha256:34bb6772f2fd` | not read |
| `runs/S-007/amr-07_2026-04-15.mcap` | 17400 | `sha256:e1987a0b1303` | not read |
| `runs/S-012/amr-08_2026-05-19/amr-08_2026-05-19_0.mcap` | 10088 | `sha256:9ce9e303f7db` | not read |
| `runs/S-012/amr-08_2026-05-19/metadata.yaml` | 1940 | `sha256:6a01ea04e416` | not read |
| `runs/S-012/amr-09_2026-03-05.mcap` | 17400 | `sha256:e7e0782cb047` | not read |
| `runs/S-012/amr-10_2026-03-06.mcap` | 17515 | `sha256:a217da2d2bc6` | not read |
| `urdf/lift_150.urdf` | 1757 | `sha256:4ed9c552ac68` | deploy_document_map 0.1.0 |
| `urdf/tug_200.urdf` | 1475 | `sha256:15e99b9d0356` | deploy_document_map 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `deploy_document_map` | `0.1.0` | `sha256:d5f47705eb6c` | none | `rec:11841aba6ec0` |
| `deploy_document_map` | `0.1.0` | `sha256:506f5fe0558a` | none | `rec:fcfb9fd53db6` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:bb980db804ee` | none | `rec:3df466751838` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:acc01325f050` | none | `rec:565fa9da5246` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:194422d5aca2` | none | `rec:713b6d848663` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:23cb09099f0b` | none | `rec:9a4e05799805` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:728fdf972685` | none | `rec:be548febf36a` |
| `geojson` | `0.1.0` | `sha256:542c42fe020a` | none | `rec:2c0e605ea2be` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `authorisation_envelope` | 4 |
| `change_record` | 6 |
| `incident_record` | 2 |
| `ingest_finding` | 26 |
| `maintenance_event` | 12 |
| `requalification_record` | 2 |
| `source_artifact` | 25 |
| `source_revision` | 25 |
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

- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:5acb6cedfbdd`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:4315df165a82`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:53f59ecd6ebb`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:73394bc3d20a`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:77f205f820c8`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:88b343d1d1f8`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:a6ec81d7010b`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:d328c6b62137`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:dad3eb766023`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:521da2e86cc5`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:ab016fbdf9f9`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:cd399de40d5f`
- **warning** `deploy_lifecycle_map.value_unreadable` (inconsistent): cells that do not read as their field's declared shape or format; the fields are unknown · `rec:718e899a0346`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:202dc3a22157`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:306b6f720828`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:4330e0d90e57`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:720ae2941267`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:1e76e2315c8e`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:6b525ad98916`
- **info** `deploy_lifecycle_map.fields_not_covered` (missing): fields of a rule's lifecycle kind that the rule does not read, in every record it made: an unread value is not covered, and an unread list is empty without stating none · `rec:e20825e5436f`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:05a0255063e5`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:3be373343a7d`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:240d02f3099b`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:5311ee26045a`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:dd5219380ba5`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:f274dd313a63`

## Ambiguous fields

None.
