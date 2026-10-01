# 0032 — The probe's container listing and the archive inspector stay two passes

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-57 (M2 gate)
- Settles: the follow-up ADR 0029's consequences left open ("whether the two fuse into one pass")

## Context

Neptune reads archives twice, with two modules written for two issues:

- `neptune.discovery.containers` (MVL-8, ADR 0027) lists a container's members for the probe
  engine, which runs for every source at stage 3. It has its own bounded parsers for zip, tar,
  gzip, bzip2 and xz, decodes at most `ProbePolicy.scan_bytes` (1 MiB) per compressed stream and
  a head per member, and probes each member's head so selection and the receipt can say what the
  container holds. A limit ends the listing with a finding; nothing raises.
- `neptune.discovery.archive` (MVL-75, ADR 0029 §2) is the hardening inspector: it walks every
  member with `zipfile` and `tarfile` behind guards, inflates each one to count real bytes against
  `ArchiveLimits` (8 GiB per member, 64 GiB in all, 100:1, depth 3), and spools nested archives
  through scratch space. It is the policy for whoever extracts or ingests an archive.

The gate asked whether they become one pass. Measured on this host (an M2 stress scenario,
`test_the_probe_lists_within_its_budget_while_the_inspector_reads_everything`, and the script
`tests/fixtures/runtime/stress_archive_passes.py`, which generates the archives and whose numbers
`docs/reviews/m2-stress-test.md` records): on a gzip-compressed tar of incompressible members,
the probe reads about 1.2 MB and takes under 1 ms whether the archive inflates to 64 MiB or
256 MiB; the inspector inflates all of it, at about 1.8 GB/s, 0.04 s and 0.15 s. On a 100 GB
archive the probe still costs milliseconds and the inspector about a minute.

## Decision

1. **Two passes, two owners, no fusion.** The probe's listing stays at stage 3, for every
   container, inside the probe engine's sandboxed call (ADR 0033 §1). The inspector stays the
   ingest-time policy: it runs where an archive is read whole, which is an archive adapter's
   `plan` or `ingest` (M4+), inside that call's sandbox, spooling through the call's scratch
   directory (ADR 0033 §2). The full pass is fused with the read that needs it, the archive
   adapter's own, as ADR 0029 already anticipated, never with probing.
2. **No shared parser now.** The two read the same formats for different questions (what does it
   hold, within a budget; is all of it within limits). Sharing `zipfile`/`tarfile` would put
   unbounded library parsing into stage 3; sharing the probe's parsers would give the inspector
   readers that stop at a budget it must not have. Each keeps its tests and fixtures.
3. **The job does not call the inspector** while no adapter reads archives: an archive nobody
   claims is `neptune.probe.unsupported` with the listing's summary, and inflating it whole
   would cost minutes for a finding that already says it was not read.

## Alternatives considered

- **One pass at probe time** (run the inspector when the head sniffs as an archive). Probing
  would cost a full inflation of every archive in a corpus, including ones no adapter reads,
  breaking "cheap inspect before expensive ingest"; and a bomb would be inflated (within limits)
  just to be told it is unsupported.
- **One pass at ingest time** (drop the probe's listing). Selection could no longer say what a
  container holds, and `explain` (MVL-15) would lose the member report ADR 0027 promises.
- **The inspector built on the probe's parsers.** The probe's readers are budgeted views; a
  budget hit is a finding there, but the inspector must reach the end or say why with a limit
  finding of its own. Two semantics in one reader is the bug surface the split avoids.

## Consequences

- An archive adapter (M4+) calls `inspect_archive` from its sandboxed `plan`, with
  `contract.scratch_directory()` as its scratch, and turns the report's findings into its own
  plan findings; nested archives larger than 1 MiB spool there (tested in the sandbox).
- Archives are inflated once to ingest, not twice: probing never inflates past its budget.
- Two modules keep their own format knowledge (signatures, member kinds). A format added to one
  (zstd, 7z) is decided for the other in the same PR.
- Revisit when an archive adapter lands and its profile shows the listing and the inspection
  both on its hot path, or if a format's members can only be listed by inflating it (then the
  listing's budget decides, and the finding says so).
