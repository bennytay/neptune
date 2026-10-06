# Ingest receipt

Receipt `rec:sha256:29369dc56ac219489fd06f154be2303c51facfc64e64755ca005d497faabedea`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 4; entities: 0
- Findings: 0 errors, 2 warnings, 10 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `robot.mcap` | 4816 | `sha256:9e775857ab35` | mcap 0.3.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `mcap` | `0.3.0` | `sha256:78bb423a8a0a` | lz4 4.4.5, zstandard 0.25.0 | `rec:ca6e8981baa9` |
| `neptune.bindings` | `0.1.0` | `sha256:dbfdd73f0250` | none | `rec:57986d5e05d5` |
| `neptune.clocks` | `0.1.0` | `sha256:8abe788c243b` | none | `rec:ad87e8e1eb57` |
| `neptune.frames` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:0f3094cd6c1c` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:2150df4468f3` |
| `neptune.introspection` | `0.1.0` | `sha256:c9bc334a3ccc` | none | `rec:9817c2be3025` |
| `neptune.validate` | `0.2.0` | `sha256:1b5b07bc3453` | none | `rec:8fb79f219a03` |

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
| `rec:73904067ade2` | unknown | unknown | 1790000000010000000 on `log_time` | 1790000000090000000 on `log_time` | 4 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:01d35e9214e5` | `rec:73904067ade2` | `/imu` | `log_time`, `publish_time (/imu)`, `header.stamp (/imu)` | `11` | unknown | unknown |
| `rec:38f3fe51756f` | `rec:73904067ade2` | `/battery` | `log_time`, `publish_time (/battery)` | `4` | unknown | unknown |
| `rec:954737a1dd9b` | `rec:73904067ade2` | `/imu_rear` | `log_time`, `publish_time (/imu_rear)`, `header.stamp (/imu_rear)` | unknown | unknown | unknown |
| `rec:d2f84e704734` | `rec:73904067ade2` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `3` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:ff4cfe0244cd`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:c609384b598e`
- **info** `mcap.attachment_not_extracted` (unsupported): an attachment of 50 bytes is an embedded file no record kind holds yet; it is cited here, its name and media type in the details · `rec:18c9498ceb20`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:8bb7900d1d21`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded (message encoding 'json' is not ROS 1 or CDR); each row cites its message · `rec:946281d1feba`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:6719fda6a1d4`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:6b6ee7ab454c`
- **info** `neptune.clocks.anchors_absent` (missing): no row holds both readings, so these two clocks stay unrelated · `rec:0be07a18b3d8`
- **info** `neptune.clocks.anchors_absent` (missing): no row holds both readings, so these two clocks stay unrelated · `rec:acb9f7aced86`
- **info** `neptune.clocks.latency_unbounded` (missing): 4 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:52a12b80e82c`
- **info** `neptune.clocks.unsynchronised` (missing): the package's clocks form 3 groups that no clock mapping joins; times in different groups cannot be compared · `rec:48c388fcbfd8`
- **info** `neptune.validate.time_out_of_order` (inconsistent): stream rec:01d35e9214e5 is not in time order on clock 0 in source order · `rec:e8e242b044ae`

## Ambiguous fields

None.
