# Ingest receipt

Receipt `rec:sha256:0fea82520e36573dee0a259b8e81732bf927a4204d0e10a3136bc28f0d210da2`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `bringup/params/nav2_params.yaml` | 2867 | `sha256:afc22a40f09a` | config 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `config` | `0.1.0` | `sha256:3b6cd0357a16` | python 3.12, pyyaml 6.0.3 | `rec:03ab66ea622a` |

## Records

| Kind | Records |
|---|---|
| `configuration_snapshot` | 1 |
| `configuration_value` | 107 |
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

None.

## Ambiguous fields

None.
