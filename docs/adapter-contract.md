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
3. **Findings, not exceptions.** Recoverable problems are `IngestFinding`s in `ChunkOutput`. An uncaught
   exception is treated by the runtime as a crash: the chunk is quarantined with a finding; the job continues.
4. **Leaf packages.** Adapters import `model/` and `identity/`; never each other, never `runtime/`.
5. **Locators are exact.** Every emitted record carries an `EvidenceRef` that resolves to the bytes it came from.
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
