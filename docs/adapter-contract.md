# Adapter contract

Status: **implemented** (MVL-7). Shape approved 2026-09-30 (ADR 0008); exact types in ADR 0024
(`neptune.adapters.contract`, `ABI_VERSION = 1`). Supersedes the seven-method list in the Linear
design contract (§8). The reference adapter to copy is `neptune.adapters.text`.

## Shape

```python
class Adapter(Protocol):
    descriptor: AdapterDescriptor
    # id, SemVer version, ABI version, formats (media types, extensions, magic), evidence record
    # kinds, config options, libraries, finding codes, locator steps, conventions, resources,
    # security notes. Static, and checked: undeclared kinds, codes and steps are refused.

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult: ...
    # confidence in [0, 1] + structured reasons + detected format version. Reads only `head`:
    # the first min(size, PROBE_HEAD_SIZE = 64 KiB) bytes. Hints (name, size) are advisory.

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult: ...
    # cheap summary without decoding payloads: a JSON object the adapter documents, + findings.

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan: ...
    # >= 1 chunk with deterministic ids, in order, + findings planning made.

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput: ...
    # pure per chunk: evidence records + series batches + findings. No cross-chunk state.
```

`SourceReader` is one artifact's bytes: `content_id`, `size`, `read(offset, length)`
(`read_pieces` streams a range). `discovery.reader.BytesReader` serves bytes from memory. A reader
that serves no bytes inside the size it declares makes `read_pieces` raise `ShortReadError`; let it
propagate. The runtime re-reads the source (ADR 0029 §3, ADR 0033 §3): if it no longer matches its
artifact, the short read is the source's, `neptune.discovery.short_read` for the unserved range
with `verify_artifact`'s account, never retried, the source quarantined and the job going on. If
the source is intact, the short read came from your code (a window over the reader that declares
the wrong size, a raise naming another reader) and the call failed like any other raise
(`plan_failed`, `chunk_failed` naming `ShortReadError`).

## Laws

1. **Purity.** `ingest` depends only on `(source bytes, chunk, config, adapter version)`. No wall-clock,
   randomness, environment, or network. Violations are bugs.
2. **Determinism of `plan`.** Chunk ids are stable across runs; this is what makes resume and caching work.
3. **Findings, not exceptions.** Recoverable problems are `IngestFinding`s in `ChunkOutput`, built with
   `identity.findings.ingest_finding` and a documented `<adapter id>.<name>` code (ADR 0017 §9). An uncaught
   exception is treated by the runtime as a crash: the chunk is quarantined with a finding; the job continues.
4. **Leaf packages.** Adapters import `model/`, `identity/` and `adapters.contract`; never each other, the
   registry or `runtime/`.
5. **Locators are exact.** Every emitted record carries an `EvidenceRef` that resolves to the bytes it came from.
   Nested evidence is a locator path from the outermost source inward (ADR 0016); build the transform with
   `identity.provenance.transform_record` and tier-2 ids with `evidence_record_id`.
6. **Declared, not assumed.** Units, frames, clocks are emitted as the source declares them; unknown stays
   `Unknown`.
7. **Cheap before expensive.** `probe` reads a bounded head; `inspect` must not decode payloads.
8. **Declared, then checked.** Every record kind, finding code (`<id>.<name>`) and adapter locator step
   (`<id>:<name>`) the adapter emits is in its descriptor. Every record and finding names the config's
   transform and cites only the source it was given; a finding cites bytes, never a location.
9. **One chunk per output.** No record or finding is emitted by two chunks. A source's output cites the
   source at least once, even when it is empty or unreadable, so the receipt always shows who read it.
10. **Sandboxed by default** (ADR 0030). Every call runs in a fresh child process: nothing an adapter
    keeps on itself survives to the next call, and opening a socket, writing a file, starting a
    process or signalling another one fails with an `OSError`. Reading files (lazy imports, codec
    and time-zone tables) works. A crash, a hang or runaway memory is a finding about the source,
    never a failed job.
11. **Scratch, only where given** (ADR 0033 §2). `contract.scratch_directory()` is an empty private
    directory a `plan` or `ingest` call may write temporary files in (a spool for a nested
    archive, a decoder that wants a file), removed when the call returns; each file is at most
    `scratch_bytes` (1 GiB by default; past it the call stops with `limit_exceeded`). It is
    `None` for `probe` and `inspect`, outside a job, with `scratch_bytes` 0, and on a host without
    Landlock. An adapter that needs scratch and has none raises `contract.ScratchUnavailableError`
    (a `ContractError`), never a finding: chunk ids do not name scratch, so a finding would be
    committed and reused by a later run that has scratch, and that package would differ from a
    fresh workspace's. The plan or chunk fails for that run only (`plan_failed`, `chunk_failed`
    with `cause` `scratch_unavailable`, never retried) and nothing of it is committed. Output never
    depends on scratch: not on whether it was given, nor on what a previous call left there.

## Config, chunks and output (ADR 0024)

- **Config.** Options are typed scalars with defaults (`ConfigOption`). `configure(descriptor, values)`
  refuses unknown names and wrong types, fills in defaults and builds the `TransformRecord`; the adapter
  gets `AdapterConfig(values, transform)` and reads settings with `text`, `integer`, `number`, `flag`.
  A setting is part of the transform, so it re-lineages records. Planning granularity is therefore a
  constructor argument, never an option: chunking must not change the output.
- **Chunks.** `make_chunk(source, config, context, cost)`. `context` is everything `ingest` needs
  besides the bytes (byte range, starting offsets, a schema table); document it in `conventions`. The
  id hashes transform, source and context; `cost` (bytes to read) is for scheduling only.
- **Output.** `ChunkOutput(records, series, findings)`. A `SeriesBatch(stream, columns)`
  (`neptune.model.series`) holds one stream's rows from one chunk as typed `SeriesColumn`s (see
  "Streams and series" below for names).

## Choosing an adapter (ADR 0024 §7)

Every registered adapter probes; the registry applies one rule. Confidence 0 is no claim. Candidates
rank by confidence, then adapter id. No candidate: `unsupported`. A tie at the top: `ambiguous`, never
broken silently (MVL-8 reports it, a manifest resolves it). Otherwise: `selected`.

Calibrate against the bands, so equal evidence gives equal confidence across adapters:

| Band | Value | Means |
|---|---|---|
| `VERIFIED` | 1.0 | structure parsed and checked beyond the signature |
| `SIGNATURE` | 0.9 | magic bytes or signature matched |
| `STRUCTURE` | 0.7 | a text format's grammar or root structure matched (JSON parses, `<robot>` root) |
| `GENERIC` | 0.4 | only a generic decoding applies (UTF-8 text) |
| `NAME_ONLY` | 0.1 | only a name, an extension or an empty file suggests it; damaged generic content |

Never decide from the name alone when the bytes can say: a renamed file must still be read.

Two adapters are generic: `text` claims any UTF-8 at `GENERIC`, and `config` claims any JSON, TOML or YAML
document with a mapping or sequence root at `STRUCTURE` (ADR 0037 §7). A format carried in one of those
(rosbag2 `metadata.yaml`, a calibration YAML, GeoJSON) must check its own structure and claim `VERIFIED`,
or it ties with or loses to them.

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

## Times, several-field values and findings (ADR 0023)

- **Civil date-times.** With a stated offset or `Z`, a time is an exact instant: count POSIX seconds from
  1970-01-01T00:00:00Z (epoch `unix`, timescale `posix`). With no zone, count the same way on the source's own
  civil clock: epoch `unix`, timescale `Unknown`. A date alone counts days (resolution 86,400 s). Never assume
  UTC or the site's zone.
- **Several fields, one value.** A value read from several fields (start plus duration, latitude plus longitude
  columns) cites the smallest part that holds them all.
- **Degrees, minutes and seconds** with a hemisphere (EXIF GPS) are read into signed degrees, exactly and then
  rounded once to a float; the declared form stays in the bytes.
- **One finding per affected range, never per sample.** A corrupt chunk is one finding naming the chunk and
  the streams it touched.
- **No new fields.** Record kinds are frozen from schema version 1. Something your format declares that no
  field holds is a new record kind naming the record it extends, added by ADR; until then it stays cited
  in the bytes. A new kind declares `since`, the version that adds it, so no other package's bytes change
  (ADR 0037 §1).

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
- The chunk that emits a `Stream` emits a `SeriesBatch` for it, empty if that chunk holds none of
  its rows: the batch types the stream's columns, so a stream with no samples still has a series.
- Give each `SeriesColumn` the `ColumnType` the source encodes (a ROS `float32` stays `float32`,
  a `uint8` stays `uint8`); arrays are `repeated` columns. `seq` and `time/<i>` are `int64`.
- Tests check every row with `Stream.check_row` and resolve `Stream.row_provenance` back to the bytes.

## Machine context (ADR 0019)

For sources that describe machines (manifests, robot descriptions, flight logs, calibration files):

- Emit a `Machine` only where the source states an identifier for it, and list every id it states in
  `identifiers`, each citing where it is stated. Document the namespaces you use (`px4.sys_uuid`).
  Never turn a model name, hostname, topic prefix or folder name into a machine.
- A robot description (URDF, SDF, MJCF) is a `HardwareConfiguration` with `machine` `NotCovered`, plus a
  `HardwareComponent` per declared part citing its element, and the `FrameGraph` / `FrameTransform`s of
  its kinematics. Another tool set or revision is another configuration; never edit one.
- Software: one `SoftwareConfiguration` per declaration of what ran, one item per software unit.
  Put each identity in its own field. Where your format could state an identity and the file does not,
  write `Unknown` citing where you looked and emit `<adapter>.software_identity_missing`.
- Calibration: one `Calibration` per calibrated subject, parameters under their declared names with
  numbers in source order, and extrinsics as `FrameTransform`s in the calibration file's own graph,
  direction `Ambiguous` unless the format says which way they map.

## Configuration (ADR 0037)

For parameter files and other configuration documents (the `config` adapter reads JSON, YAML and TOML):

- One `ConfigurationSnapshot` per document and one `ConfigurationValue` per node, in document order,
  each citing a `JsonPointer` to it and its value citing its exact span. A repeated key keeps every
  entry, addressed by position; a finding says it repeats.
- A scalar keeps its declared `text` beside its typed `value`. Type only by what the format defines:
  JSON's and TOML's grammars, YAML's tag or the YAML version the document declares. Where versions
  disagree and none is declared, the value is `Ambiguous`. A null the format defines is `KnownAbsent`.
- Never follow a reference out of the document or expand one inside it: a YAML alias is a value that
  names its anchor's path, and an `!include` tag is the application's to read (`Unknown` plus a finding).
- A value's meaning is not yours: a key named `wheel_radius` is a number, with a unit only where the
  document states one.
- Probe for settings, not for a grammar: JSON and YAML hold data too. A root sequence, GeoJSON, an object
  keyed by content or made only of tables is `config.shape_not_configuration`, left to the text adapter or a
  dialect adapter, which claims it above `STRUCTURE`.

## World and record context (ADR 0020)

For registers, geometry, photos, video files and documents:

- Tables: one `StructuredTable` and one `StructuredRecord` per row, citing the row as `Row(r)` so each
  cell's place is its `RowCell`. Keep cells as the source types them; never infer a CSV cell's type.
  Blank is `Unknown`; only a token the source or its spec defines as none is `KnownAbsent`.
- A `Site` or `Asset` per row or feature that names one, with its ids and names each citing its cell or
  span. Don't copy the rest of the row into it.
- Geometry: a `SpatialArtifact` per file, unit / CRS / frame as declared (`NotCovered` where the format
  has no place), objects cited by `ObjectLocator`.
- Media: an `Image` per still, a `Video` per video track, `capture` from EXIF / XMP / container metadata.
  Never apply EXIF orientation, never caption.
- Documents: one `DocumentRecord`, then `DocumentBlock`s in reading order with the text of each exact
  span and its page region. Document your block rule. Set `role` only where the format declares it.

## What the runtime owns (and adapters must not reimplement)

| Concern | Runtime mechanism |
|---|---|
| resume | skip chunks whose id is already committed in the store |
| cache | the chunk id, which covers source id, adapter id and version, config hash, libraries and context; a version or config change recomputes only that adapter's chunks (ADR 0031) |
| validation across sources | `validate/` engine over the store |
| sandboxing | each source's probe (every adapter's `probe` in one call, through the probe engine), each `plan` and each `ingest` in a child forked for it, with CPU, wall-time and memory limits, no network, no writes but beneath the call's scratch directory, no new processes (ADRs 0030, 0033); the result crosses back as JSON, so it must be what the contract says (records with their `to_json`) |
| explanation | assembles `ProbeResult`, `plan` output and `descriptor` into the receipt |
| scheduling / backpressure | bounded worker queues (M9) |

## Trade-off accepted

An adapter cannot resume *inside* a chunk. Formats with one giant natural chunk redo it after a crash.
Mitigation: plan finer chunks where the format allows (MCAP chunks, PDF pages, row ranges). The alternative —
fifteen bespoke checkpoint formats — was judged worse.

## Adding an adapter

1. New subpackage `adapters/<format>/` exporting an adapter class and its `DESCRIPTOR`; copy
   `adapters/text/`. Import only `neptune.model`, `neptune.identity` and `neptune.adapters.contract`.
2. Add one line to `adapters/builtin.py`. Nothing else names a format.
3. Fixtures: at least one valid file, one truncated, one corrupted, one empty, one renamed/extensionless
   (`tests/fixtures/text/` is the model).
4. Tests through `neptune.adapters.harness.ingest_source`, which runs every law: probe on each fixture,
   exact citations (every `EvidenceRef` resolves to the bytes), partial corruption (findings, not
   failure), determinism (ingest twice, byte-identical), output independent of chunk size, lineage
   (another version or config gives new ids, the old output untouched).
5. No changes to `runtime/`, `store/` or `model/`. If you need one, stop and write an ADR.
