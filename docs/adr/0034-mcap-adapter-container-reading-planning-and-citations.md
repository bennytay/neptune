# 0034 — The MCAP adapter: our own container reader, planning from the summary, exact citations

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-17

## Context

MCAP is the run container most robotics evidence arrives in, and the first format adapter after
the M2 gate. MVL-17 asks for header, footer and index inspection; schemas, channels and topics;
message counts and time coverage; attachments and metadata; compressed chunks; access by topic
and time; source-preserving citations; and corruption detection with partial recovery. Its
acceptance: a large MCAP is inspected cheaply, indexed lazily, sliced by topic and time, and
normalised without loading the recording into memory.

The runtime fixes the frame (ADRs 0024, 0028, 0030, 0033): every call is a fresh sandboxed child
with CPU, wall, memory and a 64 MiB reply cap; `ingest` is pure per chunk; output must not depend
on how the plan cuts the source; a damaged source is findings, never a raise. The M2 review's R1
warns that every read touching an 8 MiB piece re-hashes it, so a plan that reads across the file
costs a full pass. The model is frozen (ADR 0023): no new fields, no new record kinds here.

## Decision

1. **We read the container ourselves** (`neptune.adapters.mcap`), with the standard library plus
   **`zstandard` and `lz4`**, the two compressions the specification defines and the libraries the
   official reader itself uses. They are new runtime dependencies (ADR 0001 §4), imported only by
   this subpackage, decompressing into one buffer bounded by the chunk's declared size (lz4 is fed
   in pieces; a stored chunk is freed once decoded). They **are** output-affecting `libraries`,
   so their versions are in the transform id and every cache key: a whole chunk decodes to the
   bytes its format defines, but a chunk cut short keeps the prefix its stored bytes decode to
   (§7), and how far a library gets into an incomplete block is its own. The official `mcap`
   reader is a test oracle (`tests/fixtures/mcap/oracle.json`), never a dependency.
   The package imports its own modules and nothing of another adapter.
2. **Probe.** The magic (`\x89MCAP0\r\n`, format version 0) is `SIGNATURE`; a well-formed Header
   record after it is `VERIFIED`. Anything else is 0, whatever its name.
3. **What a recording becomes.**
   - One `Run` citing the Header record (the magic when there is none); `first`/`last` from the
     summary's Statistics, each citing its exact 8 bytes, on `log_time`; unknown when the file
     states no messages or no usable statistics. `logical_id` and `machine` are `Unknown`: MCAP has
     no field for them. The Header's profile and library stay in the cited bytes.
   - One `TimestampDomain` for `log_time` per file (scope `()`, role `receive`), citing the magic
     plus an adapter step `mcap:time_field{name}`, and one for each channel's `publish_time`
     (scope `(topic,)`, or `("channel", id)` without a topic; role `publish`), citing its Channel
     record plus that step: the recorder has one clock, every publisher its own. Resolution is
     1 ns, cited to the specification through the magic; epoch, timescale and monotonicity are
     `Unknown`, because the specification leaves the epoch "user-understood".
   - One `Stream` per channel: topic, message encoding and metadata verbatim; schema name and
     encoding citing the Schema record, the definition as the exact bytes of its `data`;
     `KnownAbsent` (the specification) for schema id 0; `message_count` from the Statistics
     map entry it cites, else `Unknown`; it, like the run's `first`/`last`, is `stated`: the
     writer's claim about records elsewhere, which only the indexed plan cross-checks; `first`/`last` `Unknown` (no per-channel extent is
     declared). A blank or non-UTF-8 string is `Unknown` with a finding, never a replacement
     character; a repeated metadata key is left out with a finding.
   - A channel or schema is cited at its record in a usable summary, else at its first record in
     the data section (inside a chunk where it is). Repeats that differ are a finding; the first
     declaration is read.
   - One `StructuredTable` per Metadata record (named by it, no header row) and a
     `StructuredRecord` per entry, cells key then value, cited `[record, Row(r)]`.
   - Attachments are embedded files no record kind holds: each is an info finding citing the
     record, with name, media type, times and data range, its CRC checked.
4. **Series.** One row per message: `seq` (the message's place among its channel's messages in
   file order), `time/0` log time, `time/1` publish time, `value/sequence` (`uint32`), and the
   locator of the whole Message record: one `byte_range` in the file, or the Chunk record's range
   and then a `byte_range` in its uncompressed records, offsets counted as Message Index records
   count them. `state/time/0` and `state/time/1` are always present: a u64 time past 2^63 − 1 is
   `unknown` in its row (a finding says so), never wrapped. Payloads are not decoded: each stream
   gets a `payload_not_decoded` finding, and its rows cite the bytes. Decoding them, and the
   clocks they carry (`header.stamp`), is MVL-21's.
5. **Planning reads as little as the file allows.**
   - *Head and tail*: the magic, the Header, the Footer and the summary, its CRC checked and
     parsed whole (at most 64 MiB). A summary out of bounds, over the limit, failing its CRC or
     malformed is a `summary_unusable` finding and is not used.
   - *Indexed* when the summary has chunk indexes (one per chunk, if Statistics count them), the
     indexes neither overlap nor leave the data section, the first and last chunk are where their
     indexes say, the counts agree with the Statistics where there are Statistics, and the
     summary declares every channel the index counts and every schema those channels name (the
     specification lets a summary leave the declarations to the data section; such a file is
     scanned, without a finding). Each chunk's count per
     channel comes from its Message Index records' offsets alone (each record's length is the gap
     to the next); a chunk whose offsets do not give counts is decompressed once. Nothing else of
     the data section is read to plan. An index that fails a check is an `index_invalid` finding.
     A chunk read at ingest is compared with the index entry that names it (start and end time,
     both sizes, compression): any difference is an `index_mismatch` finding with reason
     `chunk_fields`, citing the chunk; the chunk's own fields are the ones read.
   - *Walked lazily*: a chunk's records are walked over its decompressed bytes without an object
     per record (a message costs 16 bytes of kept index, its offset and log time, not a Python
     object), and at most `max_chunk_bytes / 31` of them, 31 bytes being the smallest message;
     records past that bound are not read and are one `too_many_records` finding. The Message
     Index check reads its entries lazily against that kept index.
   - *Scanned* otherwise: every top-level record is visited once and every chunk decompressed
     once, to find the declarations and count the messages. That is the price of a file cut short,
     with a damaged summary, or written without an index.
   - The data section is cut into ranges of whole *units* (a chunk and what follows it up to the
     next chunk, or a chunk and its Message Index records, or any other record) of at most
     `chunk_bytes` (64 MiB) and `max_rows` (100,000) messages, both constructor arguments. A chunk
     with more messages is read by several planned chunks, each emitting one stretch of its message
     order, the last open-ended. Chunk 0 holds the declarations; each data chunk carries its byte
     range, its chunks' index records, and per channel the `seq` its rows start from.
6. **Nothing depends on where ranges fall.** Within a range, an indexed chunk's messages are
   numbered from the counts its own index record gives (read again at ingest from the same summary
   bytes), so `seq` is the same however chunks are grouped; a message past its chunk's indexed
   count has no row and the chunk gets a `message_count_mismatch` finding, so `seq` ranges never
   overlap whatever an index claims. In the indexed layout, every unit starts at its indexed chunk
   or is skipped with an `index_mismatch` finding, and a chunk the index does not list gets no
   rows and the same finding: planning numbered none of its messages. Every finding cites one record or one chunk and
   is made by the one planned chunk that starts at it; problems with top-level messages in an
   unchunked file (undeclared channel, too short, time past 2^63 − 1) are summed once by the plan,
   and undeclared channels in any file likewise.
7. **Integrity and recovery.** Chunk and attachment CRCs are checked (0 means "not computed"); a
   chunk failing its CRC, its decompression or its size gives no rows and one finding. The
   data-section CRC is not checked: it needs one pass over the whole file in one call, against
   chunk purity and the call limits, and the fingerprint already hashes every byte. A file cut
   inside a chunk keeps the whole messages that chunk's stored prefix decodes to, a strict prefix
   of its message order (no gaps in `seq`), and the chunk gets a `chunk_truncated` finding (bytes
   decoded, bytes whose records are whole, bytes declared; the CRC cannot be checked); cut after
   its data, it loses only the summary (a warning). Every length is checked against its record before
   anything is read or allocated; `max_chunk_bytes` (256 MiB, config) bounds a chunk or record; it cannot be raised above 256 MiB
   (a `ConfigError`), because the adapter's declared memory is sized for that.
8. **Selection by topic and time** is config, so it is lineage: `topic_pattern` (a regular
   expression a whole topic must match; empty selects all) and an inclusive `log_time` window.
   Unselected messages keep their `seq` and get no row; every stream is still declared, with a
   `not_selected` finding. An indexed chunk outside the window or without a selected channel is not
   decompressed: the decision rests on the summary's index alone, so messages a lying index hides
   there have no rows, and the chunks so skipped are one `skipped_by_index` finding (their count
   by reason, `time` or `topics`; the first is cited). Only a chunk whose counts the index gives
   may be skipped, since the chunks after it are numbered from them. Consumers slice the package by topic (a series file per stream) and by time (rows
   sorted by `log_time`, Parquet row groups) and reach any message's bytes through its locator.
9. **Inspect** reads the head, the footer and the summary only, and reports the header, summary
   state, statistics, schemas, channels with their declared counts, chunk totals and each
   channel's extent at chunk granularity, attachments, metadata, and whether the summary carries a
   chunk index (`planning: "indexed"`; `plan` uses it only once it passes its checks, §5, and
   scans otherwise, which inspect does not tell, as the checks read the data section). Each list
   holds its first 1,000 entries and an `<name>_omitted` count, so a hostile summary cannot push
   the reply past the sandbox's cap.

## Alternatives considered

- **The `mcap` package as the reader.** It does not give a message's offset inside its chunk,
  raises on damage, reads through file objects rather than `SourceReader`, and trusts what the
  summary says: on `overlapping_index.mcap` it reads one message twice, and it does not check the
  summary's CRC. It needs `zstandard` and `lz4` anyway.
- **Pure-Python decompressors.** LZ4 is small enough, zstd is not, and both would be slow.
- **Scanning the data section to plan every file.** Simple, but a full pass (R1) where the
  summary exists to avoid it; kept only for files without a usable index.
- **Confirming every chunk start at planning.** One read per chunk touches every 8 MiB piece, a
  hash pass over the file. The planned chunk that reads a unit confirms it instead.
- **Counting and clamping per range.** Simpler contexts, but when an index lies the rows and the
  findings would change with the grouping, which ADR 0024 forbids.
- **Decoding payloads now** (CDR, ROS 1, JSON). MVL-21 owns schema introspection and the column
  mapping; a JSON stream's columns cannot be typed per chunk without it.
- **Payload bytes in a `value/data` column.** ADR 0025: wide payloads stay in the source, cited.
- **One `publish_time` domain per file.** It would assert that every publisher shares a clock.
- **Attachments ingested by other adapters.** Adapters never call each other; a nested source
  needs the archive pattern (ADR 0032, O3), not an MCAP special case.
- **Leaving `zstandard` and `lz4` out of the transform.** A whole chunk's output is fixed by its
  format, but a cut chunk's recovered prefix is the library's; leaving them out would let two
  versions write different records under one lineage and one cache key.
- **An object per record in a chunk.** Simple, but a chunk of small messages costs about eight
  times its bytes (measured: 32 MB for a 4 MB chunk of 120,000 messages, against 8.7 MB walked
  lazily), against the call's memory limit.

## Consequences

- Robotics recordings land with every message as a cited row, both container clocks, their
  declarations and metadata; a 60 MB, 2,000-chunk, 800,000-message recording is inspected in
  12 ms and planned in 32 ms reading 0.36% of it, 9 planned chunks of 100,000 rows each, about
  0.7 s and a 9 MB reply apiece; the sandboxed job commits it in 22 s and a rerun makes no plan or
  ingest call.
- Planning an index-less file costs a decompression of every chunk in one call; past what
  `cpu_seconds` and `wall_seconds` allow it is `limit_exceeded`. M9 can raise the limits per
  adapter or plan such files in passes.
- MVL-21 decodes payloads from `schema_definition` and row locators and adds `header.stamp`
  clocks; MVL-22 hydrates heavy streams through the locators; MVL-36 aligns `log_time` and
  `publish_time`. MVL-19 reads MCAP-backed rosbag2 storage: since adapters never import each
  other, it needs this reader lifted into a shared non-adapter package, by ADR, first.
- Fixtures' compressed bytes follow the pinned `zstandard` and `lz4`; an upgrade regenerates them.
  An upgrade of either is also a new transform: every MCAP record is re-ingested under new lineage,
  whether or not its bytes moved.
- They are the first native decoders inside adapter calls, the point at which ADR 0030 said to
  revisit a read allowlist. It stays deferred: they run under the same confinement as any adapter
  code, so a memory-safety bug in one reaches what a compromised parser already could, the files
  the user can read, into its own reply (`security.md`).
- Revisit if payload decoding moves into adapters, if lying indexes turn out common enough to
  confirm chunk starts at planning, if a whole-file CRC becomes necessary, or if format version 1
  of MCAP appears.
