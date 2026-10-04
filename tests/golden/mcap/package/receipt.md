# Ingest receipt

Receipt `rec:sha256:1aeadb9dc84bf20c24ec1888fcac75e07a5e2d151fa356722a17368d01c80a48`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 4; entities: 0
- Findings: 0 errors, 2 warnings, 11 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `robot.mcap` | 4816 | `sha256:9e775857ab35` | mcap 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `mcap` | `0.1.0` | `sha256:f42c37ac73d6` | lz4 4.4.5, zstandard 0.25.0 | `rec:89b0c5c3630b` |
| `neptune.bindings` | `0.1.0` | `sha256:dbfdd73f0250` | none | `rec:be06a14505bc` |
| `neptune.clocks` | `0.1.0` | `sha256:8abe788c243b` | none | `rec:c63eb19977e6` |
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:e338a1172927` |
| `neptune.introspection` | `0.1.0` | `sha256:c9bc334a3ccc` | none | `rec:e29bd75e0779` |
| `neptune.validate` | `0.2.0` | `sha256:1b5b07bc3453` | none | `rec:8fb79f219a03` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 13 |
| `run` | 1 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `stream` | 4 |
| `structured_record` | 3 |
| `structured_table` | 1 |
| `timestamp_domain` | 5 |
| `transform_record` | 6 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:654bd670c8d6` | unknown | unknown | 1790000000010000000 on `log_time` | 1790000000090000000 on `log_time` | 4 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:09913b4cc277` | `rec:654bd670c8d6` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)` | `3` | unknown | unknown |
| `rec:8b786d59e0b9` | `rec:654bd670c8d6` | `/imu_rear` | `log_time`, `publish_time (/imu_rear)` | unknown | unknown | unknown |
| `rec:e7e8c73d328c` | `rec:654bd670c8d6` | `/battery` | `log_time`, `publish_time (/battery)` | `4` | unknown | unknown |
| `rec:f6ee7d482736` | `rec:654bd670c8d6` | `/imu` | `log_time`, `publish_time (/imu)` | `11` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **warning** `neptune.bindings.no_software_identity` (missing): the run has no software identity: no software, build, firmware or checkpoint record is bound to it · `rec:986bb2654b8b`
- **warning** `neptune.bindings.snapshot_unresolved` (missing): no configuration_snapshot record is bound to the run: the binding is unresolved · `rec:6b0471f9a1ab`
- **info** `mcap.attachment_not_extracted` (unsupported): an attachment of 50 bytes is an embedded file no record kind holds yet; it is cited here, its name and media type in the details · `rec:3840bb82b683`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:24c920d14f41`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:2a04449178fd`
- **info** `mcap.payload_not_decoded` (unsupported): channel 4's message payloads are not decoded; each row cites its message's bytes · `rec:542c3cbcba95`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:5e30cda4992d`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no calibration record is bound to the run: the binding is unresolved · `rec:2161478b64ed`
- **info** `neptune.bindings.snapshot_unresolved` (missing): no hardware_configuration record is bound to the run: the binding is unresolved · `rec:ea291a1de32c`
- **info** `neptune.clocks.anchors_absent` (missing): no row holds both readings, so these two clocks stay unrelated · `rec:048955c70ef7`
- **info** `neptune.clocks.latency_unbounded` (missing): 3 clock mapping(s) fitted by stream.co_recorded: each anchor's two readings mark two events, and nothing bounds the time between them, so the mappings' residual bounds are unknown; the fit residuals are in the details · `rec:de7fea67ff46`
- **info** `neptune.clocks.unsynchronised` (missing): the package's clocks form 2 groups that no clock mapping joins; times in different groups cannot be compared · `rec:ab32b6b269d2`
- **info** `neptune.validate.time_out_of_order` (inconsistent): stream rec:f6ee7d482736 is not in time order on clock 0 in source order · `rec:abdf40ca9e99`

## Ambiguous fields

None.
