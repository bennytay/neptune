# Ingest receipt

Receipt `rec:sha256:58704ba2dd4e6f7a2b2451955999f07fe85e993556a55f2ef0b82447593c1a87`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 2 seen, 2 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 0 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `incident_amr_collision.pdf` | 7108 | `sha256:acf79ced9467` | pdf 0.1.0 |
| `risk_amr_iso3691_4.pdf` | 8846 | `sha256:f0e0ff53f96b` | pdf 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `neptune.grouping` | `0.1.0` | `sha256:aeddaa6f335f` | none | `rec:a46247e2db11` |
| `pdf` | `0.1.0` | `sha256:87c0d8dbca90` | pypdf 6.19.0 | `rec:fa2a9216e840` |

## Records

| Kind | Records |
|---|---|
| `document_block` | 32 |
| `document_record` | 2 |
| `source_artifact` | 2 |
| `source_revision` | 2 |
| `structured_record` | 7 |
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
