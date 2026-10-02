# Ingest receipt

Receipt `rec:sha256:e28839808f1d54280a50a2afd14a5c1aff4d9063c01daf7fbdfcd89eb5731ec9`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `records.json` | 3374 | `sha256:c5ebbba39b83` | deployment_json 1.0.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `deployment_json` | `1.0.0` | `sha256:44136fa355b3` | none | `rec:538dcf592361` |

## Records

| Kind | Records |
|---|---|
| `commissioning_baseline` | 1 |
| `maintenance_event` | 1 |
| `requalification_record` | 1 |
| `risk_assessment` | 1 |
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `timestamp_domain` | 1 |
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

None.

## Ambiguous fields

None.
