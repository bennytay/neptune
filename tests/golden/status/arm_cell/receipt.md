# Ingest receipt

Receipt `rec:sha256:eb0f8220ed677e9bffb2e555e9157783d9c65b91571e9aac1372294badad521a`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 4; entities: 0
- Findings: 0 errors, 0 warnings, 1 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `logs/arm_cell.mcap` | 5969 | `sha256:1c30f4b103f3` | mcap 0.3.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `mcap` | `0.3.0` | `sha256:78bb423a8a0a` | lz4 4.4.5, zstandard 0.25.0 | `rec:ca6e8981baa9` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 1 |
| `run` | 1 |
| `safety_state` | 9 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `status_report` | 4 |
| `stream` | 4 |
| `timestamp_domain` | 7 |
| `transform_record` | 1 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:1098307a5093` | unknown | unknown | 1790000000000000000 on `log_time` | 1790000005501000000 on `log_time` | 4 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:20790e91a91f` | `rec:1098307a5093` | `/diagnostics` | `log_time`, `publish_time (/diagnostics)`, `header.stamp (/diagnostics)` | `6` | unknown | unknown |
| `rec:415efabcec15` | `rec:1098307a5093` | `/robot_status` | `log_time`, `publish_time (/robot_status)`, `header.stamp (/robot_status)` | `12` | unknown | unknown |
| `rec:cd7ed58d9bca` | `rec:1098307a5093` | `/gripper/status` | `log_time`, `publish_time (/gripper/status)` | `2` | unknown | unknown |
| `rec:fc48408c9c5c` | `rec:1098307a5093` | `/ur/safety_mode` | `log_time`, `publish_time (/ur/safety_mode)` | `3` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **info** `mcap.payload_partly_decoded` (unsupported): channel 1's payloads are decoded, but 2 field path(s) are walked without a column; each row still cites its message · `rec:5382514d8c9d`

## Ambiguous fields

None.
