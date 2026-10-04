# Ingest receipt

Receipt `rec:sha256:a399af30569e96d2a5d29f7a39d0a0beab34655073f62f2c49244fb4c58c349f`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 4; entities: 0
- Findings: 0 errors, 2 warnings, 10 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `robot.mcap` | 4816 | `sha256:9e775857ab35` | mcap 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `mcap` | `0.2.0` | `sha256:2a8c92ad6f72` | lz4 4.4.5, zstandard 0.25.0 | `rec:020b1b8799e7` |
| `neptune.bindings` | `0.1.0` | `sha256:dbfdd73f0250` | none | `rec:de22d4557710` |
| `neptune.clocks` | `0.1.0` | `sha256:8abe788c243b` | none | `rec:35b54c0acb0f` |
| `neptune.frames` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:ebb96c0ca1d5` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:bb62e25c331a` |
| `neptune.introspection` | `0.1.0` | `sha256:c9bc334a3ccc` | none | `rec:a4c22d1acf45` |
| `neptune.validate` | `0.2.0` | `sha256:e29ee45e7bd8` | none | `rec:cc0ca8e2997c` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 12 |
| `run` | 1 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `stream` | 4 |
| `structured_record` | 3 |
| `structured_table` | 1 |
| `timestamp_domain` | 7 |
| `transform_record` | 7 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:b9873d4ab829` | unknown | unknown | 1790000000010000000 on `log_time` | 1790000000090000000 on `log_time` | 4 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:06073a27e94a` | `rec:b9873d4ab829` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | `11` | unknown | unknown |
| `rec:21dcb002c136` | `rec:b9873d4ab829` | `/imu_rear` | `log_time`, `publish_time (/imu_rear)`, `header.stamp (/imu_rear)` | unknown | unknown | unknown |
| `rec:320be71735a9` | `rec:b9873d4ab829` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `3` | unknown | unknown |
| `rec:b9f2aa8f0fa9` | `rec:b9873d4ab829` | `/battery` | `log_time`, `publish_time (/battery)` | `4` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:9365bf8e291c`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:9ecf8c3e402b`
- **info** `mcap.attachment_not_extracted` (unsupported): an attachment of 50 bytes is an embedded file no record kind holds yet; it is cited here, its name and media type in the details · `rec:2e81c72ba19e`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:8c94312891e8`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:edb8807841c4`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:4f9a3f4c054c`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:8df3ba33a443`
- **info** `neptune.clocks.anchors_absent` (missing): no row holds both readings, so these two clocks stay unrelated · `rec:092721c2cc0d`
- **info** `neptune.clocks.anchors_absent` (missing): no row holds both readings, so these two clocks stay unrelated · `rec:b3d2298c7e9d`
- **info** `neptune.clocks.latency_unbounded` (missing): 4 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:c717a7b0cdc0`
- **info** `neptune.clocks.unsynchronised` (missing): the package's clocks form 3 groups that no clock mapping joins; times in different groups cannot be compared · `rec:82a1cb9f89e6`
- **info** `neptune.validate.time_out_of_order` (inconsistent): stream rec:06073a27e94a is not in time order on clock 0 in source order · `rec:f51c126c6e19`

## Ambiguous fields

None.
