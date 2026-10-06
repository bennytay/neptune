# Ingest receipt

Receipt `rec:sha256:72a6adec82b7ab442873529d12ce4c8fd60fed99ccccf6b4bcf285c904cbd998`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 3; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `logs/boat_failsafe.bin` | 1711 | `sha256:fe5926ddf656` | flightlog 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `flightlog` | `0.2.0` | `sha256:67bec5854aef` | none | `rec:46346883ff46` |

## Records

| Kind | Records |
|---|---|
| `run` | 1 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `status_report` | 6 |
| `stream` | 3 |
| `structured_record` | 7 |
| `structured_table` | 1 |
| `timestamp_domain` | 1 |
| `transform_record` | 1 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:6c759ed2cf33` | unknown | unknown | unknown | unknown | 3 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:150eca3cfe23` | `rec:6c759ed2cf33` | `MODE` | `TimeUS` | unknown | unknown | unknown |
| `rec:56be4318a424` | `rec:6c759ed2cf33` | `ERR` | `TimeUS` | unknown | unknown | unknown |
| `rec:ec72f57b22f8` | `rec:6c759ed2cf33` | `MSG` | `TimeUS` | unknown | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

None.

## Ambiguous fields

None.
