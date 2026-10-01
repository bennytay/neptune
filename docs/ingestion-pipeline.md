# Ingestion pipeline

Status: stage contract agreed; runtime implementation is M2 (MVL-6, MVL-9, MVL-16).

## Stages

| # | Stage | Responsibility | Owner | Issue |
|---|---|---|---|---|
| 1 | discover | enumerate candidate sources through a `Source` (local FS now, object store later); apply ignore, symlink and traversal policy | discovery | MVL-2, MVL-45 |
| 2 | fingerprint | size, magic bytes, streaming sha256 + per-chunk hashes; emit `SourceArtifact` / `SourceRevision` | identity | MVL-2 |
| 3 | probe | ask registered adapters for confidence + reasons; rank; surface ties as ambiguity findings | discovery + adapters | MVL-8 |
| 4 | inspect | cheap per-source summary (streams, extents, counts) without full parse | adapters | MVL-7 |
| 5 | group | propose run/session groupings from filesystem signals (v0) and later from evidence (M7) | discovery | MVL-13, MVL-34 |
| 6 | plan | adapters emit chunks with deterministic ids and cost estimates | adapters | MVL-7 |
| 7 | ingest | per-chunk pure parse → canonical records + findings; runtime skips committed chunks (resume) and cached ones | runtime + adapters | MVL-6, MVL-9 |
| 8 | store | commit each chunk's output to the local workspace; assemble records (JSON Lines), time-series (Parquet) and blobs (CAS) into the ingest package (ADR 0026) | store | MVL-5, MVL-16 |
| 9 | validate | cross-source integrity checks over the store; findings, not exceptions | validate | MVL-41 |
| 10 | receipt | core computed from the package's records (store); volatile envelope (runtime); human and machine renderings | store + runtime | MVL-5, MVL-6 |

Alignment (clocks, frames, identities, bindings) is a separate pass after ingestion (M7); it produces new
records with their own provenance and never rewrites what stages 1–10 produced.

## Runtime vs adapter responsibilities

| Concern | Owner | Mechanism |
|---|---|---|
| Resume after crash | runtime | deterministic chunk ids + the workspace's committed chunks (ADR 0026) |
| Cache | runtime | key = chunk id, which covers (source id, adapter id, adapter version, config hash, context) (ADR 0024 §4) |
| Partial failure | runtime | per-chunk isolation; adapter crash → finding, job continues |
| Sandboxing | runtime | subprocess with CPU/memory/time limits (MVL-10) |
| Adapter-local problems | adapter | `IngestFinding`s in the chunk output |
| Cross-source validation | validate | runs over the store after all chunks |
| Explanation | runtime | assembles `probe`/`plan` results + descriptors into the receipt |

## Dry-run

`explain`/dry-run (MVL-15) executes stages 1–6 only and renders the plan. It must never call `ingest`.

## Determinism contract

Given identical source bytes, adapter versions and config, stages 2–10 produce byte-identical package
contents. Ordering is defined everywhere (sorted paths, sorted ids, sorted keys). Wall-clock, host and
duration live only in the receipt envelope.
