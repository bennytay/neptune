# 0027 — The probe engine: sniffing, bounded container inspection, and selection as findings

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-8
- Extends: ADR 0024 §7 (the selection rule) and §8 (how an unread source reaches the receipt)

## Context

File names are not evidence: robotics data arrives renamed, extensionless, inside archives and
compressed streams. ADR 0024 gave every adapter a `probe(head, hints)` and the registry a pure
selection rule, and left to MVL-8 the engine that runs probes over real sources, reports a tie
instead of breaking it, and says why a source was not read. The audit narrowed MVL-8 to that
engine: format-specific probing (does this MCAP parse, what schema does this bag declare) ships
with each adapter. What the engine needs on its own: to say what unclaimed bytes look like, to
look inside containers without extracting them or trusting what they declare, and to isolate a
probe that crashes. All input is hostile (non-negotiable 9): an archive that declares a terabyte,
a directory of a million entries, a stream that never ends.

## Decision

1. **The engine lives in `discovery/`** (`neptune.discovery.probe.ProbeEngine`), built per job
   from one `AdapterRegistry` and one `ProbePolicy`. It reads a source's head (`PROBE_HEAD_SIZE`)
   and, for containers, bounded tails and member heads; never more. It applies the registry's
   rule unchanged (`registry.select` over every adapter's result): the engine never re-ranks,
   weights names or breaks ties. There is one adapter registry; the engine is a client of it.
2. **Sniffing is observation, not a claim** (`neptune.discovery.sniff`). A fixed table of
   signatures (robotics logs, point clouds, documents, media, data files, containers) plus the
   magic every registered adapter declares (`FormatSpec.magic`) is matched against the head,
   most specific first; the head is also classified as text (`utf8`, `utf8_bom`, `utf16_bom`,
   `damaged_utf8`, `binary`, `empty`). Sniffing never selects an adapter. It names containers to
   open and tells the receipt what an unclaimed source looks like ("MCAP signature; binary").
3. **Containers are inspected, never extracted** (`neptune.discovery.containers`). Zip, tar, gzip,
   bzip2 and xz are opened with the engine's own bounded parsers (zip64, ustar, GNU long names and
   pax paths included); zstd and 7z are recognised and reported as not opened. The report lists
   each member as the container states it (name byte-exact, kind, declared sizes, method, link
   target) and probes each regular member's head with the same adapters, so the report says what
   the container holds. A member that is itself a container is opened in turn.
   - Nothing is resolved: `../escape.txt` and a symlink's target are listed verbatim and nothing is
     written anywhere. Member names are never used as evidence.
   - Every read is bounded by `ProbePolicy`: `max_members` listed per container, `scan_bytes`
     decoded per compressed stream (and read per central directory), `max_depth` containers along
     one path (default 2: a tar inside a gzip), and `max_ratio`: a member declaring more than that
     many decoded bytes per compressed byte is reported and not probed. A zip member declares in
     its directory and is not decoded at all. A gzip declares only in its trailer, and its last
     eight bytes are a trailer only if the stream ends there (in a file cut short they are deflate
     data), so it is decoded to the budget first: an end, a cut or a corrupt stream settles those
     bytes, and only a stream still going at the budget is held to the ratio by its trailer.
     Decoding stops at the budget whatever the stream declares, so a bomb costs at most
     `scan_bytes` of memory.
   - Declared sizes are never trusted for reading: a member's head is decoded to a full head
     whatever the container states, adapters are hinted what the stream holds, and a declaration
     the stream contradicts is a `container_corrupt` finding. A stream container (gzip, bzip2, xz)
     inside a member cut by the budget is not opened, because its trailer lies beyond the bytes.
   - Streams laid end to end (gzip members as `gzip -c >>` appends them, bzip2 streams as pbzip2
     writes them, xz streams) are one member whose content is their concatenation, as the formats'
     own tools decode them; one member per stream would cut a tar written across streams. They are
     decoded to `scan_bytes` rather than to a head, since their boundaries lie beyond it, and each
     stream counts against `max_members`, since one that decodes to nothing spends no budget and a
     file of them would otherwise be walked to its end. Each gzip member's stated size is checked
     against its own stream; the content is complete only when every stream was decoded whole; a
     gzip's `size` is the sum of its members' statements once all were reached, the trailer's
     statement while the first member is still being decoded (the whole for the usual single
     member), and withheld when more was seen but not the end. Null bytes after a stream are
     padding, skipped up to `scan_bytes` in all (more is a `bytes` limit, since a stream may
     follow); other bytes that open no stream are a `container_corrupt` finding.
   - Citations follow ADR 0016: a member's stored bytes are a `ByteRange` in its container's
     scope (compressed if the member is compressed; a gzip member is its whole stream), and a member
     of a nested container adds a step inside what the engine decoded. Headers are metadata.
   - Nothing ingests members. An archive adapter would, and it would reuse these parsers; until
     then the report serves selection, explanation (MVL-15) and the receipt.
4. **Selection outcomes are findings from a transform of their own.** The engine is a producer:
   `TransformRecord(adapter_id="neptune.probe", version, config=policy)`, so a policy change is
   a new lineage of findings, as ADR 0006 §4 requires of any config. Codes (`neptune.probe.*`,
   documented in `probe.FINDING_CODES`):
   - `ambiguous` (error): adapters tie at the top; the finding names them and their reasons.
     Nothing is chosen until a manifest names one (MVL-14).
   - `unsupported` (error): no adapter claims the source; the message says what sniffing saw,
     the container summary if any, and which adapter the name suggested and declined.
   - `adapter_failed` (error): a probe raised or returned the wrong type; that adapter is out of
     this source's candidates and the others still choose. Only the exception's class is recorded.
   - `name_mismatch` (info): the name's extension belongs to another adapter's format; the bytes
     decided. Said once, for the receipt, and never acted on.
   - `container_corrupt` (warning), `container_limit` (warning, `details.limit` is `members`,
     `bytes`, `depth` or `ratio`), `container_not_inspected` (info).

   Subjects cite bytes: the whole source for a selection finding, the member's entry for a member
   finding. Severity follows ADR 0017 §9: a source that produces no canonical output is an error.
5. **The explanation is `SourceProbe.to_json()`**: every adapter's result (confidence 0 included,
   so a dry run can say why each declined), the selection, the sniff, the container report and
   the findings. It is deterministic and canonical. It is not a record: ADR 0024 already rejected a
   selection record kind, and findings carry the part that must reach the package.

## Alternatives considered

- **`libmagic` / `python-magic`.** Broad, but a native dependency whose database and results vary
  by host and version, which breaks determinism; and it knows nothing of MCAP, ULog or PCD. A
  small table the repository owns is exact and testable.
- **`zipfile` and `tarfile` for inspection.** They read a whole central directory or walk every
  header, decompress on demand with no output bound, and resolve names; none of that is wanted on
  hostile input. The engine's parsers are about 400 lines, read exactly what they cite, and stop
  at every limit. The tests use the standard library as the oracle for every citation.
- **Letting the engine weigh names or declared magic.** Simpler ties, but then a name could decide
  against the bytes, which ADR 0024 forbids. Names and declared magic inform findings only.
- **Picking the first tied adapter, or a priority order.** Deterministic, and a silent guess
  (non-negotiable 4). The manifest (MVL-14) is the place to say which.
- **Per-member findings for members nobody reads.** A zip of a thousand unclaimed files would be a
  thousand findings; the container's one `unsupported` finding carries the summary and the report
  carries the detail.
- **A probe record kind.** Rejected in ADR 0024 §7; nothing here changes that.
- **Deferring container inspection to MVL-10's sandbox.** The sandbox bounds a process; the engine
  must still never ask for a terabyte. Both are needed, and the engine's bounds are its own.

## Consequences

- Renamed, extensionless and compressed sources are selected from their bytes; a tie or an
  unclaimed source is a finding in the package, and the receipt names the probe engine as the only
  transform that looked at an unread source, so "read by `neptune.probe` alone" means not decoded.
- The runtime (MVL-6) calls `ProbeEngine.probe` per source and stores its findings beside the
  engine's transform; `explain` (MVL-15) renders `SourceProbe.to_json()`; a manifest (MVL-14)
  resolves `ambiguous`.
- An adapter whose declared extensions or magic do not match its probe gets `name_mismatch` or an
  informative `unsupported` finding in tests, which is the cross-check MVL-7's contract asked for.
- The signature table grows with the adapters: an adapter's declared magic is sniffed
  automatically, and a built-in entry is one line.
- Revisit when an archive adapter lands (it should reuse `containers` and may want member readers
  over a `SourceReader`), if a container format needs a decoder outside the standard library
  (zstd arrives in Python 3.14), or if probing dominates discovery time on large corpora.
