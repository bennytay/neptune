# Ingest receipt

Receipt `rec:sha256:d8f77a6d5bd9f1033b5740703e3dc55b024b1ad25174db523e2d1b9d41a77aef`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 2 errors, 5 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `urdf/diff_drive.urdf.xacro` | 2769 | `sha256:6888c00cf846` | urdf 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `urdf` | `0.1.0` | `sha256:4f1353c3a7ce` | none | `rec:9ec0b3c14e38` |

## Records

| Kind | Records |
|---|---|
| `description_expansion` | 1 |
| `description_extension` | 1 |
| `frame` | 5 |
| `frame_graph` | 1 |
| `frame_transform` | 3 |
| `hardware_component` | 9 |
| `hardware_configuration` | 1 |
| `hardware_specification` | 7 |
| `ingest_finding` | 7 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `transform_record` | 1 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|

## Entities

| Kind | Record | Stated ids |
|---|---|---|

## Findings

- **error** `urdf.xacro_include_not_followed` (missing): xacro:include names another file, which is never read while this source is; what it would define is not covered · `rec:cb03dfd2e00f`
- **error** `urdf.xacro_undefined` (missing): the macro 'lidar_sensor' is not defined in this file; the call is dropped · `rec:0631171cdafb`
- **warning** `urdf.xacro_not_covered` (missing): the argument 'serial_port' declares no default; only the environment gives it a value, so it is not covered · `rec:b4c5f8e84663`
- **warning** `urdf.xacro_not_covered` (missing): $(find) needs a ROS installation or the environment; the value is not covered · `rec:b725335ca1f5`
- **warning** `urdf.xacro_not_covered` (missing): $(arg serial_port) needs a ROS installation or the environment; the value is not covered · `rec:d1ae80ec6a73`
- **warning** `urdf.xacro_not_covered` (missing): $(env) needs a ROS installation or the environment; the value is not covered · `rec:d247759b83a8`
- **warning** `urdf.xacro_undefined` (missing): ${caster_height} uses 'caster_height' is not defined; it is left as declared · `rec:84bec0a209d4`

## Ambiguous fields

None.
