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
- Adapters declare resource expectations in their descriptor; the runtime enforces limits per call
  (job-wide values today, ADR 0030).
- Every adapter call runs in a confined child process unless in-process isolation is chosen
  explicitly; isolation is a runtime feature, not an adapter rewrite (ADR 0030).
- Crashes become findings; a hostile file cannot fail the job or escape its chunk.
- Local-only mode is a first-class configuration: no network, no cloud sync, receipts portable.
- Raw customer data is never written to logs. Receipts reference blobs by id, not content.

## Schedule

| Milestone | Control |
|---|---|
| M1 (MVL-2) | done: `LocalSource` walks with `O_NOFOLLOW` per component; symlinks recorded, never followed; special files never opened (ADRs 0009, 0010) |
| M2 (MVL-8) | done: containers are inspected, never extracted: member names listed verbatim and never resolved; decoding bounded by `ProbePolicy` (members, decoded bytes, depth, declared ratio); a crashing probe is a finding (ADR 0027) |
| M2 (MVL-10) | done: every probe, plan and `ingest` in a forked, confined child per call (see below); CPU, wall-time and memory limits; a crash, hang or limit hit is a finding and the job goes on; the `hostile` fixture adapter (ADR 0030) |
| M2 (MVL-75) | archive-bomb limits, temp-file policy, path/symlink and truncation hardening; seed adversarial fixtures |
| M2 (MVL-16) | done: local-only mode on by default, network use refused until allowed; sources read in place and verified chunk by chunk; materialised and exported sources read through `LocalSource`, and package files opened with `O_NOFOLLOW`, regular files only (ADR 0026) |
| M6 (MVL-28/29) | malformed PDF/image safeguards; no active content execution |
| M9 | auth/profile handling for connectors; presigned uploads; idempotency keys |
| M10 (MVL-50) | consolidated adversarial suite; sandbox escape and exhaustion tests as acceptance |

Secret scanning / redaction hooks are noted in the design contract and not yet scheduled; raise an issue when
the first document adapter lands.

## Parser sandbox (ADR 0030)

By default (`JobOptions.isolation = subprocess`) each adapter call runs in a child forked for it,
confined before any adapter code runs. A sandboxed call:

- is stopped at `cpu_seconds` (60), `wall_seconds` (120) and `memory_bytes` (2 GiB of address
  space above the job's), and its reply may not exceed `memory_bytes`;
- opens no socket, starts no process or program, signals no other process, traces nothing, and
  enters no namespace (seccomp); writes no byte to any file (`RLIMIT_FSIZE` 0) and, where the
  kernel has Landlock, creates, truncates, renames or removes nothing;
- holds only its source's read-only descriptor and its reply pipe; prints to `/dev/null`;
  leaves no core dump; dies if the job dies;
- answers in JSON that the job decodes strictly and checks like any adapter's output; only the
  job writes the workspace, so a killed call leaves nothing behind.

A host that cannot apply these controls (not Linux, no seccomp filter for its architecture) fails
the job before any source is read; `isolation = in_process` runs adapters unconfined, by choice.
Residual risks: a compromised parser can read files the user can read and put them into its own
output, and the job holds a reply of up to `memory_bytes` while it decodes it.
