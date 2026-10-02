# Ingest receipt

Receipt `rec:sha256:371dccf8bb67d5a806ed73855073535267c4d1146c2ff0583c72986015773776`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 2; streams: 2; entities: 0
- Findings: 0 errors, 1 warnings, 3 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `arm_cell_bag_0.db3` | 24576 | `sha256:126db197922a` | neptune.introspection 0.1.0, rosbag2 0.1.0 |
| `metadata.yaml` | 1001 | `sha256:11c9d77029df` | rosbag2 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `neptune.clocks` | `0.1.0` | `sha256:8abe788c243b` | none | `rec:0dcf58f58fd5` |
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:a46247e2db11` |
| `neptune.introspection` | `0.1.0` | `sha256:c9bc334a3ccc` | none | `rec:05c5b71e88a8` |
| `neptune.plugins` | `0.1.0` | `sha256:44136fa355b3` | neptune-deploy 0.0.1 | `rec:c97e34263879` |
| `rosbag2` | `0.1.0` | `sha256:44136fa355b3` | none | `rec:bf370f1dddcb` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 4 |
| `run` | 2 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `stream` | 2 |
| `structured_record` | 11 |
| `structured_table` | 4 |
| `timestamp_domain` | 2 |
| `transform_record` | 5 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:0c22d18bff37` | unknown | unknown | 1772366400000000000 on `starting_time.nanoseconds_since_epoch` | 1772366405500000000 on `starting_time.nanoseconds_since_epoch` | 0 |
| `rec:317000cefab2` | unknown | unknown | 1772366400000000000 on `timestamp` | 1772366405500000000 on `timestamp` | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:2d1e4fb898f4` | `rec:317000cefab2` | `/joint_states` | `timestamp` | `6` | unknown | unknown |
| `rec:63efe34d8e43` | `rec:317000cefab2` | `/diagnostics` | `timestamp` | `6` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **warning** `neptune.introspection.type_contradicts_layout` (inconsistent): the definition of sensor_msgs/JointState lacks fields that type has; it is not classified by its name · `rec:ef83cb245951`
- **info** `neptune.clocks.unsynchronised` (missing): the package's clocks form 2 groups that no clock mapping joins; times in different groups cannot be compared · `rec:6df6188f6dde`
- **info** `rosbag2.payload_not_decoded` (unsupported): topic 1's message payloads are not decoded; each row cites its message's cell · `rec:03c47a6eb51c`
- **info** `rosbag2.payload_not_decoded` (unsupported): topic 2's message payloads are not decoded; each row cites its message's cell · `rec:7a8ffc4210c0`

## Ambiguous fields

None.
