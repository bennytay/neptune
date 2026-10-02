# Ingest receipt

Receipt `rec:sha256:17c7e82e1217b3fca0ac5121cbfadb32c3b37b904f2dbbcc28b7064832138d49`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 1; streams: 2; entities: 2
- Findings: 0 errors, 2 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `fleet.json` | 114 | `sha256:3196fbce1bad` | fleet 1.0.0 |
| `flight.ulg` | 744 | `sha256:dfa933a5f86a` | ulog 1.0.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `fleet` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:bf99dcb2bd54` |
| `ulog` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:e9fbe1a6c74e` |

## Records

| Kind | Records |
|---|---|
| `calibration` | 1 |
| `hardware_component` | 2 |
| `hardware_configuration` | 1 |
| `identity_link` | 1 |
| `ingest_finding` | 2 |
| `machine` | 2 |
| `run` | 1 |
| `run_assembly` | 1 |
| `snapshot_binding` | 3 |
| `software_configuration` | 1 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `stream` | 2 |
| `timestamp_domain` | 3 |
| `transform_record` | 2 |

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
| machine | `rec:f436770b63eb` | `fleet.asset_tag:D-07`, `px4.sys_uuid:000200000000343233345117003a0027` |

## Findings

- **warning** `ulog.dropout` (missing): the logger dropped 120 ms of data, so every series has a gap · `rec:ef085d037560`
- **warning** `ulog.software_identity_missing` (missing): the log states no ver_sw_release, so the PX4 release that ran is unknown · `rec:4a7d4fd66418`

## Ambiguous fields

None.
