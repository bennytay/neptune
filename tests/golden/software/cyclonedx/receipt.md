# Ingest receipt

Receipt `rec:sha256:60e5a7021013c616a0207ea6a119edc557584978ba98bf86a6314619e77171c9`. Every id below is shortened; `receipt.json` has them whole.

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
| `software` | `0.1.0` | `sha256:39f46f7448aa` | none | `rec:c1a3463288b8` |

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

- **warning** `software.software_identity_missing` (missing): the CycloneDX BOM gives this item no identity: no release, though the format has a place for it · `rec:3f0bf6ad4258`
- **warning** `software.software_identity_missing` (missing): the CycloneDX BOM gives this item no identity: no release or digest, though the format has a place for it · `rec:bb1051008806`

## Ambiguous fields

None.
