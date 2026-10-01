# 0033 — The M2 gate: the job probes in the sandbox, calls get scratch, short reads are the source's

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-57 (M2 gate)
- Amends: ADR 0027 §4 (`adapter_failed`'s details, a new `inspection_failed`), ADR 0028 (the
  inspect phase; `entry_skipped` retired), ADR 0029 §3–§4 (wired into the job), ADR 0030 §1, §3
  and §4 (one probe call per source; writes beneath a scratch directory; `scratch_bytes`), ADR
  0031 §2 (a per-chunk law's facts)

## Context

The M2 gate stress-tested the runtime and the adapter ABI before format adapters are written
against them (`docs/reviews/m2-stress-test.md`). Each M2 piece worked alone; the seams between
them did not all hold, and the coordinator recorded follow-ups the gate had to land:

- The job chose adapters with its own loop of per-adapter probes. The probe engine (ADR 0027)
  was never called by a job, so a tie or an unclaimed source was an event, never a finding in
  the package, and containers were never listed.
- `ShortReadError` (ADR 0029 §3) inside a call was an ordinary raise: retried, then
  `chunk_failed`, blaming the adapter for the source. A changed source got `source_changed`
  without the account `verify_artifact` gives.
- The job dropped discovery's own findings (ADR 0029 §1): a symlink was an event only, a FIFO or
  an unreadable entry a runtime `entry_skipped`, and a size change during the scan nothing.
- Scratch space (ADR 0029 §4) was never wired: no `<workspace>/scratch`, no sweep, and the
  sandbox (ADR 0030) let a call write nowhere, so the archive inspector, which spools nested
  archives to scratch, could not run inside any adapter call. Staging debris from a killed job
  stayed until a collection.
- `_chunk_series_failure` named the first failing stream and `seq` in the order the adapter
  emitted them, which a committed chunk does not keep (the latent case of ADR 0031 §2).
- ADR 0030 left open whether a degraded run's chunks may be reused by a sound one.

## Decision

1. **The job selects with the probe engine, one sandboxed call per source.** The parent reads
   the head (64 KiB) and runs `ProbeEngine.probe(reader, name, head)` in a confined child: every
   adapter's probe, the sniff and the container listing, whose decoders (zlib, bz2, lzma) read
   hostile bytes, run there. The reply is `SourceProbe.to_json()` and is never trusted:
   `ProbeEngine.source_probe_from_json` takes the sniff again from the head, requires every
   registered adapter to be asked once in id order at its own version (or to have failed), requires
   a container report exactly when the head sniffs as a container the policy opens, of that kind
   (and the `container_limit` finding when it does not open one), parses the report and the
   findings strictly (every member and finding cites this source, the nesting is within
   `ProbePolicy.max_depth`, every finding is the engine's), derives the selection
   and the concluding findings (`ambiguous`, `unsupported`, `name_mismatch`) itself, and refuses
   the reply unless the whole is exactly what the engine writes. A refused reply is a crash.
   - If the call dies, hits a limit, raises or is refused, the job emits `probe_failed` naming
     the cause and asks each adapter again in a call of its own (`ProbeEngine.probe_head`): the
     one that fails again is `neptune.probe.adapter_failed`, whose details now name an `error`,
     a `signal`, an `exit_status`, a malformed `reply`, or a `limit` and `value`; a container is
     left unopened with `neptune.probe.inspection_failed` (failed, warning) naming the cause.
     A source that changed under the probe is `source_changed` as anywhere else.
   - The engine's findings enter the package under its transform (`neptune.probe` 0.2.0), so
     ties, unclaimed sources and container problems are in every receipt (ADR 0027's
     consequence, now met). A package carries a producer's transform only if a finding cites it,
     except the runtime's on a degraded host (ADR 0030).
   - One fork per source replaces one per adapter. The cache report counts a probe call per
     adapter per source, and one more per adapter asked again.
2. **Scratch space, wired.** The workspace's private scratch root is `<workspace>/scratch`
   (`Workspace.scratch`). Every job sweeps it, and `staging/`, as it starts: what killed calls
   and writers left, never what a live process holds (`workspace_swept` event with the counts).
   A scratch root that overlaps the ingest root (a workspace inside the root) fails the job, since
   the walk would read the workspace as evidence. Each `plan` and `ingest` call gets a fresh
   `0700` directory (`scratch_space`), removed when the call returns; the call writes in a
   subdirectory of it, so it cannot remove the lock that tells a sweep the directory is live:
   - in the sandbox, a Landlock rule grants writing, making and removing regular files and
     directories beneath it (renames within it from ABI 2, truncation from ABI 3), nothing else
     anywhere; `RLIMIT_FSIZE` becomes `Limits.scratch_bytes` (default 1 GiB, 0 for none); a write
     past it fails with EFBIG, reported as `limit_exceeded` at `scratch_bytes` and not retried;
   - a host without Landlock (degraded, ABI 0) gives no call scratch: `RLIMIT_FSIZE` 0 is then
     the only bar on writes;
   - `probe` and `inspect` get none: they read a head or summarise.
   The ABI gains `neptune.adapters.contract.scratch_directory()`: the call's directory or `None`.
   It adds a function, not a parameter, so `ABI_VERSION` stays 1; output must not depend on it.
3. **Short reads, changed sources and walk entries are the source's findings.** `Raised` carries
   a well-formed `ShortReadError`'s `(source, offset, length)` across the sandbox. A short read is
   never retried. One that names this source and a range inside it is
   `neptune.discovery.short_read` for the unserved range plus `verify_artifact`'s findings, and
   the source is quarantined (`source_short_read` event); one naming any other reader or range is
   the adapter's failure at its step. A source that changed under the job also gets
   `verify_artifact`'s account (`truncated`, `grown`, `chunk_changed`) beside `source_changed`.
   Discovery's walk findings (`symlink_not_followed`, `special_file`, `vanished`, `unreadable`,
   `size_changed`) enter the package under the discovery transform; the runtime's
   `entry_skipped` is retired, so one entry has one finding, from the producer that saw it. The
   runtime's findings changed, so `RUNTIME_VERSION` is 0.2.0 (every kept chunk is judged once
   more, without the adapter, ADR 0031 §2).
4. **`inspect` crosses the sandbox.** `wire.INSPECT` encodes an `InspectResult` (its summary and
   findings) and decodes it strictly. The job still never calls `inspect`; a dry run (MVL-15)
   calls it through the runner with this codec.
5. **A per-chunk law names facts of the chunk's content, never of its emission order.** Streams
   are judged in id order and the first that breaks a law is named; within it the laws are tried
   in a fixed order (batch columns, then `seq` type, then repeated `seq`) and name the least
   offending value. A chunk judged again from its committed form fails exactly as it would fresh.
6. **Chunks are reused across isolation strengths.** A chunk committed by a degraded or an
   in-process run is reused by a sound run, as before. The chunk id names the adapter, version,
   config and bytes, and the parent decodes and checks a reply the same way under every
   isolation, so a call's output never depended on how strongly it was confined. What a degraded
   host loses is protection of files (a parser could truncate, and below ABI 1 remove or rename,
   anything the user can write), which reaches every workspace entry and every source alike, not
   the chunks that run made; partitioning chunk ids by isolation would not contain it. A
   workspace a degraded run used is trusted as far as the user trusts that run: run degraded in a
   workspace of its own when that matters.

## Alternatives considered

- **Probing adapter by adapter in the sandbox, and listing containers in the job.** Strongest
  per-adapter isolation, but N forks per source, and zlib, bz2 and lzma decoding hostile bytes in
  the job's own process. One call per source with a per-adapter fallback costs one fork, keeps
  every decoder confined, and still names the adapter that crashes.
- **Trusting the engine's reply.** It came from a process that read hostile bytes. Rebuilding it
  and comparing it with what the engine writes is cheap and leaves only the adapters' claims and
  the container's listing to the child, which is what the child is for.
- **A probe call that a compromised probe cannot use against other adapters** (a fork per adapter
  inside the engine's call). It cannot be had without N forks; a forged claim yields a wrong
  adapter or an unsupported finding, and the chosen adapter still meets the contract checks.
- **Scratch for every call, or a writable tmpfs.** `probe` and `inspect` need none; a tmpfs needs
  privileges the hosts lack. **A total disk budget for scratch**: no kernel control bounds the
  sum of files without quotas or cgroups; the per-file limit, the wall limit and removal after
  the call bound it instead (an open risk in the review).
- **`scratch` as a parameter of `plan` and `ingest`.** A signature change, so ABI 2 and every
  adapter touched, for something most adapters never use.
- **Keeping `entry_skipped` beside discovery's findings.** Two findings for one entry, from two
  producers, one of which did not see it.
- **Retrying a short read.** The bytes behind it are the same on the next attempt.
- **Folding isolation into chunk identity.** New record ids for the same evidence (ADR 0003)
  and a recomputation for nothing the output depends on; see §6.

## Consequences

- Every job's package now says what the probe engine saw: ties, unclaimed sources, container
  problems and name mismatches are findings citing their source, and such a source is read by
  `neptune.probe` alone. Packages of corpora with unclaimed files gain error findings.
- Packages with symlinks, special files or entries the walk could not read gain discovery's
  findings in place of `entry_skipped`. Runtime findings are new lineage (0.2.0).
- An archive adapter can run `inspect_archive` in its sandboxed `plan`, spooling to scratch.
- A killed job's scratch and staging are removed by the next job; a hidden package staging
  directory beside the destination is not (an open risk).
- The runtime transform's config gains `scratch_bytes` (a new lineage of runtime findings).
- Revisit if per-source probing dominates many-file corpora (cache probe results by content and
  name), if a native decoder lands in an adapter (scratch might need a read allowlist too), or if
  a host with quotas or cgroups lets scratch take a total budget.
