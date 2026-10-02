# Ingest receipt

Receipt `rec:sha256:f93812286d5cfe5dcdf1dc748354e700e4bda9c5dd622a6c9a4ad28ca30719ca`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 25 seen, 10 read, 15 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 13 warnings, 8 info; ambiguous fields: 0

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
| `maps/S-007_zones.geojson` | 1393 | `sha256:83d1dc3347fe` | deploy_document_map 0.1.0 |
| `maps/S-012_zones.geojson` | 1394 | `sha256:22a6767e99ac` | deploy_document_map 0.1.0 |
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
| `deploy_document_map` | `0.1.0` | `sha256:1844c57cf199` | none | `rec:65a32bf9b29d` |
| `deploy_document_map` | `0.1.0` | `sha256:a8b359372816` | none | `rec:a442dcb589a9` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:22fd5b3c0f8b` | none | `rec:1725dd9fd9d3` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:01f101f527e7` | none | `rec:23dc3adc91e0` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:bcba1781f592` | none | `rec:2b672723e64e` |
| `deploy_lifecycle_map` | `0.1.0` | `sha256:fd3b955cad3c` | none | `rec:c155dd82fcef` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `tabular` | `0.1.0` | `sha256:15487b4cb47e` | pyarrow 25.0.1 | `rec:b4754c46f7fb` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `authorisation_envelope` | 4 |
| `change_record` | 6 |
| `incident_record` | 2 |
| `ingest_finding` | 21 |
| `maintenance_event` | 12 |
| `requalification_record` | 2 |
| `source_artifact` | 25 |
| `source_revision` | 25 |
| `timestamp_domain` | 12 |
| `transform_record` | 9 |

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

- **warning** `deploy_lifecycle_map.column_unmapped` (unsupported): columns the mapping neither maps nor ignores; they stay in the base package's rows only · `rec:5d69a9018228`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:0e3b217ec318`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:3e68528695f8`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:7127f70bc350`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:834cd37205a9`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:b6c0fd33aaa8`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:d8f71d5e9aca`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:ea27f6d1298f`
- **warning** `deploy_lifecycle_map.list_cell_blank` (missing): a blank cell read into a list field: a list holds no unknown, so this record's list lacks what the cell would have stated; the list is not a statement of none · `rec:eff6c300a43c`
- **warning** `deploy_lifecycle_map.row_unmatched` (unsupported): rows of a mapped table that no rule of the mapping applies to; they have no lifecycle record · `rec:f62ec183b8f2`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:66565ff32dcc`
- **warning** `deploy_lifecycle_map.value_blank` (missing): blank cells in columns the mapping declares required; the fields are unknown · `rec:d21be8f6898f`
- **warning** `deploy_lifecycle_map.value_unreadable` (inconsistent): cells that do not read as their field's declared shape or format; the fields are unknown · `rec:10ed23f39873`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:345320a756d2`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:7b65517a6486`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:8937bd74deea`
- **info** `deploy_document_map.document_unmatched` (unsupported): a document no template matches; it has no lifecycle record · `rec:eb874865d6d0`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:25033f6db4a2`
- **info** `deploy_document_map.template_matched` (missing): a document matched a template: the structure it showed is cited, and the fields of the kind the template does not cover are listed as not covered · `rec:e890597554d9`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:9154f12997a6`
- **info** `deploy_lifecycle_map.item_blank` (missing): parts whose every cell is blank in the row (no part swapped, no test); none is listed · `rec:9ad74a27081b`

## Ambiguous fields

None.
