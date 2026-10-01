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

## Schedule

| Milestone | Control |
|---|---|
| M1 (MVL-2) | done: `LocalSource` walks with `O_NOFOLLOW` per component; symlinks recorded, never followed; special files never opened (ADRs 0009, 0010) |
| M2 (MVL-10) | subprocess isolation, CPU/memory/time limits, archive-bomb limits, temp-file policy, crash capture; seed adversarial fixtures |
| M2 (MVL-16) | done: local-only mode on by default, network use refused until allowed; sources read in place and verified chunk by chunk (ADR 0026) |
| M6 (MVL-28/29) | malformed PDF/image safeguards; no active content execution |
| M9 | auth/profile handling for connectors; presigned uploads; idempotency keys |
| M10 (MVL-50) | consolidated adversarial suite; sandbox escape and exhaustion tests as acceptance |

Secret scanning / redaction hooks are noted in the design contract and not yet scheduled; raise an issue when
the first document adapter lands.
