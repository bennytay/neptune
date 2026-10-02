# Ingest receipt

Receipt `rec:sha256:016f6bc26cab16adf740db983c0d595ae2d0841af038fcba770278befe5e77af`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 1; streams: 2; entities: 0
- Findings: 0 errors, 1 warnings, 0 info; ambiguous fields: 1

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `handeye.yaml` | 348 | `sha256:202b5b3f5707` | handeye 1.0.0 |
| `session.mcap` | 2570 | `sha256:d6bebd2e8559` | mcap 1.0.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `handeye` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:b067af6b936b` |
| `mcap` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:67b6d9921ecc` |

## Records

| Kind | Records |
|---|---|
| `calibration` | 1 |
| `frame` | 4 |
| `frame_binding` | 1 |
| `frame_graph` | 1 |
| `frame_transform` | 1 |
| `ingest_finding` | 1 |
| `run` | 1 |
| `run_assembly` | 1 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `stream` | 2 |
| `timestamp_domain` | 4 |
| `transform_record` | 2 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:0cf852d4efcb` | unknown | unknown | 1790762401000000000 on `log_time` | 1790762401020000000 on `log_time` | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:234161ce7ba7` | `rec:0cf852d4efcb` | `/wrist_camera/image/compressed` | `log_time`, `publish_time`, `header.stamp (/wrist_camera/image/compressed)` | `1` | unknown | unknown |
| `rec:9d2fbf066057` | `rec:0cf852d4efcb` | `/joint_states` | `log_time`, `publish_time`, `header.stamp (/joint_states)` | `2` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **warning** `handeye.unit_undeclared` (missing): the transformation states no unit for x, y and z · `rec:402177b2decb`

## Ambiguous fields

- `rec:e2c144c52c3f` `/direction`
