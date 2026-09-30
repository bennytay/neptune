# Adapter contract

Status: **approved 2026-09-30** (ADR 0008). Implemented in MVL-7. Supersedes the seven-method list in the
Linear design contract (§8).

## Shape

```python
class Adapter(Protocol):
    descriptor: AdapterDescriptor
    # id, semver, formats/magic, output record kinds, config schema,
    # resource declaration (max memory, streaming), security notes. Static.

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult: ...
    # confidence in [0, 1] + structured reasons + detected version. Cheap; reads only `head`.

    def inspect(self, source: Source) -> InspectResult: ...
    # cheap summary without full parse: streams/topics, time extents, counts, schema ids, attachments.

    def plan(self, source: Source, config: Config) -> list[Chunk]: ...
    # deterministic chunk ids and cost estimates. Same inputs ⇒ same list, same order.

    def ingest(self, source: Source, chunk: Chunk, config: Config) -> ChunkOutput: ...
    # pure per chunk: canonical records + findings. No cross-chunk state.
```

## Laws

1. **Purity.** `ingest` depends only on `(source bytes, chunk, config, adapter version)`. No wall-clock,
   randomness, environment, or network. Violations are bugs.
2. **Determinism of `plan`.** Chunk ids are stable across runs; this is what makes resume and caching work.
3. **Findings, not exceptions.** Recoverable problems are `IngestFinding`s in `ChunkOutput`, built with
   `identity.findings.ingest_finding` and a documented `<adapter id>.<name>` code (ADR 0017 §9). An uncaught
   exception is treated by the runtime as a crash: the chunk is quarantined with a finding; the job continues.
4. **Leaf packages.** Adapters import `model/` and `identity/`; never each other, never `runtime/`.
5. **Locators are exact.** Every emitted record carries an `EvidenceRef` that resolves to the bytes it came from.
   Nested evidence is a locator path from the outermost source inward (ADR 0016); build the transform with
   `identity.provenance.transform_record` and tier-2 ids with `evidence_record_id`.
6. **Declared, not assumed.** Units, frames, clocks are emitted as the source declares them; unknown stays
   `Unknown`.
7. **Cheap before expensive.** `probe` reads a bounded head; `inspect` must not decode payloads.

## Blank, default and sentinel fields (ADR 0004 §5, ADR 0011)

| Source shows | Emit | Never |
|---|---|---|
| missing key, empty or whitespace-only cell | `Unknown` | a default, `""`, `0`, "none" |
| a token the source or its format spec defines as "none" | `KnownAbsent(provenance=<that definition>)` | `KnownAbsent` without a citation |
| a spec-defined sentinel (e.g. ROS covariance `[0] == -1`) | the state the spec gives it, e.g. `NotCovered` | the sentinel as a `Known` number |
| any other value, however implausible | `Known(value)` | "fixing" it; plausibility is `validate/` |
| two conflicting readings | `Ambiguous` with each candidate cited | picking one |
| a value that fails to parse | an `IngestFinding` | a guessed state |

For text fields use `from_text(raw, parse, absent_tokens={token: definition})`. Token matching is exact.
For unit text use `unit_from_text(raw, provenance=…)` (ADR 0013); never a private alias table. A unit stated
only by the format spec is `Known` citing the spec: the bytes that establish the format (magic, header, root
element) plus your transform, or the definition itself when the source carries it (ADR 0017 §6). Community
convention (REP-103) is not a declaration.

Every record you emit carries one record-level `Provenance`, and its id is
`evidence_record_id(kind, provenance.evidence, transform)`. Two records of one kind from one piece of evidence
need finer locators (an adapter step if necessary), never a counter.

## Streams and series (ADR 0018)

For formats with timestamped samples (logs, bags, flight logs, telemetry tables, video):

- One `Stream` per channel declaration, citing it, with `run` set to the `Run` your source declares.
  A topic split across files is one stream per file.
- `clocks` lists every clock a sample carries, first the one the source orders or indexes by. Never
  drop a clock or pick a "real" one; each is its own `TimestampDomain`.
- One row per sample, in source order: `seq`, one `time/<i>` per clock, `value/<name>` columns, and
  `locator/<i>/<field>` columns that fill the `SeriesProvenance` template on the stream. Your descriptor
  documents the value column names and the templates.
- A column that can be blank or hold a sentinel your format's spec defines is wrapped: add
  `state/<column>` and leave the value null where the state is not `known`. `KnownAbsent` and `Ambiguous`
  do not fit one cell: write `unknown` plus a finding.
- A payload you do not decode still gets its rows (times and locators) plus a finding.
- Tests check every row with `Stream.check_row` and resolve `Stream.row_provenance` back to the bytes.

## What the runtime owns (and adapters must not reimplement)

| Concern | Runtime mechanism |
|---|---|
| resume | skip chunks whose id is already committed in the store |
| cache | (source id, adapter id, adapter version, config hash, chunk id) |
| validation across sources | `validate/` engine over the store |
| sandboxing | subprocess with limits; the contract is picklable/serialisable so this needs no adapter change |
| explanation | assembles `ProbeResult`, `plan` output and `descriptor` into the receipt |
| scheduling / backpressure | bounded worker queues (M9) |

## Trade-off accepted

An adapter cannot resume *inside* a chunk. Formats with one giant natural chunk redo it after a crash.
Mitigation: plan finer chunks where the format allows (MCAP chunks, PDF pages, row ranges). The alternative —
fifteen bespoke checkpoint formats — was judged worse.

## Adding an adapter

1. New subpackage `adapters/<format>/` with `descriptor`, the four methods, and `probe` magic in the registry.
2. Fixtures: at least one valid file, one truncated, one corrupted, one empty, one renamed/extensionless.
3. Tests: determinism (ingest twice, byte-identical), partial corruption (findings, not failure), locator
   round-trip (every EvidenceRef resolves).
4. No changes to `runtime/` or `model/`. If you need one, stop and write an ADR.
