# Ingest receipt

Receipt `rec:sha256:d77ba31ab7b0ba42e9750b286cf1a4c3862b85d15e11305d152c00f3b5f48fb1`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `commissioning_cell3.pdf` | 12179 | `sha256:736c63b99e10` | pdf 0.1.0 |
| `risk_cell_arm.pdf` | 11497 | `sha256:66dfef660942` | pdf 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `neptune.grouping` | `0.2.0` | `sha256:aeddaa6f335f` | none | `rec:1bc3f4fa541f` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |

## Records

| Kind | Records |
|---|---|
| `document_block` | 26 |
| `document_record` | 2 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `structured_record` | 20 |
| `structured_table` | 6 |
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
