# Ingest receipt

Receipt `rec:sha256:7ada75c9f0d821689c7bc5dca9aa2d6e852f504fda79cd16b476e00aa888c9e7`. Every id below is shortened; `receipt.json` has them whole.

## Summary

- Sources: 1 seen, 1 read, 0 not read, 0 gone
- Runs: 0; streams: 0; entities: 0
- Findings: 0 errors, 2 warnings, 0 info; ambiguous fields: 0

## Sources

| Location | Bytes | Content | Read by |
|---|---|---|---|
| `sbom/robot.cdx.json` | 1380 | `sha256:ec535b0213b9` | software 0.1.0 |

## Adapters

| Adapter | Version | Config | Libraries | Transform |
|---|---|---|---|---|
| `software` | `0.1.0` | `sha256:90af6add46b7` | none | `rec:ad6ba5e62b40` |

## Records

| Kind | Records |
|---|---|
| `ingest_finding` | 2 |
| `software_configuration` | 1 |
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

- **warning** `software.software_identity_missing` (missing): the CycloneDX BOM gives this item no identity: no release, though the format has a place for it · `rec:711a3c896950`
- **warning** `software.software_identity_missing` (missing): the CycloneDX BOM gives this item no identity: no release or digest, though the format has a place for it · `rec:8db05ceedacb`

## Ambiguous fields

None.
