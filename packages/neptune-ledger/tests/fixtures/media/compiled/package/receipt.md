# Ingest receipt

Receipt `rec:sha256:0839e609244dbb6c6ab92559365a73ed5e9d5e38fe6d4728380313cf626c6956`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 2; streams: 2; entities: 0
- Findings: 0 errors, 4 warnings, 10 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `arm/wrist_camera.mcap` | 3522 | `sha256:8cf02dea9fd8` | mcap 0.2.0, neptune.media 0.1.0 |
| `legged/head_camera.mcap` | 2355 | `sha256:1d77bbf8defa` | mcap 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `mcap` | `0.2.0` | `sha256:2a8c92ad6f72` | lz4 4.4.5, zstandard 0.25.0 | `rec:020b1b8799e7` |
| `neptune.bindings` | `0.1.0` | `sha256:dbfdd73f0250` | none | `rec:de22d4557710` |
| `neptune.clocks` | `0.1.0` | `sha256:8abe788c243b` | none | `rec:35b54c0acb0f` |
| `neptune.frames` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:ebb96c0ca1d5` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:bb62e25c331a` |
| `neptune.introspection` | `0.1.0` | `sha256:c9bc334a3ccc` | none | `rec:a4c22d1acf45` |
| `neptune.media` | `0.1.0` | `sha256:64c36eca5440` | none | `rec:4ff08a33369d` |
| `neptune.plugins` | `0.1.0` | `sha256:44136fa355b3` | neptune-deploy 0.0.1 | `rec:c97e34263879` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 14 |
| `run` | 2 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `stream` | 2 |
| `timestamp_domain` | 6 |
| `transform_record` | 8 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:08618ced96bb` | unknown | unknown | 1790762401000000000 on `log_time` | 1790762401066666666 on `log_time` | 1 |
| `rec:6d6a29e23086` | unknown | unknown | 1790762401000000000 on `log_time` | 1790762401066666666 on `log_time` | 1 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:605d29d02254` | `rec:08618ced96bb` | `/head_camera/image_raw` | `log_time`, `publish_time (/head_camera/image_raw)`, `header.stamp (/head_camera/image_raw)` | `4` | unknown | unknown |
| `rec:84cbd4311a0f` | `rec:6d6a29e23086` | `/wrist_camera/image/compressed` | `log_time`, `publish_time (/wrist_camera/image/compressed)`, `header.stamp (/wrist_camera/image/compressed)` | `3` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:35233737cf93`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:6f586e5f1f6d`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:0ec2fbb6e065`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:92eef3855f03`
- **info** `mcap.payload_partly_decoded` (unsupported): channel 1's payloads are decoded, but 1 field path(s) are walked without a column; each row still cites its message · `rec:43e7af49ce9b`
- **info** `mcap.payload_partly_decoded` (unsupported): channel 1's payloads are decoded, but 1 field path(s) are walked without a column; each row still cites its message · `rec:e94a908d610a`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:1d795cf5fd82`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:58074b6ae3f9`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:b27289b42ff1`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:b52f6f6f0a61`
- **info** `neptune.clocks.latency_unbounded` (missing): 2 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:d0c815eb632b`
- **info** `neptune.clocks.latency_unbounded` (missing): 2 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:e924f8ca83b8`
- **info** `neptune.clocks.unsynchronised` (missing): the package's clocks form 2 groups that no clock mapping joins; times in different groups cannot be compared · `rec:d2c234c28b15`
- **info** `neptune.media.derivative_not_covered` (unsupported): no thumbnail is made for these streams (codec_not_covered) · `rec:9e67487d84af`

## Ambiguous fields

None.
