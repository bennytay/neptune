# Ingest receipt

Receipt `rec:sha256:96510d9e21379e9b4572cf4c375827684ecf041714d571e8ece6f17ab550652e`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 25 seen, 25 read, 0 not read, 0 gone
- Runs: 8; streams: 21; entities: 0
- Findings: 1 errors, 2 warnings, 25 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `authorisation/zone_register.csv` | 788 | `sha256:4b797207931a` | tabular 0.1.0 |
| `changes/servicenow_changes.csv` | 1618 | `sha256:2b7b4b194dd1` | tabular 0.1.0 |
| `cmms/work_orders.csv` | 1841 | `sha256:184e31cadd41` | tabular 0.1.0 |
| `config/AMR-05/nav2_params.yaml` | 405 | `sha256:6495d96ee25f` | config 0.1.0 |
| `config/AMR-06/nav2_params.yaml` | 405 | `sha256:7f04720e8e8b` | config 0.1.0 |
| `config/AMR-07/nav2_params.yaml` | 405 | `sha256:9e0b456f5606` | config 0.1.0 |
| `config/AMR-08/nav2_params.yaml` | 405 | `sha256:171f2ff18c14` | config 0.1.0 |
| `config/AMR-09/nav2_params.yaml` | 424 | `sha256:daf6a02ed856` | config 0.1.0 |
| `config/AMR-10/nav2_params.yaml` | 405 | `sha256:0ea560b788ce` | config 0.1.0 |
| `incidents/INC-0007.pdf` | 6897 | `sha256:b793183fb43a` | pdf 0.1.0 |
| `incidents/INC-0013.pdf` | 6416 | `sha256:c33a7d5acbe3` | pdf 0.1.0 |
| `maps/S-007_zones.geojson` | 1393 | `sha256:83d1dc3347fe` | text 0.1.0 |
| `maps/S-012_zones.geojson` | 1394 | `sha256:22a6767e99ac` | text 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |
| `requalification/requalification_tests.csv` | 785 | `sha256:78f1799105e9` | tabular 0.1.0 |
| `runs/S-007/amr-05_2026-03-03.mcap` | 16942 | `sha256:b2b1499057fb` | mcap 0.1.0 |
| `runs/S-007/amr-06_2026-03-03.mcap` | 17057 | `sha256:7595ffcdd0c9` | mcap 0.1.0 |
| `runs/S-007/amr-07_2026-04-02.mcap` | 17058 | `sha256:c27e609862cb` | mcap 0.1.0 |
| `runs/S-007/amr-07_2026-04-15.mcap` | 16942 | `sha256:be78d4d4d2ea` | mcap 0.1.0 |
| `runs/S-012/amr-08_2026-05-19/amr-08_2026-05-19_0.mcap` | 10088 | `sha256:9ce9e303f7db` | mcap 0.1.0 |
| `runs/S-012/amr-08_2026-05-19/metadata.yaml` | 1940 | `sha256:6a01ea04e416` | rosbag2 0.1.0 |
| `runs/S-012/amr-09_2026-03-05.mcap` | 16942 | `sha256:210280b3b1b3` | mcap 0.1.0 |
| `runs/S-012/amr-10_2026-03-06.mcap` | 17057 | `sha256:3810579592a0` | mcap 0.1.0 |
| `urdf/lift_150.urdf` | 1757 | `sha256:4ed9c552ac68` | text 0.1.0 |
| `urdf/tug_200.urdf` | 1475 | `sha256:15e99b9d0356` | text 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `mcap` | `0.1.0` | `sha256:f42c37ac73d6` | lz4 4.4.5, zstandard 0.25.0 | `rec:89b0c5c3630b` |
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:d268e329090f` |
| `neptune.manifest` | `0.1.0` | `sha256:3bedd387c711` | none | `rec:8a63ca51c7d3` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `rosbag2` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:bf370f1dddcb` |
| `tabular` | `0.1.0` | `sha256:15487b4cb47e` | pyarrow 25.0.1 | `rec:b4754c46f7fb` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `configuration_snapshot` | 7 |
| `configuration_value` | 115 |
| `document_block` | 36 |
| `document_record` | 6 |
| `ingest_finding` | 28 |
| `run` | 8 |
| `source_artifact` | 25 |
| `source_revision` | 25 |
| `stream` | 21 |
| `structured_record` | 43 |
| `structured_table` | 10 |
| `timestamp_domain` | 29 |
| `transform_record` | 8 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:56a6c57747e4` | unknown | unknown | 1775102700000000000 on `log_time` | 1775102875000000000 on `log_time` | 3 |
| `rec:8f5268a17ea8` | unknown | unknown | 1779144000000000000 on `starting_time.nanoseconds_since_epoch` | 1779144115000000000 on `starting_time.nanoseconds_since_epoch` | 0 |
| `rec:a75897dd4717` | unknown | unknown | 1772701200000000000 on `log_time` | 1772701375000000000 on `log_time` | 3 |
| `rec:b05e26a817f1` | unknown | unknown | unknown | unknown | 3 |
| `rec:ce7b23d8ad41` | unknown | unknown | 1772790000000000000 on `log_time` | 1772790175000000000 on `log_time` | 3 |
| `rec:dc107c0fe0d4` | unknown | unknown | 1776290400000000000 on `log_time` | 1776290575000000000 on `log_time` | 3 |
| `rec:dde5d2b35cfe` | unknown | unknown | 1772500200000000000 on `log_time` | 1772500375000000000 on `log_time` | 3 |
| `rec:ef9f75e4a917` | unknown | unknown | 1772573400000000000 on `log_time` | 1772573575000000000 on `log_time` | 3 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:00fec56badd3` | `rec:ce7b23d8ad41` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:0ac2d6c147b2` | `rec:a75897dd4717` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:0b185efedac7` | `rec:ef9f75e4a917` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:2bb5ced651f7` | `rec:b05e26a817f1` | `/status` | `log_time`, `publish_time (/status)` | unknown | unknown | unknown |
| `rec:47a7f7a6022a` | `rec:b05e26a817f1` | `/battery_voltage` | `log_time`, `publish_time (/battery_voltage)` | unknown | unknown | unknown |
| `rec:4d33fa1b1da6` | `rec:a75897dd4717` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:4e0dfaafa0b4` | `rec:ce7b23d8ad41` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:4f358a306efc` | `rec:dde5d2b35cfe` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |
| `rec:5ce4a61bcb9d` | `rec:ef9f75e4a917` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:65266fa49ca0` | `rec:ce7b23d8ad41` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |
| `rec:79e293ab2423` | `rec:56a6c57747e4` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:895a9ce6dbe8` | `rec:dc107c0fe0d4` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:8aa01d7c8c75` | `rec:b05e26a817f1` | `/imu` | `log_time`, `publish_time (/imu)` | unknown | unknown | unknown |
| `rec:8b362db37b82` | `rec:56a6c57747e4` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:9ed5974e5440` | `rec:ef9f75e4a917` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:ac92788de285` | `rec:dc107c0fe0d4` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:afa8c014a326` | `rec:a75897dd4717` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:b82de7ad96dd` | `rec:dde5d2b35cfe` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:daf3a663a66a` | `rec:56a6c57747e4` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |
| `rec:e159554c8e91` | `rec:dde5d2b35cfe` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:feb5b40404c1` | `rec:dc107c0fe0d4` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **error** `mcap.truncated` (corrupt): the file ends inside a chunk: 1507 of its bytes are there, and 5 message(s) are read from them · `rec:b0ccc4982d56`
- **warning** `config.duplicate_key` (inconsistent): 2 entries repeat a key of their mapping; each is kept in source order (first: '/controller_server/ros__parameters/max_vel_x') · `rec:7ee22e82dd5d`
- **warning** `mcap.chunk_truncated` (corrupt): the chunk is cut short: its stored bytes decode to 1458 of its 2965 bytes, whose whole records end at 1151; only those records are read, unchecked by its CRC · `rec:bea2a7286905`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:2b04e32f6e3b`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:2f6fa01f8d2a`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:3301c18538e0`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:3810629e3520`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:38973789c904`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:6278dcb13a9e`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:728905d4217e`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:72fe7e336ca6`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:8355264b924a`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:906aab43ddad`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:93d922ea09e9`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:9586f13551d8`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:963017b0a8f5`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:9b6953010bc8`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:a273a3ddae1f`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:aac5eae55d88`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:b3b8ebe4dc78`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:d561fef06a6b`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:d822ea8c3594`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:dfad4a81c72e`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:ee1eeed1b963`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:1ea18398b615`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:873cb9fcf76e`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:8b22f879a5a9`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:ebf3e6cfd782`

## Ambiguous fields

None.
