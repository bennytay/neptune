# Ingest receipt

Receipt `rec:sha256:c0beebc8288c96775757b3d192400e73b0016237c4932926c9d3523b9723da29`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 1; streams: 2; entities: 1
- Findings: 0 errors, 2 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `flight.ulg` | 744 | `sha256:dfa933a5f86a` | ulog 1.0.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `ulog` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:e9fbe1a6c74e` |

## Records

| Kind | Records |
|---|---|
| `calibration` | 1 |
| `hardware_component` | 2 |
| `hardware_configuration` | 1 |
| `ingest_finding` | 2 |
| `machine` | 1 |
| `run` | 1 |
| `software_configuration` | 1 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `stream` | 2 |
| `timestamp_domain` | 3 |
| `transform_record` | 1 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:bbe0a997b028` | not covered | `px4.sys_uuid:000200000000343233345117003a0027` | 12000000 on `timestamp` | not covered | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:0e2ade545b75` | `rec:bbe0a997b028` | `sensor_accel` | `timestamp`, `timestamp_sample (sensor_accel)` | not covered | not covered | not covered |
| `rec:6a0ff7acdbaf` | `rec:bbe0a997b028` | `vehicle_gps_position` | `timestamp`, `time_utc_usec (vehicle_gps_position)` | not covered | not covered | not covered |

## Entities

| Kind | Record | Stated ids |
|---|---|---|
| machine | `rec:534181a799ce` | `px4.sys_uuid:000200000000343233345117003a0027` |

## Findings

- **warning** `ulog.dropout` (missing): the logger dropped 120 ms of data, so every series has a gap · `rec:ef085d037560`
- **warning** `ulog.software_identity_missing` (missing): the log states no ver_sw_release, so the PX4 release that ran is unknown · `rec:4a7d4fd66418`

## Ambiguous fields

None.
