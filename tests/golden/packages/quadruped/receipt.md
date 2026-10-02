# Ingest receipt

Receipt `rec:sha256:2ca90283e4b76410044be9db30cb19b4ebe610537f807be03599a4904e068d1c`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 4 seen, 4 read, 0 not read, 0 gone
- Runs: 1; streams: 2; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bag/metadata.yaml` | 1818 | `sha256:4b9a6b056ea0` | rosbag2 1.0.0 |
| `bag/walk_0.mcap` | 4783 | `sha256:e6ffc3f6e3f8` | rosbag2 1.0.0 |
| `meshes/body.stl` | 145 | `sha256:589540e2bb28` | stl 1.0.0 |
| `robot.urdf` | 772 | `sha256:9f8f2aaf7b59` | urdf 1.0.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `rosbag2` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:3b71f1d5e3ff` |
| `stl` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:2db8afc06bc9` |
| `urdf` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:163827bfaac7` |

## Records

| Kind | Records |
|---|---|
| `clock_mapping` | 1 |
| `frame` | 3 |
| `frame_binding` | 2 |
| `frame_graph` | 1 |
| `frame_transform` | 2 |
| `hardware_component` | 6 |
| `hardware_configuration` | 1 |
| `run` | 1 |
| `run_assembly` | 1 |
| `snapshot_binding` | 1 |
| `software_configuration` | 1 |
| `source_artifact` | 4 |
| `source_revision` | 4 |
| `spatial_artifact` | 1 |
| `stream` | 2 |
| `timestamp_domain` | 5 |
| `transform_record` | 3 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:5bf3bccba577` | not covered | not covered | 1790762400000000000 on `starting_time` | 1790762400045000000 on `starting_time` | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:016871b7b1d0` | `rec:5bf3bccba577` | `/body_pose` | `log_time`, `publish_time`, `header.stamp (/body_pose)` | `3` | unknown | unknown |
| `rec:7c71ce89a8f6` | `rec:5bf3bccba577` | `/joint_states` | `log_time`, `publish_time`, `header.stamp (/joint_states)` | `3` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

None.

## Ambiguous fields

None.
