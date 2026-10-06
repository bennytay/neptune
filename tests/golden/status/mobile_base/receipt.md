# Ingest receipt

Receipt `rec:sha256:c0939bdf21b83966478eaa7ddbc043c4a7cadf0e528484a9e64c89e8beb0e615`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 2; entities: 0
- Findings: 0 errors, 0 warnings, 1 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `logs/mobile_base.bag` | 11402 | `sha256:02f466a808ab` | rosbag1 0.3.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `rosbag1` | `0.3.0` | `sha256:e93b23fec682` | bz2 cpython-3.12, lz4 4.4.5 | `rec:26984b1adc1b` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 1 |
| `run` | 1 |
| `safety_state` | 2 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `status_report` | 4 |
| `stream` | 2 |
| `timestamp_domain` | 3 |
| `transform_record` | 1 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:c842ec5fcc2c` | unknown | unknown | 1790000000000000000 on `time` | 1790000004010000000 on `time` | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:21b46abc2e94` | `rec:c842ec5fcc2c` | `/status` | `time`, `header.stamp (/status)` | `5` | unknown | unknown |
| `rec:2421dd78dab3` | `rec:c842ec5fcc2c` | `/diagnostics_agg` | `time`, `header.stamp (/diagnostics_agg)` | `5` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **info** `rosbag1.payload_partly_decoded` (unsupported): connection 0's payloads are decoded, but 2 field path(s) are walked without a column; each row still cites its message · `rec:7aa38ebc16a8`

## Ambiguous fields

None.
