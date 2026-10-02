# Ingest receipt

Receipt `rec:sha256:1793c5c4814161ce25b1d26613f8ebea0def2e17ceae2fcd609750d6b8560e95`. Every id below is shortened; `receipt.json` has them whole.

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
| `urdf` | `0.1.0` | `sha256:1973ff076078` | none | `rec:f287dc5eb907` |

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

- **error** `urdf.xacro_include_not_followed` (missing): xacro:include names another file, which is never read while this source is; what it would define is not covered · `rec:756b82fffa1e`
- **error** `urdf.xacro_undefined` (missing): the macro 'lidar_sensor' is not defined in this file; the call is dropped · `rec:7be8aedd0149`
- **warning** `urdf.xacro_not_covered` (missing): $(find) needs a ROS installation or the environment; the value is not covered · `rec:3a68473f173a`
- **warning** `urdf.xacro_not_covered` (missing): $(arg serial_port) needs a ROS installation or the environment; the value is not covered · `rec:52a56272c5e1`
- **warning** `urdf.xacro_not_covered` (missing): the argument 'serial_port' declares no default; only the environment gives it a value, so it is not covered · `rec:9b79ffcbead6`
- **warning** `urdf.xacro_not_covered` (missing): $(env) needs a ROS installation or the environment; the value is not covered · `rec:ba78a6a9e210`
- **warning** `urdf.xacro_undefined` (missing): ${caster_height} uses 'caster_height' is not defined; it is left as declared · `rec:91fd1a26842a`

## Ambiguous fields

None.
