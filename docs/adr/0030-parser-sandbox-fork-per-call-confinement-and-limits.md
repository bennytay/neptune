# 0030 — The parser sandbox: a confined child process per adapter call, limits, and crash findings

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-10
- Extends: ADR 0008 §2 and §5 (adapters never touch the filesystem or network; the runtime owns
  sandboxing), ADR 0028 §3 (faults are findings about one source)

## Context

Every source Neptune reads is hostile (non-negotiable 9). An adapter is reviewed code, but the
bytes it parses are not: a malformed MCAP chunk can make a decoder segfault, loop forever or
allocate without bound, and a native decoder with a memory-safety bug can be made to run code.
ADR 0028 turns an adapter's *exception* into a finding, but a segfault, a hang or an exhausted
host kills or stalls the job itself, so one file could still stop every other source from
landing. ADR 0008 kept the contract subprocess-safe so isolation would be a runtime feature, not
an adapter rewrite. MVL-10 (its execution half; file handling is MVL-75) asks for that feature:
process isolation, CPU, memory and time limits, and crashes captured as findings inside a running
job. Its acceptance: a parser that crashes, hangs or allocates without bound yields a finding and
the job completes for every other source and chunk; killing the sandboxed process leaves no
partial chunk in the workspace.

## Decision

1. **Every adapter call goes through a runner** (`neptune.runtime.sandbox`): each probe, each
   plan and each chunk's `ingest` (the job never calls `inspect`; MVL-15's dry run will, through
   the same runner). A runner returns one of four outcomes, never an exception: `Returned`,
   `Raised` (the class, never the text; a contract error; a changed source; the type of a wrong
   result; a result that cannot be encoded), `Crashed` (a signal, an exit status, or a reply that
   does not decode) and `Exceeded` (a limit and its value). `JobOptions.isolation` picks the
   runner: **`subprocess` by default**; `in_process` (unconfined, unlimited) only when asked, for
   tests and trusted adapters. There is no automatic fallback: a host that cannot confine a call
   fails the job before any source is read, and the error says to ask for `in_process`.
2. **One fork per call.** The child is a copy of the job process, so it runs the very adapter
   object the job built: nothing to import by name or pickle, and test or third-party adapters
   need nothing special. Each call starts from the job's state and dies with its own, so no
   state crosses chunks (ADR 0008's purity law, now enforced) and every limit is per call. A
   call costs about 2 ms on top of the adapter's work. CPython warns about forking a process
   with threads (pyarrow's pool, after the first commit): the child runs only adapter code and
   the reply writer, glibc, jemalloc and Arrow reset their locks at fork, and a deadlock there
   would end at the wall limit as a finding. The test suite filters that one warning.
3. **The child is confined before any adapter code runs** (`neptune.runtime.confine`, Linux,
   x86_64 and aarch64):
   - killed if the job dies (`PR_SET_PDEATHSIG`), and `setsid` so it has no controlling terminal:
     `/dev/tty` does not open, and `TIOCSTI` input injection and `TIOCSPGRP` fail on any
     terminal, since both need the caller's own. `setsid` does not stop SIGIO: the job's
     `/dev/pts/N` stays readable, and `O_ASYNC` on it makes the kernel aim SIGIO at the
     terminal's foreground group, the job's; the seccomp filter below closes that on every ABI
     (ABI 6 scoping also blocks the delivery). Standard streams to `/dev/null`; every other
     descriptor closed except the reply pipe and the source's read-only descriptor;
   - `RLIMIT_CPU`, `RLIMIT_AS` (the address space at fork plus the memory budget),
     `RLIMIT_CORE` 0 and not dumpable (no core dump, even through a piped `core_pattern`, holds
     source data), `RLIMIT_FSIZE` 0 (no byte is written to any file);
   - `no_new_privs`, then Landlock where the kernel has it (5.13+): no file or directory created,
     written, truncated, renamed or removed; from ABI 4 no TCP; from ABI 6 no signal or abstract
     socket outside the sandbox;
   - a seccomp filter, built in-process as classic BPF: no `socket`; no `fork`, `vfork`,
     `clone` without `CLONE_THREAD`, `clone3` (ENOSYS, so libc falls back), `execve`,
     `execveat`; no `kill` or `tgkill` but to itself, no `tkill`, `rt_*sigqueueinfo` or
     `pidfd_send_signal`. It also closes the async-I/O path to a signal, which only Landlock
     ABI 6 scopes, with argument filters that hold on every ABI: `fcntl`
     `F_SETOWN`/`F_SETOWN_EX`/`F_SETSIG`, `fcntl` `F_SETFL` with `O_ASYNC` (a JSET on the flag
     argument's low word; any other `F_SETFL` is allowed) and `ioctl`
     `FIOSETOWN`/`SIOCSPGRP`/`FIOASYNC` get EPERM, as do `prctl` `PR_SET_PDEATHSIG` (so a child
     cannot shed its parent-death signal and outlive a killed job) and `PR_SET_DUMPABLE` (so it
     cannot re-enable a core dump after confinement). No file-metadata change (`chmod`, `chown`, `utimensat` and the `*xattr`
     family) and no `fallocate` either: Landlock covers none of those, so without the filter a
     parser could make the source unreadable, world-write a user's file, punch its bytes, or
     retime it to defeat change detection even above the Landlock floor. No `ptrace`,
     `process_vm_*`, `pidfd_getfd`; no `unshare` or `setns`; no `bpf`; no `io_uring` (ENOSYS).
     Another ABI's syscalls kill the process. Threads work.

   Reads stay open: lazy imports, codecs, time zone data and shared libraries keep working, and
   nothing the child reads can leave but through its reply. Seccomp and `RLIMIT_FSIZE` are the
   floor on every Linux. **The sandbox fails closed below Landlock ABI 3**: below it a source is
   not immutable. ABI 1 already blocks a file being created, written, removed, renamed or
   relinked (reparenting is denied while the REFER right is not handled, below ABI 2; a
   same-directory rename or link needs the MAKE and REMOVE rights, handled and granted to
   nothing); ABI 3 adds truncation; and below ABI 1 there is no Landlock, and procfs with Yama
   `ptrace_scope` 0 reaches the parent's memory, which seccomp does not cover. So
   `JobOptions.isolation = subprocess` on such a host raises `JobError`. `allow_degraded_sandbox`
   runs on it by explicit choice and records the exact guarantees lost in the `sandbox_ready`
   event and, hashed into the runtime transform (even on a run with no findings), in the receipt,
   so degraded output never shares a lineage with a sound run. ABI 5
   adds device-`ioctl` scoping and ABI 6 Landlock signal and abstract-socket scoping; neither is
   part of the floor, since the child holds no device descriptor and seccomp closes the signal
   path on every ABI. The `sandbox_ready` event says which ABI applied. The child writes one
   status byte once confined; a control that fails before that byte is the host's fault
   (`JobError`), never a finding, and a hostile adapter, which runs only after it, cannot fake it.
4. **Limits are job-wide, per call, with defaults**: `cpu_seconds` 60, `wall_seconds` 120,
   `memory_bytes` 2 GiB of address space above what the process held at fork, and `reply_bytes`
   64 MiB — a separate cap far below `memory_bytes`, so one hostile call that emits a giant reply
   cannot exhaust the job as the parent reads and decodes it. The parent reads the reply into one
   buffer (no list joined into a second copy), decodes it once, and bounds the decode in count and
   depth as well as bytes: a reply under the byte cap that packs it with empty containers or short
   strings would still build millions of objects, so a count past 4 Mi values (the byte cap at 16
   bytes a value; the densest legitimate reply the test suite produces averages 16.4) is refused
   as `reply_bytes` too, as is a reply nested past the JSON parser's recursion guard (a
   `RecursionError` anywhere in the decode), so neither is a crash the job retries. An exact
   depth scan before parsing would cost 30 to 90% of the parse on a large legitimate reply; the
   parser's guard costs nothing. The parent enforces wall time (it kills the child at the
   deadline) and the reply caps; the kernel enforces the rest (SIGXCPU, then SIGKILL a second later; `MemoryError`
   or a failed allocation). They are the runtime transform's config with `attempts`, `isolation`
   and, on a degraded host, the guarantees lost (`neptune.runtime` 0.1.0), so the receipt names
   the policy whenever a runtime finding is in it; in-process runs record no limits, since none
   bound them, and refuse to be given any.
5. **The reply is data, never code.** The child sends JSON: records and findings as their
   `to_json` (ADR 0024 §6 makes every record read back as itself), series cells by column type
   with every float as its eight IEEE-754 bytes (NaN payloads, infinities and `-0.0` cross
   exactly), plans as their chunks, probe results as theirs. The parent parses it with the
   standard library and rebuilds it only through the model's own parsers, then runs the same
   contract checks it runs on any adapter's output. Only the parent writes the workspace, after
   those checks, so a child killed at any instant leaves nothing: no staged directory, no
   chunk. Output is byte-identical in either isolation.
6. **Findings name the adapter, its version, the step, the chunk and the cause**, never text.
   A raise is `chunk_failed` or `plan_failed` as before (ADR 0028), at `ingest` or `plan`, at
   `ingest_result`/`plan_result` for a wrong type, or at the check step for a result that
   cannot be encoded, as the check would have refused it; both now carry the adapter's
   `version`. Two codes are new, both failed/error and quarantining their source:
   `adapter_crashed` (`signal`, `exit_status` or `reply`: malformed) and `limit_exceeded`
   (`limit`, `value`). A crash of `ingest` is retried, since it may be the host's (an OOM
   killer); a limit is not, since the same bytes hit it again; neither is a plan's. The next job
   tries again in every case, since nothing failed is committed. A probe that crashes or hits a
   limit is a `probe_failed` event naming the cause, and that adapter leaves the candidates.

## Alternatives considered

- **Spawn or forkserver per call.** Clean children, but the adapter must be importable by name
  and picklable (test adapters are loaded from files, some are built with constructor
  arguments), and starting an interpreter with Neptune imported costs 50 to 300 ms per call.
- **A pool of long-lived workers.** Amortises start-up, but CPU and address-space limits are
  per process, not per call, state leaks from one chunk into the next, and one crash takes every
  call in flight. M9's scheduler can revisit it with measurements.
- **Pickle for the reply.** Unpickling what a compromised child wrote is code execution in the
  job. Arrow IPC for series would put a native parser of hostile bytes in the parent.
- **User and network namespaces.** The strongest isolation, but Ubuntu 24.04+ restricts
  unprivileged user namespaces (AppArmor), so it is unavailable on the hosts Neptune targets.
- **cgroups v2 `memory.max`.** Accounts resident memory, not address space, but needs a
  delegated cgroup (systemd), which a developer's shell or CI may not have.
- **Per-adapter limits from `Resources.max_memory`.** Declared raw-byte budgets do not map onto
  an address-space limit with an interpreter inside; a default every adapter shares, set per job,
  is honest until M9 schedules by declared cost.
- **A Landlock read allowlist** (Python's paths only). It would stop a compromised parser reading
  the user's other files, but breaks lazy imports, codecs, time zone data and `ld.so` lookups in
  ways that look like adapter crashes. Revisit when a native decoder lands.
- **Disabling `socket` in Python** (monkeypatching). Native code calls the kernel directly.
- **libseccomp bindings.** A new native dependency for a filter of some sixty instructions,
  which a test runs through a BPF interpreter for both architectures.
- **Retrying limit hits.** A ten-minute hang would cost twenty.
- **Falling back to in-process when the sandbox is unavailable.** Silent, and exactly when the
  protection is missing; the job says so instead.

## Consequences

- An adapter cannot keep state between calls, write a temporary file, start a helper process
  or open a socket; ADR 0008 already forbade all four, and now they fail. A call that needs
  scratch space, an archive adapter spooling nested members through ADR 0029's
  `scratch_space` for one, must be given that one directory explicitly (a Landlock write rule
  and a file-size budget for it) when it is wired in, never by lifting the sandbox.
- A `ShortReadError` raised inside a call is, for now, a raise like any other in either
  isolation; when the job treats it as the source's fault (ADR 0028's and ADR 0029's
  follow-up), `Raised` carries it as it carries `SourceChangedError`.
- The sandbox is Linux-only. Elsewhere a job must ask for `in_process`; `JobError` says so.
- Each call costs a fork and a JSON round trip; a job forks once per (source, adapter) to probe.
  MVL-8's engine and M9's scheduler can batch probes per source if that shows in profiles.
- Residual risks, recorded in `security.md`: a compromised parser can read files the user can
  read and put them in its own output (the contract checks citations, not every text); while
  it decodes one reply the parent holds it three times (buffer, payload, the parser's text) plus
  the objects it decodes to, which the value cap bounds, not the byte cap: about 550 MiB at the
  defaults for the worst shape measured (one object of 4 Mi distinct keys).
- A committed chunk's id (ADR 0024 §4, ADR 0031) covers the adapter, its version, config and the
  source, not the isolation or the Landlock ABI it ran under, so a workspace shared between a
  degraded and a sound run reuses chunk outputs across them — as it already does between
  `in_process` and `subprocess`. Degraded is a rare, opt-in, below-floor mode; run it in its own
  workspace if a later sound run must not inherit its chunks. Folding isolation into chunk
  identity is a cache question for ADR 0031, not this ADR.
- The sandbox is unavailable, not silently weaker, below Landlock ABI 3: a host there fails the
  job unless `allow_degraded_sandbox` is set, and a degraded run is a distinct lineage that
  names what it could not guarantee. Ubuntu 24.04 (kernel 6.8, ABI 4) is above the floor;
  Debian 12 (kernel 6.1, ABI 2) and RHEL 9 are below it (truncation arrived at ABI 3, kernel
  6.2), so they need a 6.2+ kernel — a backports kernel on Debian — or degraded mode by choice.
- MVL-15 runs `inspect` through the runner with an `InspectResult` codec; MVL-8's engine runs
  probes through it when it replaces the job's selection; MVL-50 builds the escape and
  exhaustion suite on the `hostile` fixture adapter.
- Revisit if fork or JSON overhead dominates ingest time, when a native decoder lands (read
  allowlist, tighter defaults), if Neptune must sandbox on macOS, or if adapters need per-adapter
  limits.
