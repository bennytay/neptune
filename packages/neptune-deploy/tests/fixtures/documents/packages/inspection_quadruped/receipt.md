# Ingest receipt

Receipt `rec:sha256:f1d6910fb02e5d2bbc80fc02d19460a46f93b99e77342bcd601a9c35fa813ee4`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `sop_foot_pad.pdf` | 6497 | `sha256:0f2d6bb52e5b` | pdf 0.1.0 |
| `sop_foot_pad_runbook.md` | 549 | `sha256:11033bc306ca` | markdown 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `markdown` | `0.1.0` | `sha256:9303501e224f` | markdown-it-py 4.2.0 | `rec:40ee4b52a530` |
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:a46247e2db11` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |

## Records

| Kind | Records |
|---|---|
| `document_block` | 32 |
| `document_record` | 2 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `structured_record` | 3 |
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

None.

## Ambiguous fields

None.
