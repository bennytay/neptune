# 0008 — Adapter ABI surface and runtime/adapter responsibility split

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-55

## Context

The design contract (§8) required every adapter to implement seven methods: `probe`, `inspect`, `plan`,
`ingest`, `validate`, `resume` and `explain`. Neptune will have dozens of adapters, many written in parallel by
different agents (M3–M6). Every method on the contract is multiplied by the adapter count, in code, tests and
opportunities for inconsistency:

- `resume` would mean fifteen bespoke checkpoint formats.
- `validate` would split cross-source checks across adapters that must not import each other.
- `explain` would drift into free-text logging.

The contract must also allow the runtime to run adapters in resource-limited subprocesses (MVL-10) and against
object stores (MVL-45), without adapter changes.

The four-method reduction was approved on 2026-09-30 (audit §4A). This ADR records it.

## Decision

1. **The adapter surface is a static descriptor plus four methods:**

   ```
   descriptor: AdapterDescriptor   id, semver, formats/magic, output record kinds, config schema,
                                   resource declaration, output-affecting dependencies, security notes
   probe(head: bytes, hints) -> ProbeResult          confidence in [0,1] + structured reasons + detected version
   inspect(source) -> InspectResult                   cheap summary; must not decode payloads
   plan(source, config) -> list[Chunk]                deterministic ids and order, cost estimates
   ingest(source, chunk, config) -> ChunkOutput       canonical records + series batches + findings
   ```

   Adapters are structural implementations of a `Protocol`, not subclasses of a framework base class. Their
   exact signatures and result types are MVL-7's.
2. **Adapters never touch the filesystem or network directly.**
   - They read through the `Source` interface: read-only, random-access and bounded (MVL-2).
   - They never write. They return outputs, and the runtime writes the store.
   - This is what makes sandboxing, object stores and caching runtime features rather than adapter features.
3. **The chunk is the unit of work, caching, resume, isolation and failure.**
   - Everything `ingest` needs beyond the source bytes travels inside the `Chunk`, computed deterministically
     by `plan`. For example, the MCAP schema/channel table needed by messages in later chunks is carried this
     way.
   - A chunk id is derived from the chunk's own content, so a change in planned context changes the id.
   - Chunks and outputs are serialisable, so a chunk can be shipped to a subprocess.
4. **Laws** (enforced by tests in each adapter; see `adapter-contract.md`):
   - **Purity.** `ingest` depends only on source bytes, chunk, config and adapter version. It uses no wall-clock,
     randomness, environment or network, and it keeps no state across chunks.
   - **Determinism.** `plan` returns the same chunks in the same order for the same inputs.
   - **Findings, not exceptions.** Recoverable problems are `IngestFinding`s in the output. An uncaught exception
     is a crash: the runtime quarantines the chunk with a finding, and the job continues.
   - **Cheap before expensive.** `probe` reads only a bounded head, and `inspect` never decodes payloads.
   - **Leaves.** An adapter imports `model/` and `identity/` only. It never imports another adapter or
     `runtime/`.
   - **Exact locators and declared semantics.** Locators are exact (ADR 0006). Units, clocks and frames are
     emitted as declared (ADRs 0004, 0005, 0007).
5. **The runtime owns what the design contract assigned to adapters:**
   - **Resume**: skip chunks whose id is already committed (MVL-6).
   - **Cache**: keyed by `(source content id, adapter id, adapter version, config hash, chunk id)` (MVL-9).
   - **Validate**: adapter-local problems are findings from `ingest`. Cross-source checks run in `validate/`
     over the store (MVL-41).
   - **Explain**: the receipt assembles `ProbeResult`s, `plan` output and descriptors (MVL-5, MVL-15).
6. **Versioning.** An adapter bumps its semver whenever any output byte may change (ADR 0003). The descriptor
   lists output-affecting dependencies, whose versions are recorded on `TransformRecord`s (ADR 0006).

This supersedes the seven-method list in the Linear design contract §8 and in MVL-7's original description.

## Alternatives considered

- **The seven-method contract as written.** Each adapter would implement checkpointing, cross-output validation
  and explanation itself. That means N checkpoint formats, validation that cannot see across sources, and
  unstructured explanations. It loses to runtime-owned mechanisms driven by deterministic chunk ids.
- **A single `parse(source) -> Iterator[Record]`.** Minimal. It gives no cheap `inspect` for dry-run and
  receipts, no plan to schedule, cache or resume from, and no unit of isolation. A crash midway loses the whole
  source.
- **Adapter-managed intra-chunk checkpoints**, as an optional fifth method. It would recover partial chunks
  after a crash, but an optional method becomes a de-facto requirement for large formats and brings the bespoke
  checkpoint problem back. Finer chunks recover the same benefit.
- **Abstract base class with template methods.** It adds hidden inherited behaviour and makes adapters harder to
  sandbox and pickle. A `Protocol` with plain data results keeps adapters as leaf functions over data.
- **Adapters write to the store directly.** Avoids a copy. It couples adapters to the store layout, breaks
  sandboxing, and lets a crashing adapter leave partial writes.

## Consequences

- Adding an adapter touches no runtime or model code. If it would, that is a signal to write an ADR.
- There is no resume *inside* a chunk. Formats with one giant natural chunk redo it after a crash. Adapters
  mitigate this by planning finer chunks where the format allows: MCAP chunks, PDF pages, row ranges, ULog
  segments.
- `plan` quality becomes a performance concern, because chunk granularity trades scheduling overhead against
  redo cost. Cost estimates in `plan` exist for the scheduler (M9).
- Chunk and output types must stay serialisable, which rules out handles, open files and callbacks in them.
- Revisit if a supported format cannot be chunked finer than a size that makes redo-on-crash unacceptable, or if
  the subprocess boundary (MVL-10) shows serialisation overhead dominating.
