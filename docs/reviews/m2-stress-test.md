# M2 gate: stress test of the runtime and the adapter ABI

- Date: 2026-10-01 · Issue: MVL-57 · Reviewed: ADRs 0008 and 0024–0031, `neptune.runtime`,
  `neptune.discovery`, `neptune.store`, `neptune.adapters`, and the fixture adapters
  (`tests/fixtures/adapters/`)
- Method: every scenario was run, not walked on paper: through the real job with the real sandbox
  (ADR 0030) on Linux 7.0, x86_64, Landlock ABI 8, 20 cores, sources on tmpfs. Each is a test in
  `tests/integration/test_m2_gate_stress.py`; the scale scenarios are the scripts
  `tests/fixtures/runtime/stress_large_source.py` and `stress_archive_passes.py`, whose output is
  recorded below.
- Outcome: the runtime and the ABI hold for every scenario once eight defects were fixed here (ADR
  0033). The seams between the M2 pieces were where they failed: the job never ran the probe engine,
  never recorded discovery's findings, never wired scratch space, and treated a short read as the
  adapter's fault. One ABI addition (`contract.scratch_directory()`, `ABI_VERSION` stays 1), one
  decision on archives (ADR 0032). M3 and M4 may start once this is merged and `main` is tagged
  `m2-gate`.

## Scenarios

| Scenario | Test | What happened | Verdict |
|---|---|---|---|
| Kill and resume mid-parse of a multi-chunk source | `test_a_job_killed_mid_parse_resumes_to_the_clean_package` | The job is SIGKILLed while a sandboxed `ingest` sleeps inside a 7-chunk source. The call dies with it (parent-death signal), no partial chunk exists, the call's scratch directory is left and the next job sweeps it, reuses every committed chunk and plan, and writes a package byte-identical to a clean run's. | holds, after D4 |
| Parser crash inside the sandbox | `test_a_crash_inside_the_sandbox_costs_one_chunk_and_the_rerun_retries_only_it` | SIGSEGV on one line: `adapter_crashed` after two attempts; the source's other four chunks commit; the rerun makes exactly the two `ingest` calls of that chunk; same package. A crash in a probe (`test_a_probe_that_crashes_takes_only_its_adapter_out`) and in a container member's probe (`test_a_container_whose_member_crashes_a_probe_is_left_closed`) take out one adapter or leave one container closed. | holds |
| An adapter returning findings for half its chunks | `test_findings_on_half_the_chunks_land_with_everything_else`, `test_the_same_finding_from_two_chunks_quarantines_its_source_and_says_which` | Five of ten chunks carry only a finding: each commits, is reused, cites its own line; the source lands with five rows. An adapter that emits one whole-source finding from every bad chunk breaks law 9 and is quarantined with `output_invalid` naming `finding_repeated` and the chunk. | holds; law 9 is sharp, see L1 |
| Two adapters, one extensionless file, equal confidence | `test_two_adapters_tied_on_an_extensionless_file_are_a_finding_never_a_guess` | Before the gate the tie was an event and nothing in the package. Now `neptune.probe.ambiguous` names both adapters, the shared confidence and each one's reasons; nothing plans the file; the receipt says only the probe engine looked; a second job in a fresh workspace writes the same package. | holds, after D5 |
| Cache hit after a rename | `test_a_renamed_source_is_a_cache_hit` | A source moved and renamed: zero `plan` and `ingest` calls, every plan, chunk and derivative a hit, the same evidence records; the old location is recorded absent. | holds |
| Cache miss after an adapter version bump | `test_an_adapter_version_bump_misses_exactly_its_own_chunks` | `transform_changed` naming `adapter_version`; that adapter's chunks, verdict and series file recomputed, nothing of the other adapter's; new record ids (ADR 0003). | holds |
| Config change invalidating exactly one derivative | `test_a_config_change_invalidates_exactly_one_derivative` | `text.block_rule` changed: one derivative missed (the text source's verdict), the tally source's verdict and series file held. | holds |
| A large source: inspect cost, plan size, peak memory, time to first receipt | `test_a_large_sparse_source_is_inspected_cheaply_and_planned_small` and the script | See the measurements below: inspecting costs milliseconds at any size, the plan is about 330 bytes a chunk, memory stays under 200 MiB, and the first receipt costs three passes over the bytes. | holds; R1, R2 |
| The hostile suite end to end through a real job (MVL-57 follow-up) | `test_the_hostile_suite_through_a_real_job` | Loops, escaping links, a FIFO, odd names, a 64-deep tree, bombs, traversal names, truncated, corrupt and encrypted archives: the job commits, the tree is untouched, the canary outside the root is never read, the 11 links and the FIFO are discovery's findings, every archive is `unsupported` with its container findings, no call fails, scratch and staging end empty, and a second job writes the same package. Before the gate the links were not in the package at all. | holds, after D1 |
| Archive inspection where an archive adapter will run it | `test_the_archive_inspector_runs_in_a_sandboxed_call_through_its_scratch` | `inspect_archive` in a sandboxed call spools a 2 MiB nested zip through the call's scratch directory and leaves nothing. Before the gate it could not run in any call: the sandbox let nothing be written. | holds, after D8 |

## Measurements

`python tests/fixtures/runtime/stress_large_source.py WORKDIR GIB` (a sparse frame log of 4 MiB
frames, 16 per chunk, through the sandboxed job, then again) and `... WORKDIR many FILES`.

| | 16 GiB | 100 GiB | 1,000 small text files |
|---|---|---|---|
| fingerprint (hash pass) | 14.3 s | 88.9 s (1.2 GB/s) | — |
| inspect phase (head + one probe call per source) | 0.009 s | 0.009 s | 3.1 s (3.1 ms a source) |
| adapter `inspect` through the sandbox | 0.003 s | 0.004 s | — |
| plan | 8.2 s | 51.2 s | 4.0 s |
| chunks · plan kept | 257 · 86 KB | 1,601 · 536 KB | 2,000 chunks |
| parse + normalize | 12.0 s | 74.7 s | 8.9 s (4.4 ms a chunk) |
| assemble (merge series, admit) | 0.7 s | 4.2 s | — |
| time to first receipt | 35.3 s | 219 s | 18.1 s |
| rerun, every chunk a hit | 14.5 s, 0 plan and ingest calls | 89.3 s, 0 plan and ingest calls | 5.5 s |
| peak RSS, job · largest call | 120 · 80 MiB | 178 · 123 MiB | 103 MiB |

- Inspecting is constant: the head read and one fork, whatever the size. The plan grows with
  chunks, not bytes. Memory grows with chunk count (the plan and the cross-chunk ids), not bytes.
- The first receipt costs three passes over the bytes: the fingerprint hash, then `plan` and the
  `ingest` calls, which read a 12-byte header per frame, but `LocalReader` verifies each 8 MiB
  piece of the artifact it touches before serving a byte (ADR 0026), so headers spread through a
  file re-hash all of it. A rerun costs the hash alone. At 100 GB that is 3.7 minutes first and
  1.5 minutes after.
- A call costs about 3 to 4.4 ms of fork, confinement and JSON on this host, so many small files
  are bound by calls: 18 ms a file for probe, plan and two chunks. The job runs one call at a time.
- The probe's container listing reads a fixed budget (1.2 MB, under 1 ms) of a gzip-compressed tar
  of 64 MiB or 256 MiB; the hardening inspector inflates all of it (0.04 s and 0.15 s, about
  1.8 GB/s). `python tests/fixtures/runtime/stress_archive_passes.py WORKDIR 64 256` generates
  the archives and measures both (ADR 0032).

## Questions

- **Will M4 adapters force a runtime or ABI rewrite?** No. The gate's one ABI change is additive
  (`scratch_directory()`), so `ABI_VERSION` stays 1 and no adapter signature moves. An archive
  adapter now has what it needs (scratch, the inspector inside its call); an MCAP adapter plans
  from its summary section and reads chunk by chunk, which the cost model above rewards.
- **Is every failure a finding about one source?** Yes, and now in the package: a crash, a hang,
  a limit (including scratch), a short read, a changed file, a tie, an unclaimed file and a walk
  entry not read each cite their source, from the producer that saw them. The job fails only for
  itself (root, destination, workspace, host); a workspace inside its root is now one of those.
- **Is resume safe at any instant?** Yes, including inside a sandboxed call and while scratch is
  in use: nothing a call does reaches the workspace, and the next job sweeps what a kill leaves.
- **Does the cache do near-zero work for unchanged sources?** Near: zero adapter calls and no
  merge, but every source is fingerprinted and probed again (R2).
- **Is the output independent of the cache's contents?** Yes, and now also of the order an adapter
  emits a chunk's rows in (D7): a judged chunk fails exactly as a fresh one.
- **Is the sandbox enough for M4's native decoders?** For crashes, hangs, memory and writes, yes,
  and the probe's container decoders now run inside it too. Reads stay open (ADR 0030); revisit
  with the first native decoder.

## Findings

### Defects found and fixed here (ADR 0033)

- **D1. Discovery's findings never reached a job's package.** ADR 0029 §1 put every symlink,
  special file and unreadable entry in receipts; the job kept them as events or a runtime
  `entry_skipped`, and dropped `size_changed`. Now discovery's findings are recorded under its
  transform and `entry_skipped` is retired (runtime 0.2.0).
- **D2. A short read was blamed on the adapter, whatever the source held.** `ShortReadError`
  crossed the sandbox as a plain raise: retried, then `chunk_failed`. Now it carries its range,
  and the job verifies the source: one that no longer matches its artifact is
  `neptune.discovery.short_read` with `verify_artifact`'s account, never retried; over an intact
  source the short read is the adapter's own window's, and stays its failure.
- **D3. A changed source had no account of what changed.** `source_changed` now comes with
  `verify_artifact`'s `truncated`, `grown` or `chunk_changed`, as ADR 0029 §3 promised.
- **D4. What killed jobs left stayed.** No scratch root existed, and staging debris waited for a
  collection. `<workspace>/scratch` exists and each job sweeps it and `staging/` as it starts.
- **D5. The job never ran the probe engine.** Ties and unclaimed sources were events only, and
  containers were never listed, so ADR 0027's findings never reached a package. The job now runs
  the engine, one sandboxed call per source, and reads the reply back strictly.
- **D6. The engine would have decoded hostile containers in the job's process.** Its zlib, bz2
  and lzma decoding runs in the probe call's sandbox; a crash there leaves one container closed.
- **D7. A per-chunk law named facts in emission order.** The stream and `seq` a series failure
  named depended on the adapter's batch order, which a committed chunk does not keep. Now the
  least stream and value are named, whatever the order.
- **D8. Nothing could write in the sandbox, so the archive inspector could run nowhere.** Plan and
  ingest calls get a private scratch directory, bounded per file.

The gate's own new code was stress-tested the same way: forging the probe reply 18 ways
(`tests/unit/discovery/test_probe_reply.py`) and a code review caught three gaps before merge, all
closed: a container member citing another source, a container report dropped or invented against
the head, and a call able to remove the lock of its own scratch directory (which would let a
concurrent job's sweep delete it mid-call).

### Decisions

- Probing and archive inspection stay two passes; the inspector belongs to an archive adapter's
  call (ADR 0032).
- One probe call per source with a per-adapter fallback, its reply re-derived (ADR 0033 §1).
- Scratch for `plan` and `ingest` only, per file `scratch_bytes`, none without Landlock (§2). An
  adapter that needs scratch and has none raises `ScratchUnavailableError`, so nothing that
  depends on scratch is ever committed (law 11).
- A short read is the source's, never retried, when the source no longer matches its artifact,
  and the adapter's otherwise; walk entries are discovery's findings (§3).
- A degraded run's chunks may be reused by a sound run: output never depended on isolation; the
  degraded risk is to files, workspace included, not to chunk identity (§6).

### Accepted limitations (not fixed; revisit if they bite)

- **L1.** Law 9 quarantines a whole source for one finding emitted by two chunks. Correct by the
  contract and named precisely in the finding; adapter authors cite the chunk's own bytes.
- **L2.** A probe that hangs costs `wall_seconds` twice: once in the source's call, once alone.
- **L3.** `inspect` crosses the sandbox but the job does not call it; MVL-15 does.

### Open risks

- **R1. Read amplification.** Verification is per 8 MiB artifact piece, so an adapter whose reads
  spread across a file re-hashes all of it: three passes for the first receipt here. Adapters
  should plan from indexes; M9 may verify smaller pieces or let a call reuse a verified digest.
- **R2. Per-call cost on many small files.** 3 to 4.4 ms a call, one call at a time, and every
  unchanged source is probed again on each job (100,000 files: about 5 minutes of probing per
  rerun). M9's scheduler runs calls in parallel; a probe result could be a derivative keyed by
  content, name and the engine's transform.
- **R3. Scratch is bounded per file, not in total.** A hostile call can write at disk speed until
  its wall limit; the directory is removed when it returns. Quotas or cgroups would bound it.
- **R4. One probe call per source lets a compromised probe forge other adapters' claims.** The
  job re-derives everything else; the worst case is a wrong adapter, itself sandboxed and checked,
  or an `unsupported` finding.
- **R5. A degraded host can damage the workspace.** Committed chunk files are not hashed on load,
  so a parser truncating one at a line boundary on a host without Landlock ABI 3 would drop
  records silently. Degraded mode is opt-in and recorded; run it in its own workspace.
- **R6. A package staged beside its destination is left if the job is killed** (`.name.<hex>`):
  hidden and never the destination, but nothing removes it.

### Open for M3 and later

- **O1.** MVL-15 renders `SourceProbe.to_json()` and calls `inspect` through `wire.INSPECT`.
- **O2.** M9: parallel calls, a probe cache, verification granularity (R1, R2).
- **O3.** The first archive adapter fuses `inspect_archive` with its own read (ADR 0032).
