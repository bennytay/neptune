# 0024 — The adapter ABI's exact types, adapter selection, and the reference adapter

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-7
- Extends: ADR 0008 (which left the exact signatures and result types to MVL-7)

## Context

ADR 0008 fixed the adapter surface: a static descriptor and `probe`, `inspect`, `plan`, `ingest`,
with the runtime owning resume, caching, validation, sandboxing and explanation. It left the exact
types to MVL-7. Every adapter of M4–M6 is written against them, several in parallel, so a type that
changes after the M2 gate changes every adapter.

MVL-7 also asks for a registry with priority and conflict resolution, versioning and capability
metadata, and a reference adapter. Its acceptance is that a new format needs no change to core
code, and that adapter selection and parser versions appear in the receipt. ADR 0022 makes the
receipt a function of the package's records alone.

## Decision

1. **Signatures** (`neptune.adapters.contract`, `ABI_VERSION = 1`):

   ```
   descriptor: AdapterDescriptor
   probe(head: bytes, hints: ProbeHints) -> ProbeResult
   inspect(source: SourceReader, config: AdapterConfig) -> InspectResult
   plan(source: SourceReader, config: AdapterConfig) -> Plan
   ingest(source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput
   ```

   - `head` is the first `min(size, PROBE_HEAD_SIZE)` bytes (64 KiB); `ProbeHints` is the
     location's last name and the size, advisory only.
   - `inspect` takes the config because its findings name a transform. Its findings are for dry
     runs; an ingest finds them again, so they never enter a package from `inspect`.
   - `plan` returns `Plan(chunks, findings)`: at least one chunk, no repeats. Planning can find
     problems (an oversized block, an unreadable index) that no chunk will see.
   - `SourceReader` is `content_id`, `size` and `read(offset, length)`: one artifact's bytes.
   - An adapter is any object with these members (a `Protocol`), not a subclass.
2. **The descriptor is the adapter's documentation, checked.** It holds the id (the transform's
   `adapter_id`), a SemVer version, the ABI version, formats (names, media types, extensions,
   magic), the evidence record kinds it emits, config options, output-affecting libraries, finding
   codes (`<id>.<name>`), adapter locator steps (`<id>:<name>`), conventions (block rules, column
   mappings), resources (memory, streaming) and security notes. The checks refuse an undeclared
   record kind, finding code or locator step.
3. **Config is typed scalar options with defaults.** `configure(descriptor, values)` refuses
   unknown names and values of another type, fills in defaults, and builds the `TransformRecord`.
   The adapter receives `AdapterConfig(values, transform)`. Omitting an option and stating its
   default are the same config and the same transform. Planning granularity is never an option,
   because chunking must not change the output: it is a constructor argument of the adapter.
4. **Chunks.** `Chunk(id, source, transform, context, cost)`. The id is
   `chunk:sha256:<hex>` over the transform id, the source's content id and the context, so equal
   ids mean equal output and the id alone keys resume and cache (it covers ADR 0008 §5's key).
   `cost` estimates bytes read, for scheduling, and is not in the id. Chunks round-trip through
   JSON for checkpoints.
5. **Output.** `ChunkOutput(records, series, findings)`: complete evidence records, series
   batches, findings. A `SeriesBatch` is one stream's rows from one chunk, column by column: each
   `SeriesColumn` has a name in ADR 0018's namespaces, a `ColumnType` (bool, signed and unsigned
   8–64-bit integers, float32, float64, string, binary), a `repeated` flag for arrays, and one cell
   per row. Neptune's own columns have fixed types (`seq` and `time/<i>` int64, a locator field
   int64, float64 or string, a state string). The store maps the types to Parquet (MVL-16).
   The batch types live in `neptune.model.series`, beside the column contract they type, so the
   store writes them without importing adapters. They are not records: the schema is unchanged.
   The adapter numbers `seq`, which ADR 0018 §7 left to MVL-7: `plan` gives each chunk the `seq`
   its rows start from, so rows are numbered in source order however the source is chunked.
6. **Laws added to ADR 0008's**, checked by `neptune.adapters.check` on every harness run:
   - every record's id derives from its record-level evidence and the config's transform, and it
     reads back from its JSON as itself;
   - every provenance names that transform, and every citation is of the one source given;
   - an adapter's finding cites bytes, never a location;
   - no record or finding is emitted by two chunks;
   - every series batch names a stream of the same source's output and keeps its row contract;
   - **the output cites its source at least once**, so a source an adapter read always shows the
     adapter in the receipt.
7. **Registry and selection** (`neptune.adapters.registry`). A registry is explicit, built per
   job. It refuses two adapters with one id and an adapter of another ABI version. Selection is a
   pure rule over every adapter's probe:
   - confidence 0 is no claim; candidates rank by confidence, then adapter id;
   - no candidate is `unsupported`; a tie at the top is `ambiguous`; otherwise `selected`;
   - nothing breaks a tie silently: the probe engine reports it (MVL-8), and a manifest names the
     adapter (MVL-14).
   Confidence is calibrated against named bands: `VERIFIED` 1.0, `SIGNATURE` 0.9, `STRUCTURE`
   0.7, `GENERIC` 0.4, `NAME_ONLY` 0.1. A generic reader therefore never outranks a specific one.
8. **Selection reaches the receipt through the records**, as ADR 0022 requires: the receipt lists
   each source with the transforms whose records cite it, and every transform's adapter id,
   version, config hash and libraries. Law 6 guarantees the selected adapter is listed. A source
   with no adapter is "not read" in the receipt; the finding that says why is MVL-8's.
9. **Built-ins are listed in one place.** `neptune.adapters.builtin` lists the shipped adapters.
   Adding a format is a subpackage and one line there; nothing else names a format. A job can
   build its own registry from any adapters.
10. **The reference adapter is plain UTF-8 text** (`neptune.adapters.text`): a `DocumentRecord`
    per file and a `DocumentBlock` per paragraph (or per line), each citing its exact `Span`. It
    is real, small, needs no model change, and shows every pattern: streaming plan, chunk context,
    findings instead of exceptions, a resource limit, and output that never depends on chunking.

## Alternatives considered

- **Entry-point plugin discovery** (`neptune.adapters` entry points). The standard Python plugin
  mechanism, but it makes a job's adapter set depend on what happens to be installed. No third-party
  adapter exists yet, and an explicit list is the same one line per format. Add it when one does.
- **A selection record kind**, holding every probe's confidence and reasons. It would put the "why"
  of each selection into the package, but it is a model addition (ADR 0023) for information a dry
  run recomputes deterministically. Findings carry the why exactly where it matters: ambiguous and
  unsupported sources.
- **Pick the first adapter on a tie, or by a declared priority.** Deterministic, but a silent guess
  between two equally supported readings, which non-negotiable 4 forbids.
- **Series rows as dictionaries, or as Arrow arrays.** Rows lose column types (float32 against
  float64, unsigned integers), which "exactly as the source encodes them" needs. Arrow would put a
  native dependency into every adapter and the contract; columns of plain values convert to Arrow
  in the store, and a faster path stays open at M9 (ADR 0001 §6).
- **Planning granularity as a config option.** It would put a scheduling knob into the transform,
  so changing it would re-lineage every record although no output changed.
- **A toy format as the reference adapter.** No preemption of later issues, but nothing real to copy.
  The test-only `tally` adapter (`tests/fixtures/adapters/`) covers the series path instead, and
  proves that a format outside `src/` plugs in.
- **CSV or a trajectory format as the reference.** Both need decisions owned elsewhere: CSV typing
  (MVL-30) or a decimal-seconds rounding rule (ADR 0012's consequences), and either would set a
  precedent in a reference.

## Consequences

- Adapters of M4–M6 have exact types to write against, and one harness (`ingest_source`) that runs
  every law in their tests.
- The runtime (MVL-6) calls the same four methods and the same checks, adding persistence,
  isolation and resume. The store (MVL-16) writes `SeriesBatch`es to Parquet.
- Plain text from MVL-28's scope is done; MVL-28 keeps PDF and Markdown.
- A change to any type here is a new `ABI_VERSION`, and the registry refuses adapters of the old
  one, so it can only happen deliberately.
- Revisit if a format needs inputs beyond one source's bytes (a bag split across files), if series
  conversion dominates ingest time, or if third-party adapters need discovery.
