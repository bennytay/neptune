# Ingest receipt

Receipt `rec:sha256:dd0a939fd7d615f911109fea36a55cac336fd659d3da700e1401dea0365df822`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 1 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `pump_sop.md` | 791 | `sha256:139a09a8d7cd` | markdown 0.1.0 |
| `pump_sop.pdf` | 6882 | `sha256:989f10a28ac8` | pdf 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `markdown` | `0.1.0` | `sha256:9303501e224f` | markdown-it-py 4.2.0 | `rec:40ee4b52a530` |
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:a46247e2db11` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |

## Records

| Kind | Records |
|---|---|
| `document_block` | 27 |
| `document_record` | 2 |
| `ingest_finding` | 1 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `structured_record` | 5 |
| `structured_table` | 2 |
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

- **info** `markdown.link_definitions` (unrepresentable): 1 link reference definition(s) are blocks with an unknown role; the canonical model has no role for them · `rec:60a17f6976c5`

## Ambiguous fields

None.
