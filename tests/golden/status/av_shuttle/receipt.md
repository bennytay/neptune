# Ingest receipt

Receipt `rec:sha256:9dbbba9694ec0099e966eb7f325975510a2669ac337597a3e8782b5323fa7621`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 2; entities: 0
- Findings: 0 errors, 0 warnings, 1 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `logs/av_shuttle.db3` | 24576 | `sha256:918f6ceb9cc5` | rosbag2 0.3.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `rosbag2` | `0.3.0` | `sha256:b3978879ac60` | none | `rec:260e48093d24` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 1 |
| `run` | 1 |
| `safety_state` | 3 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `status_report` | 3 |
| `stream` | 2 |
| `timestamp_domain` | 2 |
| `transform_record` | 1 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:e988bf4b3f38` | unknown | unknown | 1790000000000000000 on `timestamp` | 1790000003505000000 on `timestamp` | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:df467db485d0` | `rec:e988bf4b3f38` | `/system/emergency/emergency_state` | `timestamp` | `8` | unknown | unknown |
| `rec:fb77ef805278` | `rec:e988bf4b3f38` | `/diagnostics` | `timestamp`, `header.stamp (/diagnostics)` | `8` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **info** `rosbag2.payload_partly_decoded` (unsupported): topic 1's payloads are decoded, but 2 field path(s) are walked without a column; each row still cites its message · `rec:3ec122f855bb`

## Ambiguous fields

None.
