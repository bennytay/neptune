# 0069 — Salvage by chunk: a lost chunk, declared extents, and an exact account of what was lost

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-42
- Amends: ADR 0028 §3 (quarantine by source, for chunk failures) and §1 (what `assemble` admits);
  ADR 0024 (an optional descriptor field, `extent`, at ABI 1)

## Context

ADR 0028 §3 quarantined a whole source whenever one of its chunks failed for good, and listed
"quarantine by chunk" as an alternative to revisit "if a supported format needs part of a crashed
source in the package". Field robotics data is exactly that case. A two-hour MCAP from a mobile
base, a ROS 1 bag from an arm cell or a flight controller's ULog is planned as tens to thousands
of chunks; a decoder bug, a crash on one hostile record, or a sandbox limit on one chunk threw
away every other chunk's evidence, and the receipt said only that the source was missing.

The two reasons ADR 0028 gave against keeping part of a source are both checkable rather than
fatal. Rows of a stream declared in the lost chunk would be orphaned: the cross-chunk laws
(ADR 0028 §5) already detect that (`stream_undeclared`) and can be run over just the chunks that
committed. And a package could not say which part of a source it holds: it can, if it knows which
bytes each lost chunk decoded.

MVL-42 asks for per-source and per-chunk failure isolation, salvage of readable MCAP/ROS sections,
a retry policy, preservation of unsupported fields, explicit loss accounting and a quarantine
state, with the acceptance that one malformed chunk or PDF does not destroy a multi-source run and
the receipt states exactly what was lost.

## Decision

1. **A chunk that fails for good is lost, not its source.** A chunk whose `ingest` raises after
   every attempt, crashes, is stopped at a sandbox limit, or whose output the per-chunk checks
   refuse (now, or when a kept chunk is judged again under a new runtime version) gets its
   finding (`chunk_failed`, `adapter_crashed`, `limit_exceeded`) and is recorded as lost. The
   source's other chunks still run and commit. Faults that are about the source, not one chunk,
   still quarantine the whole source as before: a plan that fails, crashes or hits a limit; a
   source that changed, cannot be opened, or read short (ADR 0033 §3).
2. **`assemble` salvages or refuses, by law.** A source with lost chunks is judged on the chunks
   it kept, by the same cross-chunk laws and the same cached verdict (ADR 0031 §4) keyed by the
   kept chunk ids:
   - some chunks committed and they pass every law on their own: the source is **admitted
     without the lost chunks**. Staging (`store.assemble.stage(omit=…)`) leaves the lost chunks'
     records, findings and runs out, and each stream is merged from the runs that remain (its
     series derivative key names exactly those chunks). The job emits `source_salvaged`, not
     `source_admitted`, and records one `neptune.runtime.source_partial` finding;
   - nothing committed, or what committed breaks a law without the rest (rows of a stream
     declared in a lost chunk, an output that says nothing, or `reference_lost`): the source is
     **quarantined** with `neptune.runtime.salvage_refused`, naming the laws and the problems,
     or "every chunk was lost". `source_quarantined` then lists the lost chunks' codes beside it.

   `reference_lost`: a kept record or finding names a record the kept chunks do not hold, such
   as a text block whose document, a configuration value whose snapshot, or a finding whose
   `records` was in a lost chunk. References are read from the model, not a hand list, by one
   walker both checks call (`model.references.named`): every field typed as a `RecordId`, alone,
   in a tuple, in a `Knowledge`, or nested in a value such as a `Timestamp`'s `domain_id` or a
   `FrameRef`'s `frame_graph_id`, and every finding's `records`; not a record's own `id`, its
   `provenance`, or a finding's `transform`. Fields typed as data (cells, configuration values)
   are never read, so text that looks like an id names nothing. Fields marked external
   (`ids.EXTERNAL` field metadata: an assertion's `scope`, a revision's or absence's
   `supersedes`, a transform's `upstream`) may name another package's records and are checked by
   neither. One problem per target. It applies to salvaged sources only: a whole source whose
   references dangle is an adapter bug that validation reports; validation's
   `dangling_reference` (rule version 3) checks every reference the same walker reads, keeping a
   table only for the kind a reference must name where the model states one. A missing frame graph is
   reported by both `frame_unresolved` and `dangling_reference`, as intended. A source that lost
   nothing is judged as before (`output_invalid` if it breaks a law).

   **Refuse, never drop.** Such a source is refused, not admitted without the dangling records:
   dropping them would edit committed chunks' output at assembly, a value without its snapshot
   (or a finding without what it qualifies) is not evidence a reader can use, and the package
   could no longer say which part of the source it holds by chunk. Adapters that keep what others
   name (declarations, documents, snapshots, tables) in a chunk of their own keep salvage useful.
3. **Exact byte extents are declared, not guessed.** `AdapterDescriptor.extent` (optional,
   `ChunkExtent(start="start", end="end")`) names the chunk-context keys holding the `[start,
   end)` source bytes a chunk decodes. A chunk with neither key (a declarations chunk, a whole
   document) names none. `check_plan` refuses one key without the other, a non-integer, and a
   range outside the source. The extent is read from the plan's context, which the chunk id
   already hashes, so it changes no id and a reused plan answers as a fresh one does. MCAP,
   ROS 1 bags, flight logs (ULog and DataFlash) and text declare it; adapters whose chunks are
   not byte windows (a PDF's pages, a workbook's sheets) do not, and their lost chunks cite the
   whole source. It is an additive optional field: `ABI_VERSION` stays 1 and every adapter that
   does not set it is unchanged.
4. **The receipt states exactly what was lost.** A lost chunk's finding cites its extent as its
   subject and in `details.extent` (`{offset, length}`), else the whole source. The salvaged
   source's one `source_partial` finding (failed, error; subject the whole source) holds: the
   adapter and version; `chunks` planned and `committed`; `lost`, every lost chunk sorted by id
   with its finding's code and its extent if declared; `codes`, a count per code; `not_covered`,
   the lost extents merged into the fewest sorted disjoint ranges, each also a `related`
   `EvidenceRef`; `not_covered_bytes`; and `undeclared`, the lost chunks with no extent. Its
   message spells out the counts and the first four ranges (then "and N more"), so it stays
   within the message bound however many chunks were lost. Every list is sorted, so the finding
   is the same every run. The receipt's findings list carries both, so a reader learns from the
   receipt alone which bytes the package holds no evidence for. `not_covered` is an upper bound:
   bytes another kept chunk also decoded are still listed, because evidence is per chunk and
   the runtime does not reason about overlap.
5. **Retry policy is unchanged and stated here.** `ingest` is tried `attempts` times (default
   2); a raise or a crash is retried, a `ContractError`, a wrong result type, a short read, a
   changed source and a sandbox limit are not (the same bytes under the same limit fail again).
   `plan` is never retried within a job. Lost chunks are never committed, so the next job over
   the same workspace retries exactly them; after a transient fault or an adapter fix it writes
   the package a fresh workspace writes (ADR 0028 §2). No quarantine or loss is ever cached.
   The same holds one level up for a connector's source (ADR 0067): a fetch that stops part way
   (a full disk, a connector that raises) still fails the job, but the URI's ledger is saved
   first with every object already fetched and hashed (whole observations only; nothing is
   marked absent from an unfinished pass), so the retry recognises those by token and fetches
   only the rest instead of failing identically every time. A connector that broke the Source
   protocol saves nothing (what it listed, or began to call gone, is not trusted). The retry
   sees an object fetched by the failed job as already observed, so its cache report does not
   name the bytes that revision replaced (ADR 0031 §3); the package is the one a fresh workspace
   writes.
6. **Quarantine state** is the set of runtime finding codes a source carries (`_Source.quarantined`
   plus its lost chunks' codes in the `source_quarantined` event), the `quarantined` status of a
   dry run's explanation (ADR 0044), and in the package the runtime's findings citing it with no
   adapter transform reading it. A salvaged source is not quarantined: it is read by its adapter
   and by the runtime, which says what is missing.
7. **Unsupported fields are preserved as findings and bytes, not guessed.** Nothing new is
   needed: adapters report what they cannot decode as `unsupported` or `corrupt` findings citing
   the exact bytes (ADR 0017 §9; the MCAP adapter's `crc_mismatch`, `chunk_truncated`,
   `decompression_failed`, the ROS 1 adapter's `chunk_truncated` and `corrupt_record`, the flight
   logs' cut messages), which is the adapter-level salvage of readable sections; the raw source is
   referenced and never rewritten (non-negotiable 1). Runtime salvage is the second line, for
   what an adapter could not survive. A salvaged source keeps every finding its committed chunks made, so no
   unsupported-field report is lost with a neighbouring chunk.
8. **`RUNTIME_VERSION` is 0.3.0.** What the runtime admits changed, so cached admission verdicts
   (ADR 0031 §4) are judged again and packages that hold a runtime finding name the new version.

## Alternatives considered

- **Keep quarantine by source** (ADR 0028 §3). Loses every good chunk of a long recording for
  one bad one, and the receipt cannot say how much was lost. The issue's acceptance rules it out.
- **Drop the records of streams whose declaration was lost** and admit the rest. That edits a
  committed chunk's output at assembly, which no law then checks, and the package would hold a
  subset of a chunk the receipt cannot describe. Refusing (`salvage_refused`) keeps a package's
  unit of evidence the chunk; adapters keep declarations in a chunk of their own (MCAP, ROS 1).
- **Byte extents from the evidence** (the union of the lost chunk's would-be record locators).
  The lost chunk produced no records, so there is nothing to take them from; and guessing a
  range from neighbouring chunks is a silent assumption about the format.
- **A required extent on every adapter** (an ABI 2). Pages, sheets and members are not byte
  windows; a required field would force a fake one. Optional, and counted as `undeclared` when
  absent, is honest.
- **Extents in a new receipt field** (a schema change to `IngestReceipt`). The finding already
  carries structured details and a related `EvidenceRef` per range, the receipt lists it, and the
  model stays frozen (M1 gate, ADR 0023). A typed receipt section is for a consumer that needs it.
- **Committing a lost chunk's finding as its output** (so resume skips it). Freezes a transient
  fault under a deterministic id, as ADR 0028 already argued.
- **Retrying limits with a backoff, or more attempts by default.** A limit is deterministic for
  the same bytes and limit; more attempts only cost time on a real bug. The knob exists
  (`--attempts`, ADR 0043) and is part of the runtime transform.

## Consequences

- One malformed chunk costs that chunk's bytes, stated in the receipt; the rest of the recording,
  every other source and the job survive. A package may now hold part of a source under a
  transform; `source_partial` is the only record that says which part, and validation
  (`source_incomplete`, ADR 0054) still rolls up the adapter's own damage findings.
- `store.assemble.stage` takes `omit`; the job passes the lost chunk ids of salvaged sources and
  skips them wherever it reads committed outputs for the package (run assembly, introspection,
  clock alignment, media). Series derivatives of a salvaged stream are keyed by the kept chunks.
- Adapter authors should declare `extent` when chunks are byte windows and keep stream
  declarations in a chunk of their own with an empty typed batch per stream; the adapter contract
  (`docs/adapter-contract.md`) says so.
- Tests: decoder bugs injected per chunk into the real MCAP, ROS 1, ULog, DataFlash and PDF
  adapters; a lost declarations chunk refusing salvage; every chunk lost; resume retrying only
  the lost chunk and matching a fresh package; generated hostile mutants (truncations, garbage
  tails, zeroed regions) of six formats through the sandbox, byte-identical across runs.
- Revisit if a consumer needs the not-covered ranges as a typed receipt section, if an adapter
  needs a lost chunk's declarations re-derived from another chunk, or if per-chunk overlap makes
  `not_covered` too loose to be useful.
