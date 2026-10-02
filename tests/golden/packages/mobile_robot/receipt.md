# Ingest receipt

Receipt `rec:sha256:800258255ce4c8470fa2e11d5d25420267a25bc87747935f4ea0edd186a93646`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 4 seen, 4 read, 0 not read, 0 gone
- Runs: 1; streams: 2; entities: 2
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `deployment/records.json` | 4903 | `sha256:5d7a7e18bb8b` | deployment_json 1.0.0 |
| `drive.bag` | 5810 | `sha256:dbdd615de58b` | rosbag1 1.0.0 |
| `photos/dock.png` | 352 | `sha256:a129a6a387b1` | png 1.0.0 |
| `sites.csv` | 128 | `sha256:7d0b94de1c53` | csv 1.0.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `csv` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:800d71037ae3` |
| `deployment_json` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:538dcf592361` |
| `png` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:98cfc802e399` |
| `rosbag1` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:266373f75daf` |

## Records

| Kind | Records |
|---|---|
| `authorisation_envelope` | 1 |
| `change_record` | 1 |
| `commissioning_baseline` | 1 |
| `image` | 1 |
| `incident_record` | 1 |
| `intervention` | 1 |
| `risk_assessment` | 1 |
| `run` | 1 |
| `site` | 2 |
| `source_artifact` | 4 |
| `source_revision` | 4 |
| `stream` | 2 |
| `structured_record` | 2 |
| `structured_table` | 1 |
| `timestamp_domain` | 4 |
| `transform_record` | 4 |

## Runs

| Run | Session | Machine | First | Last | Streams |
|---|---|---|---|---|---|
| `rec:83e76d50caf6` | not covered | not covered | 1790766000000000000 on `time` | 1790766000200000000 on `time` | 2 |

## Streams

| Stream | Run | Topic | Clocks | Messages | First | Last |
|---|---|---|---|---|---|---|
| `rec:2c7dee58bb97` | `rec:83e76d50caf6` | `/battery` | `time` | `1` | unknown | unknown |
| `rec:9d081eb5f137` | `rec:83e76d50caf6` | `/wheel_odom` | `time`, `header.stamp (/wheel_odom)` | `3` | unknown | unknown |

## Entities

| Kind | Record | Stated ids |
|---|---|---|
| site | `rec:2344dc781011` | `register:S-008` |
| site | `rec:ba9fb1995439` | `register:S-007` |

## Findings

None.

## Ambiguous fields

None.
