# Ingest receipt

Receipt `rec:sha256:77c0b6e417c92cfcc3df69b58d885aacaaadf5722520d11d6acccd3cccc1b470`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `diagnostics_export.json` | 1182 | `sha256:cdecc88cc5cc` | tabular 0.2.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:a46247e2db11` |
| `neptune.plugins` | `0.1.0` | `sha256:44136fa355b3` | neptune-deploy 0.0.1 | `rec:c97e34263879` |
| `tabular` | `0.2.0` | `sha256:e0564923e5f0` | pyarrow 25.0.1 | `rec:33ac8b80c8f1` |

## Records

| Kind | Records |
|---|---|
| `source_artifact` | 1 |
| `source_revision` | 1 |
| `structured_record` | 3 |
| `structured_table` | 1 |
| `transform_record` | 3 |

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
