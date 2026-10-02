# Ingest receipt

Receipt `rec:sha256:05d5ef8847f269b85e30dd8fb4189f2953b28d83176f4c417f8647b27db48260`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 4; entities: 0
- Findings: 0 errors, 0 warnings, 6 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `robot.mcap` | 4816 | `sha256:9e775857ab35` | mcap 0.1.0, neptune.validate 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `mcap` | `0.1.0` | `sha256:f42c37ac73d6` | lz4 4.4.5, zstandard 0.25.0 | `rec:89b0c5c3630b` |
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:a46247e2db11` |
| `neptune.validate` | `0.1.0` | `sha256:0a0d08ccf4b7` | none | `rec:c712df3eb7ab` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 6 |
| `run` | 1 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `stream` | 4 |
| `structured_record` | 3 |
| `structured_table` | 1 |
| `timestamp_domain` | 5 |
| `transform_record` | 3 |

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

- **info** `mcap.attachment_not_extracted` (unsupported): an attachment of 50 bytes is an embedded file no record kind holds yet; it is cited here, its name and media type in the details · `rec:3840bb82b683`
- **info** `mcap.payload_not_decoded` (unsupported): channel 2's message payloads are not decoded; each row cites its message's bytes · `rec:24c920d14f41`
- **info** `mcap.payload_not_decoded` (unsupported): channel 3's message payloads are not decoded; each row cites its message's bytes · `rec:2a04449178fd`
- **info** `mcap.payload_not_decoded` (unsupported): channel 4's message payloads are not decoded; each row cites its message's bytes · `rec:542c3cbcba95`
- **info** `mcap.payload_not_decoded` (unsupported): channel 1's message payloads are not decoded; each row cites its message's bytes · `rec:5e30cda4992d`
- **info** `neptune.validate.time_out_of_order` (inconsistent): stream rec:f6ee7d482736 is not in time order on clock 0 in source order · `rec:e756366c5877`

## Ambiguous fields

None.
