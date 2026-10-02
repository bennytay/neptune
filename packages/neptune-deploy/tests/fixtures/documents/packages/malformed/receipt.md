# Ingest receipt

Receipt `rec:sha256:7b9e8b307c455326c999bcbe90d29d7086668c9a8b8ab493a76affcdee285bfc`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 4 seen, 4 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `incident_amr_split.pdf` | 7112 | `sha256:32ec7135bace` | pdf 0.1.0 |
| `risk_amr_revision3.pdf` | 8846 | `sha256:62984684ca5f` | pdf 0.1.0 |
| `risk_amr_rotated.pdf` | 8857 | `sha256:5147bcf85748` | pdf 0.1.0 |
| `scan_0042.pdf` | 796 | `sha256:0df01f1b29c6` | pdf 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:1bc3f4fa541f` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |

## Records

| Kind | Records |
|---|---|
| `document_block` | 48 |
| `document_record` | 4 |
| `source_artifact` | 4 |
| `source_revision` | 4 |
| `structured_record` | 10 |
| `structured_table` | 3 |
| `transform_record` | 2 |

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
