# Security

Status: threat model agreed 2026-09-30; controls land per the schedule below.

## Threat model

All ingested data is untrusted. Sources may be malformed by accident (field data) or by design (a customer
upload, a shared bucket). Ingestion runs on developer machines with access to sensitive customer data and,
later, in shared infrastructure.

Threats: parser crashes and memory/CPU exhaustion from malformed binaries; archive bombs; path traversal and
symlink escape during discovery; dangerous embedded content in documents; leakage of sensitive data through
receipts, logs, caches or cloud sync; supply-chain risk from parser dependencies.

## Principles

- Discovery enforces path, symlink and archive policy before any adapter sees a file.
- Adapters declare resource expectations in their descriptor; the runtime enforces limits.
- The adapter contract is subprocess-safe so isolation is a runtime feature, not an adapter rewrite.
- Crashes become findings; a hostile file cannot fail the job or escape its chunk.
- Local-only mode is a first-class configuration: no network, no cloud sync, receipts portable.
- Raw customer data is never written to logs. Receipts reference blobs by id, not content.

## File-handling controls (ADR 0028)

Everything below lives in `src/neptune/discovery/` and reports through `IngestFinding`s, never
exceptions. The fixtures are `tests/fixtures/hostile/` (README lists each file and its finding).

| Control | Where | What it guarantees |
|---|---|---|
| Walk policy | `source.py` | `LocalPath` cannot spell `..`; directories open with `O_NOFOLLOW` per component; symlinks never followed, special files never opened (ADRs 0009, 0010) |
| Walk findings | `policy.py`, `scan.py` | every symlink (`symlink_not_followed`, with its target as declared and a lexical inside/outside flag), special file, vanished or unreadable entry, and a size that changed between walk and digest is a finding under the `neptune.discovery` transform |
| Archive limits | `archive.py` | `ArchiveLimits`: 10,000 members, 8 GiB per member, 64 GiB total, 100:1, depth 3 by default. Declared sizes refused before inflating, actual bytes counted while inflating, compressed tars inflated by a bounded reader, pax headers and zip link targets capped at 1 MiB, member names checked for traversal, link and special members recorded and never followed, nested archives spooled to scratch. Limits are the `neptune.archive` transform's config |
| Verification | `verify.py` | `verify_artifact` re-reads a source against its `SourceArtifact`: `truncated`, `grown`, `chunk_changed` findings citing the exact range; `short_read_finding` records a `ShortReadError` from `adapters.contract.read_pieces` (a reader served an adapter no bytes inside the declared size) so one plan or chunk fails alone and the job goes on |
| Scratch space | `scratch.py` | `scratch_space(private_root)`: `0700`, owned by this user, never overlapping the ingest root, removed on exit; `clear_scratch` on resume removes only what no live process holds |

Not in place yet (MVL-10): subprocess isolation, CPU/memory/time limits, crash capture.

## Schedule

| Milestone | Control |
|---|---|
| M1 (MVL-2) | done: `LocalSource` walks with `O_NOFOLLOW` per component; symlinks recorded, never followed; special files never opened (ADRs 0009, 0010) |
| M2 (MVL-75) | done: walk findings, archive-bomb limits, truncation detection, scratch-space policy, hostile fixture suite (ADR 0028) |
| M2 (MVL-10) | subprocess isolation, CPU/memory/time limits, crash capture (file handling and the adversarial seed landed with MVL-75) |
| M2 (MVL-16) | local-only mode |
| M6 (MVL-28/29) | malformed PDF/image safeguards; no active content execution |
| M9 | auth/profile handling for connectors; presigned uploads; idempotency keys |
| M10 (MVL-50) | consolidated adversarial suite; sandbox escape and exhaustion tests as acceptance |

Secret scanning / redaction hooks are noted in the design contract and not yet scheduled; raise an issue when
the first document adapter lands.
