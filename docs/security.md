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

## File-handling controls (ADR 0029)

Everything below lives in `src/neptune/discovery/` and reports through `IngestFinding`s, never
exceptions. The fixtures are `tests/fixtures/hostile/` (README lists each file and its finding).

| Control | Where | What it guarantees |
|---|---|---|
| Walk policy | `source.py` | `LocalPath` cannot spell `..`; directories open with `O_NOFOLLOW` per component; symlinks never followed, special files never opened (ADRs 0009, 0010) |
| Walk findings | `policy.py`, `scan.py` | every symlink (`symlink_not_followed`, with its target as declared and a lexical inside/outside flag that never depends on where the root is mounted: an absolute target is outside), special file, vanished or unreadable entry, and a size that changed between walk and digest is a finding under the `neptune.discovery` transform |
| Archive limits | `archive.py` | `ArchiveLimits`: 10,000 members, 8 GiB per member, 64 GiB total, 100:1, depth 3 by default. Declared sizes (a sparse tar member's expanded size, against the chunks it stores) refused before inflating, actual bytes counted while inflating, compressed tars inflated by a bounded reader, one tar member's headers and zip link targets capped at 1 MiB, a zip central directory bounded and its entries counted before `zipfile` parses it, member names checked for traversal, link and special members recorded and never followed, nested archives spooled to scratch, any parser exception a finding with a fixed error code. Limits are the `neptune.archive` transform's config |
| Verification | `verify.py` | `verify_artifact` re-reads a source against its `SourceArtifact`: `truncated`, `grown`, `chunk_changed` findings citing the exact range; `short_read_finding` records a `ShortReadError` from `adapters.contract.read_pieces` (a reader served an adapter no bytes inside the declared size). The job records both: a short read in `plan` or `ingest` quarantines its source without a retry, and a source that changed under the job is verified (ADR 0033 §3) |
| Scratch space | `scratch.py` | `scratch_space(private_root, ingest_root=...)`: `0700`, owned by this user, never overlapping the ingest root (required, so the check always runs), removed on exit; `clear_scratch(private_root, ingest_root=...)` removes only what no live process holds. The job's private root is `<workspace>/scratch`, swept with `staging/` as each job starts; a workspace inside the ingest root fails the job (ADR 0033 §2) |

## Schedule

| Milestone | Control |
|---|---|
| M1 (MVL-2) | done: `LocalSource` walks with `O_NOFOLLOW` per component; symlinks recorded, never followed; special files never opened (ADRs 0009, 0010) |
| M2 (MVL-8) | done: containers are inspected, never extracted: member names listed verbatim and never resolved; decoding bounded by `ProbePolicy` (members, decoded bytes, depth, declared ratio); a crashing probe is a finding (ADR 0027) |
| M2 (MVL-75) | done: walk findings, archive-bomb limits, truncation detection, scratch-space policy, hostile fixture suite (ADR 0029) |
| M2 (MVL-10) | done: every probe, plan and `ingest` in a forked, confined child per call (see below); CPU, wall-time and memory limits; a crash, hang or limit hit is a finding and the job goes on; the `hostile` fixture adapter (ADR 0030) |
| M2 (MVL-16) | done: local-only mode on by default, network use refused until allowed; sources read in place and verified chunk by chunk; materialised and exported sources read through `LocalSource`, and package files opened with `O_NOFOLLOW`, regular files only (ADR 0026) |
| M2 (MVL-57) | done: the probe engine, its container decoders included, runs in the sandbox, one call per source, its reply re-derived and refused unless exact; `plan` and `ingest` write only beneath a per-call scratch directory; short reads and changed sources verified; walk findings in every package; the hostile suite through a real job (ADR 0033) |
| M6 (MVL-28/29) | malformed PDF/image safeguards; no active content execution |
| M9 | auth/profile handling for connectors; presigned uploads; idempotency keys |
| M10 (MVL-50) | consolidated adversarial suite; sandbox escape and exhaustion tests as acceptance |

Secret scanning / redaction hooks are noted in the design contract and not yet scheduled; raise an issue when
the first document adapter lands.

## Parser sandbox (ADR 0030)

By default (`JobOptions.isolation = subprocess`) each adapter call runs in a child forked for it,
confined before any adapter code runs. A sandboxed call:

- is stopped at `cpu_seconds` (60), `wall_seconds` (120) and `memory_bytes` (2 GiB of address
  space above the job's); its reply may not exceed `reply_bytes` (64 MiB, a separate cap far
  below `memory_bytes`) in size, in the number of containers and elements it decodes to (8 Mi),
  or in nesting (the JSON parser's recursion guard);
- opens no socket, starts no process or program, and signals no other process — neither directly
  (`kill`, `tgkill`, `tkill`, `rt_*sigqueueinfo`, `pidfd_send_signal`) nor through a descriptor's
  async-I/O owner (`fcntl` F_SETOWN/F_SETOWN_EX/F_SETSIG and F_SETFL with O_ASYNC, `ioctl`
  FIOSETOWN/SIOCSPGRP/FIOASYNC), the path only Landlock ABI 6 scopes, which seccomp closes on
  every ABI — including the job's own terminal, which stays readable and on which O_ASYNC alone
  would make the kernel aim SIGIO at the job's process group;
- cannot shed its parent-death signal or re-enable a core dump (`prctl` PR_SET_PDEATHSIG and
  PR_SET_DUMPABLE are refused after setup); has no controlling terminal (`setsid`: `/dev/tty`
  does not open, and `TIOCSTI` injection and `TIOCSPGRP` fail on any terminal; SIGIO through a
  terminal is the O_ASYNC filter above, not `setsid`); traces nothing and enters no namespace
  (seccomp);
- writes no byte to any file outside its scratch directory, changes no file's mode, owner, times
  or xattrs and punches no bytes (`chmod`/`chown`/`utimensat`/`*xattr`/`fallocate` refused —
  Landlock covers none of these), and, under Landlock, creates, truncates, renames or removes
  nothing outside it. A `plan` or `ingest` call gets a fresh `0700` scratch directory under
  `<workspace>/scratch`, removed when it returns: Landlock grants regular files and directories
  beneath it and nothing else, and each file is bounded by `scratch_bytes` (1 GiB; a write past
  it is `limit_exceeded`). `probe` and `inspect` get none, and neither does any call on a host
  without Landlock, where `RLIMIT_FSIZE` stays 0 (ADR 0033 §2);
- holds only its source's read-only descriptor and its reply pipe; prints to `/dev/null`;
  leaves no core dump; dies if the job dies;
- answers in JSON that the job decodes strictly, bounded, and checks like any adapter's output;
  only the job writes the workspace, so a killed call leaves nothing behind.

The sandbox fails closed below **Landlock ABI 3**, the floor at which a source is immutable: ABI 1
already blocks a file being created, written, removed, renamed or relinked, ABI 3 adds truncation,
and below ABI 1 procfs plus Yama `ptrace_scope` 0 can even reach the parent's memory. A host below
it fails the job unless `allow_degraded_sandbox` is set, which runs anyway and records the exact
guarantees lost in the `sandbox_ready` event and the receipt's runtime transform. Ubuntu 24.04
(kernel 6.8, ABI 4) is above the floor; Debian 12 (kernel 6.1, ABI 2) and RHEL 9 are below it and
need a 6.2+ kernel or degraded mode by choice. A host that cannot apply the controls at all (not
Linux, no seccomp filter for its architecture) also fails the job before any source is read;
`isolation = in_process` runs adapters unconfined, by explicit choice. Residual risks: a
compromised parser can read files the user can read and put them into its own output; and while
the job decodes one reply it holds it three times (the read buffer, the payload and the text the
parser reads: up to 3 × `reply_bytes`) plus the Python objects it decodes to, which the 8 Mi value
cap bounds, not the byte cap. Measured at the defaults, a hostile reply peaks the job at about
1 GiB (one object of distinct keys filling the byte cap, which the parser memoises as it decodes),
0.3 to 0.6 GiB for lists of short strings, floats or empty containers, and 256 MiB for one 64 MiB
string. A lower value cap would refuse legitimate replies first: integer and boolean series cells
cost 2 to 6 bytes each on the wire.

Two more residual risks came with the M2 gate (ADR 0033): scratch is bounded per file, not in
total, so a hostile call can fill the disk at its write rate until its wall limit (removed when
it returns); and the probe engine asks every adapter in one call per source, so a parser that a
head compromises can forge the other adapters' claims in that reply. The job re-derives
everything but the claims and the container listing, and a forged claim yields at worst a wrong
adapter (whose own calls are sandboxed and checked) or an `unsupported` finding.
