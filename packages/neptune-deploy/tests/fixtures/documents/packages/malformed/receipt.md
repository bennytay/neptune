# Ingest receipt

Receipt `rec:sha256:4f5bac0d965735cafa666707c26349d011c817dae3381b8bfa4ec5757b39693d`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 3 seen, 3 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `risk_amr_revision3.pdf` | 8846 | `sha256:62984684ca5f` | pdf 0.1.0 |
| `risk_amr_rotated.pdf` | 8857 | `sha256:5147bcf85748` | pdf 0.1.0 |
| `scan_0042.pdf` | 796 | `sha256:0df01f1b29c6` | pdf 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:a46247e2db11` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |

## Records

| Kind | Records |
|---|---|
| `document_block` | 31 |
| `document_record` | 3 |
| `source_artifact` | 3 |
| `source_revision` | 3 |
| `structured_record` | 6 |
| `structured_table` | 2 |
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
