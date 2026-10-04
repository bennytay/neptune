# Ingest receipt

Receipt `rec:sha256:5f304db8596f01a068d2f3b50ad6c291af514a7a4c8f3bd747389381c6de383f`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 25 seen, 25 read, 0 not read, 0 gone
- Runs: 8; streams: 21; entities: 0
- Findings: 1 errors, 22 warnings, 48 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `authorisation/zone_register.csv` | 788 | `sha256:4b797207931a` | tabular 0.2.0 |
| `changes/servicenow_changes.csv` | 1618 | `sha256:2b7b4b194dd1` | tabular 0.2.0 |
| `cmms/work_orders.csv` | 1978 | `sha256:46a7c074ef28` | tabular 0.2.0 |
| `config/AMR-05/nav2_params.yaml` | 405 | `sha256:6495d96ee25f` | config 0.1.0 |
| `config/AMR-06/nav2_params.yaml` | 405 | `sha256:7f04720e8e8b` | config 0.1.0 |
| `config/AMR-07/nav2_params.yaml` | 405 | `sha256:9e0b456f5606` | config 0.1.0 |
| `config/AMR-08/nav2_params.yaml` | 405 | `sha256:171f2ff18c14` | config 0.1.0 |
| `config/AMR-09/nav2_params.yaml` | 424 | `sha256:daf6a02ed856` | config 0.1.0 |
| `config/AMR-10/nav2_params.yaml` | 405 | `sha256:0ea560b788ce` | config 0.1.0 |
| `incidents/INC-0007.pdf` | 6897 | `sha256:b793183fb43a` | pdf 0.1.0 |
| `incidents/INC-0013.pdf` | 6416 | `sha256:c33a7d5acbe3` | pdf 0.1.0 |
| `maps/S-007_zones.geojson` | 1393 | `sha256:83d1dc3347fe` | geojson 0.1.0 |
| `maps/S-012_zones.geojson` | 1394 | `sha256:22a6767e99ac` | geojson 0.1.0 |
| `neptune.yaml` | 149 | `sha256:2c6ff33f9633` | config 0.1.0 |
| `requalification/requalification_tests.csv` | 785 | `sha256:82e663e574ca` | tabular 0.2.0 |
| `runs/S-007/amr-05_2026-03-03.mcap` | 17400 | `sha256:08528ca2ef6f` | mcap 0.2.0 |
| `runs/S-007/amr-06_2026-03-03.mcap` | 17515 | `sha256:b80fb68ef024` | mcap 0.2.0 |
| `runs/S-007/amr-07_2026-04-02.mcap` | 17516 | `sha256:34bb6772f2fd` | mcap 0.2.0 |
| `runs/S-007/amr-07_2026-04-15.mcap` | 17400 | `sha256:e1987a0b1303` | mcap 0.2.0 |
| `runs/S-012/amr-08_2026-05-19/amr-08_2026-05-19_0.mcap` | 10088 | `sha256:9ce9e303f7db` | mcap 0.2.0, neptune.introspection 0.1.0 |
| `runs/S-012/amr-08_2026-05-19/metadata.yaml` | 1940 | `sha256:6a01ea04e416` | rosbag2 0.2.0, neptune.grouping 0.2.0 |
| `runs/S-012/amr-09_2026-03-05.mcap` | 17400 | `sha256:e7e0782cb047` | mcap 0.2.0 |
| `runs/S-012/amr-10_2026-03-06.mcap` | 17515 | `sha256:a217da2d2bc6` | mcap 0.2.0 |
| `urdf/lift_150.urdf` | 1757 | `sha256:4ed9c552ac68` | text 0.1.0 |
| `urdf/tug_200.urdf` | 1475 | `sha256:15e99b9d0356` | text 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:cfb937994850` | python 3.12, pyyaml 6.0.3 | `rec:746e5f2c7c59` |
| `geojson` | `0.1.0` | `sha256:542c42fe020a` | none | `rec:2c0e605ea2be` |
| `mcap` | `0.2.0` | `sha256:2a8c92ad6f72` | lz4 4.4.5, zstandard 0.25.0 | `rec:020b1b8799e7` |
| `neptune.bindings` | `0.1.0` | `sha256:dbfdd73f0250` | none | `rec:eeeae4b50647` |
| `neptune.clocks` | `0.1.0` | `sha256:8abe788c243b` | none | `rec:813f84ae2b74` |
| `neptune.frames` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:eea7c92058c7` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:5a4dc4d9d1f4` |
| `neptune.introspection` | `0.1.0` | `sha256:c9bc334a3ccc` | none | `rec:a4c22d1acf45` |
| `neptune.manifest` | `0.1.0` | `sha256:3bedd387c711` | none | `rec:8a63ca51c7d3` |
| `neptune.plugins` | `0.1.0` | `sha256:44136fa355b3` | neptune-deploy 0.0.1 | `rec:c97e34263879` |
| `neptune.validate` | `0.1.0` | `sha256:0a0d08ccf4b7` | none | `rec:c712df3eb7ab` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |
| `rosbag2` | `0.2.0` | `sha256:6407455961dc` | none | `rec:4df8cd116009` |
| `tabular` | `0.2.0` | `sha256:f3ca9581546a` | pyarrow 25.0.1 | `rec:a849dd895889` |
| `text` | `0.1.0` | `sha256:12b7c5463bbb` | none | `rec:3b8cd771874a` |

## Records

| Kind | Records |
|---|---|
| `configuration_snapshot` | 7 |
| `configuration_value` | 115 |
| `document_block` | 34 |
| `document_record` | 4 |
| `ingest_finding` | 71 |
| `run` | 8 |
| `run_assembly` | 1 |
| `source_artifact` | 25 |
| `source_revision` | 25 |
| `spatial_artifact` | 2 |
| `stream` | 21 |
| `structured_record` | 60 |
| `structured_table` | 14 |
| `timestamp_domain` | 36 |
| `transform_record` | 15 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:07e15a3cb053` | unknown | unknown | 1772701200000000000 on `log_time` | 1772701375000000000 on `log_time` | 3 |
| `rec:243f448f60b0` | unknown | unknown | 1776290400000000000 on `log_time` | 1776290575000000000 on `log_time` | 3 |
| `rec:28ff7d688bbb` | unknown | unknown | 1772500200000000000 on `log_time` | 1772500375000000000 on `log_time` | 3 |
| `rec:3d72d551e6be` | unknown | unknown | 1775102700000000000 on `log_time` | 1775102875000000000 on `log_time` | 3 |
| `rec:4434a63692ed` | unknown | unknown | 1779144000000000000 on `starting_time.nanoseconds_since_epoch` | 1779144115000000000 on `starting_time.nanoseconds_since_epoch` | 0 |
| `rec:4f737785e8b8` | unknown | unknown | 1772790000000000000 on `log_time` | 1772790175000000000 on `log_time` | 3 |
| `rec:5d0265b03dad` | unknown | unknown | 1772573400000000000 on `log_time` | 1772573575000000000 on `log_time` | 3 |
| `rec:b345cce39f04` | unknown | unknown | unknown | unknown | 3 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:27892e413a29` | `rec:243f448f60b0` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:3c63147e9e90` | `rec:4f737785e8b8` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:3cca92f346ab` | `rec:07e15a3cb053` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:49c6459bcc45` | `rec:4f737785e8b8` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |
| `rec:5271805c95e8` | `rec:28ff7d688bbb` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | `36` | unknown | unknown |
| `rec:56a672386e56` | `rec:3d72d551e6be` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | `36` | unknown | unknown |
| `rec:5d6f9da7d22f` | `rec:3d72d551e6be` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:7d820fcf1f5f` | `rec:4f737785e8b8` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | `36` | unknown | unknown |
| `rec:8b6a5353d385` | `rec:b345cce39f04` | `/battery_voltage` | `log_time`, `publish_time (/battery_voltage)` | unknown | unknown | unknown |
| `rec:9dc6d154de61` | `rec:b345cce39f04` | `/status` | `log_time`, `publish_time (/status)` | unknown | unknown | unknown |
| `rec:a28dcc491b29` | `rec:28ff7d688bbb` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |
| `rec:aa407c8ff808` | `rec:5d0265b03dad` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:b3866679ba28` | `rec:5d0265b03dad` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | `36` | unknown | unknown |
| `rec:c17d75abaaab` | `rec:07e15a3cb053` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | unknown | unknown | unknown |
| `rec:c50c62066382` | `rec:243f448f60b0` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:d2128dc864e5` | `rec:07e15a3cb053` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | `36` | unknown | unknown |
| `rec:d5d2cc8ca28d` | `rec:5d0265b03dad` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:e0598fcef000` | `rec:243f448f60b0` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | `36` | unknown | unknown |
| `rec:ed5f870f1440` | `rec:28ff7d688bbb` | `/battery` | `log_time`, `publish_time (/battery)` | `6` | unknown | unknown |
| `rec:eea1676a899d` | `rec:b345cce39f04` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | unknown | unknown | unknown |
| `rec:f4087070f9d6` | `rec:3d72d551e6be` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `1` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **error** `mcap.truncated` (corrupt): the file ends inside a chunk: 1507 of its bytes are there, and 5 message(s) are read from them · `rec:ba090e403418`
- **warning** `config.duplicate_key` (inconsistent): 2 entries repeat a key of their mapping; each is kept in source order (first: '/controller_server/ros__parameters/max_vel_x') · `rec:7ee22e82dd5d`
- **warning** `mcap.chunk_truncated` (corrupt): the chunk is cut short: its stored bytes decode to 1458 of its 2965 bytes, whose whole records end at 1151; only those records are read, unchecked by its CRC · `rec:f70315288875`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:0aca39ee0f1c`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:58f6723b5cc3`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:8f0273a5ffd3`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:be30338974b5`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:bf00dd5a25ce`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:cc2c54c80abb`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:dd827bdc5098`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:e810f203e7bb`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:1a510085125e`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:1c3c458df1d3`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:2896ba2ed677`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:3bf780cd7da5`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:41e5c0620adc`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:48750a7ed2e9`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:876966c8efdd`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:d229443f1fb9`
- **warning** `neptune.introspection.definition_unreadable` (unsupported): the stream's definition cannot be used: the definition is not one byte range · `rec:1c4eb3c5948e`
- **warning** `neptune.introspection.definition_unreadable` (unsupported): the stream's definition cannot be used: the definition is not one byte range · `rec:331368f51f35`
- **warning** `neptune.introspection.definition_unreadable` (unsupported): the stream's definition cannot be used: the definition is not one byte range · `rec:ba0b6d23c6ac`
- **warning** `neptune.validate.source_incomplete` (corrupt): source sha256:9ce9e303f7db is incomplete: 2 findings report cut-off or corrupt bytes; 9 records hold what was read · `rec:0ea7c84d76ef`
- **info** `geojson.crs_legacy` (inconsistent): the file states CRS local:site-grid-m in a crs member, which RFC 7946 removed · `rec:b14603956bb4`
- **info** `geojson.crs_legacy` (inconsistent): the file states CRS local:site-grid-m in a crs member, which RFC 7946 removed · `rec:e36dbe8c6388`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:281ba9763a3a`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:5964904af37d`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:715a1dab1f0f`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:73d6688c41c0`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:780a143ce827`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:7f337d6a4f89`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:88a9b2914474`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:8bb7d2ec1b49`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:923249b03b98`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:dc70fadd71a1`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:f528702a7e89`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:fd74eb5e0284`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:139ce8f8e388`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:2010b40e3622`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:29dba1a56714`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:3560f65b3adc`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:4a953aab06f5`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:4fc27ff7ed50`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:533832eaf556`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:63917ada8001`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:7ef76ec8d547`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:8ccd5458d1f8`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:8ce2d5b6fdce`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:c5d9793314a1`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:d22b13da61f6`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:d5d502ac0b4b`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:ef08f87ac83a`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:fb10d3d12c2f`
- **info** `neptune.clocks.anchors_absent` (missing): no row holds both readings, so these two clocks stay unrelated · `rec:0e8caa694531`
- **info** `neptune.clocks.anchors_absent` (missing): no row holds both readings, so these two clocks stay unrelated · `rec:88acc34e270a`
- **info** `neptune.clocks.anchors_absent` (missing): no row holds both readings, so these two clocks stay unrelated · `rec:a75cac171fc0`
- **info** `neptune.clocks.latency_unbounded` (missing): 4 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:00f7cffd66c2`
- **info** `neptune.clocks.latency_unbounded` (missing): 4 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:30430e737285`
- **info** `neptune.clocks.latency_unbounded` (missing): 3 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:31097676e721`
- **info** `neptune.clocks.latency_unbounded` (missing): 4 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:6ed0bee96a32`
- **info** `neptune.clocks.latency_unbounded` (missing): 3 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:6f74ee044094`
- **info** `neptune.clocks.latency_unbounded` (missing): 3 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:7d68a9fe1c3c`
- **info** `neptune.clocks.latency_unbounded` (missing): 4 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:aaea955875a4`
- **info** `neptune.clocks.single_instant` (missing): every anchor is one instant of the source clock: the offset holds there only and the rate is unknown · `rec:4b507f4c272d`
- **info** `neptune.clocks.single_instant` (missing): every anchor is one instant of the source clock: the offset holds there only and the rate is unknown · `rec:505d1bfe6f4a`
- **info** `neptune.clocks.single_instant` (missing): every anchor is one instant of the source clock: the offset holds there only and the rate is unknown · `rec:8b6978e00c53`
- **info** `neptune.clocks.unsynchronised` (missing): the package's clocks form 11 groups that no clock mapping joins; times in different groups cannot be compared · `rec:930916b68562`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:100048fd4ac5`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:2f08528c577c`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:7ba4868308e1`
- **info** `tabular.csv_dialect` (missing): a CSV declares no dialect: read as UTF-8, delimited by comma (sniffed), quoted with '"', header first_row · `rec:c9dbb00ab9f7`

## Ambiguous fields

None.
