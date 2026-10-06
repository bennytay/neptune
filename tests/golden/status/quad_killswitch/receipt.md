# Ingest receipt

Receipt `rec:sha256:9e6ada2ee1092438e86768f220803d24e2b80a67c1e0df341d2f62c0a8f9f393`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 2; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `logs/quad_killswitch.ulg` | 667 | `sha256:32d98bd355fc` | flightlog 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `flightlog` | `0.2.0` | `sha256:67bec5854aef` | none | `rec:46346883ff46` |

## Records

| Kind | Records |
|---|---|
| `run` | 1 |
| `safety_state` | 3 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `status_report` | 4 |
| `stream` | 2 |
| `structured_record` | 4 |
| `structured_table` | 2 |
| `timestamp_domain` | 1 |
| `transform_record` | 1 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:5ba366c31d79` | unknown | `px4.sys_uuid:quad-0042-killsw` | 5000000 on `timestamp` | unknown | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:4b60de0e0381` | `rec:5ba366c31d79` | not applicable | `timestamp` | unknown | unknown | unknown |
| `rec:d5eccc7c04f7` | `rec:5ba366c31d79` | `actuator_armed` | `timestamp` | unknown | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

None.

## Ambiguous fields

None.
