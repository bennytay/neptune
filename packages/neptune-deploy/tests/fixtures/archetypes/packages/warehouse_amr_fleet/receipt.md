# Ingest receipt

Receipt `rec:sha256:5ad3edc84f84f09d0135c0a2ee267b5dcd5d7cf692f9350a2765a24575de67e6`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 25 seen, 25 read, 0 not read, 0 gone
- Runs: 8; streams: 21; entities: 0
- Findings: 1 errors, 2 warnings, 25 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `authorisation/zone_register.csv` | 788 | `sha256:4b797207931a` | tabular 0.1.0 |
| `changes/servicenow_changes.csv` | 1618 | `sha256:2b7b4b194dd1` | tabular 0.1.0 |
| `cmms/work_orders.csv` | 1978 | `sha256:46a7c074ef28` | tabular 0.1.0 |
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
| `requalification/requalification_tests.csv` | 785 | `sha256:82e663e574ca` | tabular 0.1.0 |
| `runs/S-007/amr-05_2026-03-03.mcap` | 17400 | `sha256:08528ca2ef6f` | mcap 0.1.0 |
| `runs/S-007/amr-06_2026-03-03.mcap` | 17515 | `sha256:b80fb68ef024` | mcap 0.1.0 |
| `runs/S-007/amr-07_2026-04-02.mcap` | 17516 | `sha256:34bb6772f2fd` | mcap 0.1.0 |
| `runs/S-007/amr-07_2026-04-15.mcap` | 17400 | `sha256:e1987a0b1303` | mcap 0.1.0 |
| `runs/S-012/amr-08_2026-05-19/amr-08_2026-05-19_0.mcap` | 10088 | `sha256:9ce9e303f7db` | mcap 0.1.0 |
| `runs/S-012/amr-08_2026-05-19/metadata.yaml` | 1940 | `sha256:6a01ea04e416` | rosbag2 0.1.0 |
| `runs/S-012/amr-09_2026-03-05.mcap` | 17400 | `sha256:e7e0782cb047` | mcap 0.1.0 |
| `runs/S-012/amr-10_2026-03-06.mcap` | 17515 | `sha256:a217da2d2bc6` | mcap 0.1.0 |
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
| `structured_record` | 44 |
| `structured_table` | 10 |
| `timestamp_domain` | 29 |
| `transform_record` | 8 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:52c7a4fb54b9` | unknown | unknown | 1772573400000000000 on `log_time` | 1772573575000000000 on `log_time` | 3 |
| `rec:65f76ceff675` | unknown | unknown | 1772701200000000000 on `log_time` | 1772701375000000000 on `log_time` | 3 |
| `rec:84bf932cf4c7` | unknown | unknown | 1775102700000000000 on `log_time` | 1775102875000000000 on `log_time` | 3 |
| `rec:86903f26e3d2` | unknown | unknown | 1776290400000000000 on `log_time` | 1776290575000000000 on `log_time` | 3 |
| `rec:8f5268a17ea8` | unknown | unknown | 1779144000000000000 on `starting_time.nanoseconds_since_epoch` | 1779144115000000000 on `starting_time.nanoseconds_since_epoch` | 0 |
| `rec:b05e26a817f1` | unknown | unknown | unknown | unknown | 3 |
| `rec:d3203b4463d3` | unknown | unknown | 1772500200000000000 on `log_time` | 1772500375000000000 on `log_time` | 3 |
| `rec:d512c374bf3e` | unknown | unknown | 1772790000000000000 on `log_time` | 1772790175000000000 on `log_time` | 3 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:011d1bbb62c3` | `rec:d3203b4463d3` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:12c531a9ec38` | `rec:52c7a4fb54b9` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:25089ce30b53` | `rec:d512c374bf3e` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:2bb5ced651f7` | `rec:b05e26a817f1` | `/status` | `log_time`, `publish_time (/status)` | unknown | unknown | unknown |
| `rec:34ba43351f1e` | `rec:84bf932cf4c7` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |
| `rec:47a7f7a6022a` | `rec:b05e26a817f1` | `/battery_voltage` | `log_time`, `publish_time (/battery_voltage)` | unknown | unknown | unknown |
| `rec:6aa738e5caff` | `rec:d512c374bf3e` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |
| `rec:6b80a55c1da2` | `rec:86903f26e3d2` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:81aeb272a5b2` | `rec:52c7a4fb54b9` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:8aa01d7c8c75` | `rec:b05e26a817f1` | `/imu` | `log_time`, `publish_time (/imu)` | unknown | unknown | unknown |
| `rec:945a24466bc7` | `rec:65f76ceff675` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:9bea856acceb` | `rec:84bf932cf4c7` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:a9e9d8fec34a` | `rec:d3203b4463d3` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:c2bd33445b6f` | `rec:d512c374bf3e` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:c4c8602a654c` | `rec:65f76ceff675` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:cd2f361eed42` | `rec:52c7a4fb54b9` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:d8913e0847ec` | `rec:86903f26e3d2` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:e2133b577b7a` | `rec:65f76ceff675` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:f5a555b6e34a` | `rec:86903f26e3d2` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |
| `rec:f6c468bff5a2` | `rec:d3203b4463d3` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |
| `rec:fc02cb428ea3` | `rec:84bf932cf4c7` | `/imu` | `log_time`, `publish_time (/imu)` | `36` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **error** `mcap.truncated` (corrupt): the file ends inside a chunk: 1507 of its bytes are there, and 5 message(s) are read from them · `rec:b0ccc4982d56`
- **warning** `config.duplicate_key` (inconsistent): 2 entries repeat a key of their mapping; each is kept in source order (first: '/controller_server/ros__parameters/max_vel_x') · `rec:7ee22e82dd5d`
- **warning** `mcap.chunk_truncated` (corrupt): the chunk is cut short: its stored bytes decode to 1458 of its 2965 bytes, whose whole records end at 1151; only those records are read, unchecked by its CRC · `rec:bea2a7286905`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:2a2a77e18f26`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:2b04e32f6e3b`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:3d7af1403a85`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:3df7d266cdf3`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:4ae44f39e68f`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:728905d4217e`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:72dddbb991a4`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:8355264b924a`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:94784625e2ef`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:9c088641c11c`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:a25b82f6ca08`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:a62c21734b5d`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:a77ff3605c6e`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:ac3bf3a1312a`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:bde90dc9ec37`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:c34ed80c170c`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:c5e6673189e7`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:ca333780edfd`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:cee68bfcba62`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:e765b4253ec8`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:ee43ce9237a6`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:4ec1a1ce29b4`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:8b22f879a5a9`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:aeba41f5da58`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:ebf3e6cfd782`

## Ambiguous fields

None.
