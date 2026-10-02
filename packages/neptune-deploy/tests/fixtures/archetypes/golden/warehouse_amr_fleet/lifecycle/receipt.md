# Ingest receipt

Receipt `rec:sha256:aa9d77da51b713d8ff173b76f6196ed3dd37876d787aee16de5748021263b921`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 25 seen, 11 read, 14 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 13 warnings, 12 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `authorisation/zone_register.csv` | 788 | `sha256:4b797207931a` | deploy_lifecycle_map 0.1.0 |
| `changes/servicenow_changes.csv` | 1618 | `sha256:2b7b4b194dd1` | deploy_lifecycle_map 0.1.0 |
| `cmms/work_orders.csv` | 1841 | `sha256:184e31cadd41` | deploy_lifecycle_map 0.1.0 |
| `config/AMR-05/nav2_params.yaml` | 405 | `sha256:6495d96ee25f` | not read |
| `config/AMR-06/nav2_params.yaml` | 405 | `sha256:7f04720e8e8b` | not read |
| `config/AMR-07/nav2_params.yaml` | 405 | `sha256:9e0b456f5606` | not read |
| `config/AMR-08/nav2_params.yaml` | 405 | `sha256:171f2ff18c14` | not read |
| `config/AMR-09/nav2_params.yaml` | 424 | `sha256:daf6a02ed856` | not read |
| `config/AMR-10/nav2_params.yaml` | 405 | `sha256:0ea560b788ce` | not read |
| `incidents/INC-0007.pdf` | 6897 | `sha256:b793183fb43a` | deploy_document_map 0.1.0 |
| `incidents/INC-0013.pdf` | 6416 | `sha256:c33a7d5acbe3` | deploy_document_map 0.1.0 |
| `maps/S-007_zones.geojson` | 1393 | `sha256:83d1dc3347fe` | deploy_document_map 0.1.0 |
| `maps/S-012_zones.geojson` | 1394 | `sha256:22a6767e99ac` | deploy_document_map 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | not read |
| `requalification/requalification_tests.csv` | 785 | `sha256:78f1799105e9` | deploy_lifecycle_map 0.1.0 |
| `runs/S-007/amr-05_2026-03-03.mcap` | 16942 | `sha256:b2b1499057fb` | not read |
| `runs/S-007/amr-06_2026-03-03.mcap` | 17057 | `sha256:7595ffcdd0c9` | not read |
| `runs/S-007/amr-07_2026-04-02.mcap` | 17058 | `sha256:c27e609862cb` | not read |
| `runs/S-007/amr-07_2026-04-15.mcap` | 16942 | `sha256:be78d4d4d2ea` | not read |
| `runs/S-012/amr-08_2026-05-19/amr-08_2026-05-19_0.mcap` | 10088 | `sha256:9ce9e303f7db` | not read |
| `runs/S-012/amr-08_2026-05-19/metadata.yaml` | 1940 | `sha256:6a01ea04e416` | deploy_lifecycle_map 0.1.0 |
| `runs/S-012/amr-09_2026-03-05.mcap` | 16942 | `sha256:210280b3b1b3` | not read |
| `runs/S-012/amr-10_2026-03-06.mcap` | 17057 | `sha256:3810579592a0` | not read |
| `urdf/lift_150.urdf` | 1757 | `sha256:4ed9c552ac68` | deploy_document_map 0.1.0 |
| `urdf/tug_200.urdf` | 1475 | `sha256:15e99b9d0356` | deploy_document_map 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `deploy_document_map` | `0.1.0` | `sha256:e75e1879f0b0` | none | `rec:0a994c16736e` |
| `deploy_document_map` | `0.1.0` | `sha256:2d0e39fee009` | none | `rec:ec11b41da4e1` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:3f70a2fd58e3` | none | `rec:837d0868049f` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:40eaa8d296cf` | none | `rec:867b9af3211d` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:bf3b8cdd268d` | none | `rec:a7f7a1007f8c` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:d8cf61b3bdcd` | none | `rec:afa4821c8429` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:c063a04d85a1` | none | `rec:e40a95ddb8af` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `rosbag2` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:bf370f1dddcb` |
| `tabular` | `0.1.0` | `sha256:15487b4cb47e` | pyarrow 25.0.1 | `rec:b4754c46f7fb` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `authorisation_envelope` | 4 |
| `change_record` | 6 |
| `incident_record` | 2 |
| `ingest_finding` | 25 |
| `maintenance_event` | 11 |
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

- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:31815de49e75`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:159a55109c7e`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:5d386be20d4b`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:881c67f746c2`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:a24fa2242d44`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:dd5c114d7d9c`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:dfc2fc2c76dd`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:f3ce1724fb8c`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:ffeb4a0385c6`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:e76215bd7ee1`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:7c13eb73c96d`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:b169871c9387`
- **warning** `deploy_lifecycle_map.value_unreadable` (inconsistent): cells that do not read as their field's declared shape or format; the fields are unknown · `rec:0657e14eca70`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:32d35b30b31a`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:3f856d932ed7`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:9feb52b53021`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:e9b162bcc47e`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:9655dffde423`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:b093016f3fd6`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:0ae92cce9014`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:b0c71c09833e`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:31f6ae774078`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:3e5e6b5ae582`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:a0527848bac5`
- **info** `deploy_lifecycle_map.table_unmapped` (unsupported): tables no mapping applies to; they have no lifecycle records · `rec:bda029b5473a`

## Ambiguous fields

None.
