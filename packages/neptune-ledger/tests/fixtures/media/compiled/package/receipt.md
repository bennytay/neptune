# Ingest receipt

Receipt `rec:sha256:34226f9e7140eeb8c725e5778d0b839d22ced40b46ba33e7bf370c67d88c452d`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 2; streams: 2; entities: 0
- Findings: 0 errors, 4 warnings, 10 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `arm/wrist_camera.mcap` | 3522 | `sha256:8cf02dea9fd8` | mcap 0.1.0, neptune.media 0.1.0 |
| `legged/head_camera.mcap` | 2355 | `sha256:1d77bbf8defa` | mcap 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `mcap` | `0.1.0` | `sha256:f42c37ac73d6` | lz4 4.4.5, zstandard 0.25.0 | `rec:89b0c5c3630b` |
| `neptune.bindings` | `0.1.0` | `sha256:dbfdd73f0250` | none | `rec:be06a14505bc` |
| `neptune.clocks` | `0.1.0` | `sha256:8abe788c243b` | none | `rec:c63eb19977e6` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:e338a1172927` |
| `neptune.introspection` | `0.1.0` | `sha256:c9bc334a3ccc` | none | `rec:e29bd75e0779` |
| `neptune.media` | `0.1.0` | `sha256:64c36eca5440` | none | `rec:e2feba8018f9` |
| `neptune.plugins` | `0.1.0` | `sha256:44136fa355b3` | neptune-deploy 0.0.1 | `rec:c97e34263879` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 14 |
| `run` | 2 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `stream` | 2 |
| `timestamp_domain` | 4 |
| `transform_record` | 7 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:4a5503226257` | unknown | unknown | 1790762401000000000 on `log_time` | 1790762401066666666 on `log_time` | 1 |
| `rec:bba16f3ab5a2` | unknown | unknown | 1790762401000000000 on `log_time` | 1790762401066666666 on `log_time` | 1 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:9c23960daf87` | `rec:bba16f3ab5a2` | `/head_camera/image_raw` | `log_time`, `publish_time (/head_camera/image_raw)` | `4` | unknown | unknown |
| `rec:cb3270c8c35a` | `rec:4a5503226257` | `/wrist_camera/image/compressed` | `log_time`, `publish_time (/wrist_camera/image/compressed)` | `3` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:d0528b759964`
- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:f73d20740285`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:25df28e5f499`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:d178f553aed4`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:b1a16fa94ecd`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:f8cafc518b15`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:3b074a108202`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:b21e5d1d9feb`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:c8ceec3dbe64`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:e1d953f361b8`
- **info** `neptune.clocks.latency_unbounded` (missing): 1 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:73f68e9e728b`
- **info** `neptune.clocks.latency_unbounded` (missing): 1 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:b8033a0b44c6`
- **info** `neptune.clocks.unsynchronised` (missing): the package's clocks form 2 groups that no clock mapping joins; times in different groups cannot be compared · `rec:3bbb84d09eb1`
- **info** `neptune.media.derivative_not_covered` (unsupported): no thumbnail is made for these streams (codec_not_covered) · `rec:d2434792f4d1`

## Ambiguous fields

None.
