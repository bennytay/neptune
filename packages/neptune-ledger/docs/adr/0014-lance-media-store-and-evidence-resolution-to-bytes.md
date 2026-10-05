# 0014 — Lance media store: evidence references resolved to verified bytes, and lazy hydration

- Status: Accepted
- Date: 2026-10-05
- Issue: MVL-96
- Amends: ADR 0013 §3 (`ObjectStore` gains `read_range`).

## Context

Every record cites its evidence as `EvidenceRef(source content id, locator)`: a path of steps,
outermost first. Step 0 addresses the source's stored bytes, and each later step addresses
inside what the transform decoded from the step before (root ADR 0016 §1). The catalog's
`resolve` (ADR 0004, ADR 0006 §5) says which packages hold a source and the routes each one
states: a blob in the package, or the locations where a referenced source was seen. It never
fetches bytes. Layers above need the bytes themselves: the frame a perception claim cites, the
page a procedure cites, the row a site record came from. They must get them without parsing
packages or sources themselves.

Several things go wrong if this is done badly:

- The returned bytes differ from the cited content, because a source moved or changed.
- A hostile path or member range reads outside its source.
- A decoded frame is passed off as evidence.
- The same citation gives different bytes on two calls.
- A large video is read whole to serve one slice.

Referenced sources raise one more problem: their locations are relative to an ingest root
that no package records (root ADR 0010).

## Decision

1. **Three modules under `lake/`.**
   - `evidence` resolves a reference to verified bytes.
   - `decode` extracts a cited region with the Ledger's own decoders.
   - `media` holds the Lance table and the hydration API (`MediaLake`).

   None is a catalog-API call. `query` (MVL-98) and `access/` (MVL-99) decide the surface, so
   `contracts/` is unchanged, as in ADR 0013. The Ledger never imports the compiler's
   adapters. It decodes with pinned libraries: `mcap`, `mcap-ros2-support`, `lz4`,
   `zstandard`, `pillow`, `pypdfium2`, `pyarrow` and `pyyaml`. No migration: nothing new is
   stored in PostgreSQL.
2. **Resolution to bytes.** `EvidenceResolver.resolve(ref, as_of)` calls the catalog's
   `resolve`. It then tries each route of `fetch` in registration order. Within a referenced
   route, it tries each stated location in each source store, in order.
   - A **materialised** source is the package's `blobs/sha256/..` object (the catalog's stated
     `blob_path`), read through the package's `ObjectStore` (`locate`, ADR 0013 §2).
   - A **referenced** source is a `local` or `local_raw` location: a root-relative path. It is
     looked up in the deployment's `SourceStore`s for that package
     (`source_roots(package_id, root_locator)`, the ADR 0007 interface). The default is none,
     so a referenced source is unavailable until a deployment says where its ingest roots
     are. Another kind of location (an external object) is reached by its connector, not by
     the Ledger.
   - The first object with the source's stated size is the **route**. Every later object of
     that size, under the same chunk ids, is a fallback: a chunk is read from the first copy
     whose bytes still hash to it, so one edited copy never hides an intact one.
   - The locator is parsed strictly by the compiler's step reader. A leading `byte_range` is
     the **span** of stored bytes; any other step 0 means the whole source. The remaining
     steps are **inner**. A span past the source's size is refused.
   - Status is one of four:
     - `resolved`: `open()` reads the span;
     - `unavailable`: no route has the bytes (moved, removed or resized);
     - `unresolvable`: no package holds the source;
     - `invalid`: the reference or `as_of` is outside the contract.
3. **Reads are lazy and verified per chunk.** A `SourceReader` serves any range of the span.
   - It reads only the chunks the range overlaps, using `ObjectStore.read_range`, or a seek on
     the `SourceStore`'s stream. The last chunk read is kept, so sequential reads hash each
     chunk once. A nested `byte_range` (an archive member) narrows the reader and stays lazy.
   - It hashes each chunk against the chunk ids in the route package's `source_artifact`
     record, from the catalog's stored body (ADR 0009).
   - A chunk that differs is `file_digest_mismatch`, and one that has gone is `file_missing`.
     No unverified byte is ever returned, and a slice costs at most one chunk per end.
   - This amends ADR 0013 §3: `ObjectStore` gains `read_range(key, offset, length)`, and
     both stores implement it.
4. **Variants and decoders.** `hydrate(ref, variant)`. The locator's innermost step decides
   which variants apply:

   | Variant | Innermost step | Artefact |
   |---|---|---|
   | `bytes` | `byte_range` only | the bytes every step addresses: the source itself, lazily, or decoded bytes when a step lies inside compressed content |
   | `frame` | `record_range`, or `byte_range` of one MCAP Message record | the one image message (ROS 2 CDR `sensor_msgs/{Image,CompressedImage}`), as PNG |
   | `image_region` | `image_region` | the box of a stored image, or of a frame, as PNG |
   | `page` | `page`, `page_region` | the page rendered by PDFium at 2 px/pt, or its box, as PNG |
   | `row` | `row`, `row_cell` | a CSV or Parquet row or cell as canonical JSON (a null is `{"null": true}`) |
   | `value` | `json_pointer`, `span` | the JSON or YAML node as its own text; a span's text as UTF-8 |

   - **Inner steps.** A step addresses inside what the step before decodes to (root ADR
     0016). Before each inner step the scope is decoded: an MCAP source's scope that is
     exactly one Chunk record becomes the chunk's uncompressed records (none, zstd or lz4,
     inflated to exactly the stated size, its CRC checked when stated); a gzip, bzip2, xz or
     zstd stream is decompressed; anything else is used as it is. Every inflation is bounded
     by `max_decoded_bytes`. A nested `byte_range` must lie inside its scope, so an archive
     member cannot leave its archive.
   - **`bytes` never drops a step.** It follows every `byte_range` step. When none needed
     decompression, the result is still the source, read lazily and verified per chunk. When
     one did, the result is the decoded bytes, and `inflated` names each decompression
     (`mcap-chunk:zstd`). A locator with any other step is `invalid_request` for `bytes`:
     those steps are read by a decoding variant.
   - **Frames.** The compiler cites a message as the Chunk record's byte range, then the
     Message record's byte range in the chunk's records (root ADR 0034), or as one byte range
     when unchunked. That Message record's channel and schema are looked up in the recording's
     summary, else in one bounded pass over its records. A `record_range` is served too: with
     a summary, only the chunks its index says overlap the range and hold the channel are
     read; without one, the file is read through once. MCAP chunks are always inflated here,
     never by the library.
   - **Tables as the compiler reads them.** A `row` counts records, the header included
     (root ADR 0016). The CSV grammar and delimiter sniffing mirror the compiler's tabular
     adapter (root ADR 0042): records end at an LF outside quotes, a blank line is not a
     record, a leading BOM is skipped, and the delimiter is the one of `,`, tab, `;` that the
     first 64 KiB agree on, else `,`. The artefact names the delimiter and whether it was
     sniffed. A delimiter declared in the compiler's config (`csv_delimiter`) is not in the
     citation, so the Ledger cannot know it; a `row_cell`'s stated column name catches most
     such mismatches. A cell that is not UTF-8 is `{"hex": ...}`; bytes holding NUL are not a
     table. A Parquet row has one cell per leaf column, named by its dotted path, as the
     compiler's header lists them. A date, time, timestamp or duration is its stored integer
     (its unit and zone are in `type`), a decimal its exact text, and a leaf inside a list or
     map is `"decoded": false`, as in the compiler.
   - **Pointers.** The node is returned verbatim, from its first character to its last: no
     reader's typing of a scalar is assumed (YAML `yes` or `0x10`, a float's digits). The
     document is walked as a stream (JSON tokens; PyYAML's pure-Python parser events, as the
     compiler reads YAML), so nothing is built from it. Keys are matched by their text, as
     the compiler cites them. A key held twice on the path is `invalid_request`: no silent
     choice of one. Being a slice of the document, a block collection's text keeps the
     indentation of every line but its first (`/cam0` of `cam0:\n  intrinsics: [1, 2]\n  model:
     pinhole` is `intrinsics: [1, 2]\n  model: pinhole`), so it need not parse as YAML on its
     own; a reader dedents it by the node's column.
   - **No silent choices.** A frame is exactly one message: a range holding none, or several
     sharing a tick, is `invalid_request` (cite `[t, t + 1)`). Multi-frame images are not
     decoded. A `row_cell` whose stated `column_name` differs from the table's header is
     refused. EXIF orientation and PDF `/Rotate` are not applied to rasters or regions. A
     `page_region` on a rotated or cropped page is `no_decoder` in this version.
   - **Not decoded in this version (`no_decoder`):** `video_frame` (cite its byte range for
     bytes), ROS 1 bags, non-CDR MCAP channels, adapter-specific steps, `object` and `frame`.
5. **Hostile input.**
   - Paths are checked with `location_path`: no `..`, `.`, empty part or NUL.
   - They are opened component by component with `O_NOFOLLOW` (ADR 0006 §3), so a link reads
     as missing and nothing outside a store is opened. A stated path that would escape is
     `unsafe_entry`.
   - Every library decoder runs inside `guarded`, so anything it raises on hostile bytes is
     `undecodable` (root ADR 0029). One read of more than `max_decoded_bytes` through a span
     (a forged length field) is `unsafe_entry`, before the bytes are fetched.
   - Bombs are `unsafe_entry`: decompressed output and whole-scope reads above
     `max_decoded_bytes` (256 MiB), and rasters or page renders above `max_pixels` (64 M).
   - A JSON or YAML document is at most `max_document_bytes` (8 MiB, the compiler's limit for
     parsed documents), and walking it costs its text plus its nesting: an 8 MiB document of
     tiny nodes peaks at 8.4 MB (JSON) and 18 MB (YAML) beyond its bytes, where building it
     cost 1.5 GB and 1.1 GB per 48 and 4 MiB. Nesting past `max_document_depth` (200, the
     compiler's config `max_depth`) is `unsafe_entry`. Time is linear in the document: an
     8 MiB JSON worst case takes about 3 s. PyYAML's pure-Python parser is about 30 times
     slower, so a YAML document of more than `max_yaml_events` (1M) parser events is
     `unsafe_entry`, refused after about 8 s on a loaded 20-thread host; real documents hold
     far fewer. YAML aliases are refused.
   - Parquet repeats the compiler's guards before a page is decoded: a footer of at most
     16 MiB that fits the file (an encrypted one is `no_decoder`), at most 16 384 leaf columns,
     row-group counts that are not negative and add up, and every column chunk read lying
     before the footer and decoding, as stated, to at most `max_decoded_bytes` in all. Only
     the cited leaves' top-level columns of the cited row group are read, 1 024 rows at a
     time, so a run-length column decodes one batch, not its group. Byte arrays are read as
     dictionaries, so a value a dictionary repeats over a batch is held once and only the
     cited row is decoded. What a batch decodes is bounded as the footer states it, before a
     page is read: a value per row of a flat leaf (a fixed-length byte array at its declared
     width) and every stated value of a repeated leaf; over `max_decoded_bytes` is
     `unsafe_entry`. A footer and page headers that both understate are bounded only by the
     batch: the decoding subprocess (§ Consequences) bounds the rest.
   - PDFium is not thread-safe, so every call into it holds one process-wide lock.
6. **Findings.** `MediaFinding(code, subject, detail)`, never an exception. The codes shared
   with the catalog API (`as_of_out_of_range`, `file_digest_mismatch`, `file_missing`,
   `invalid_request`, `unresolvable_evidence`, `unsafe_entry`) mean what they mean there. Three
   are the media store's own:
   - `no_decoder`: this version has no decoder for a step or encoding;
   - `undecodable`: the bytes are not the format the step needs;
   - `unknown_artefact`: no artefact at a pinned snapshot.

   Only `CatalogUnavailable` and a media-table I/O failure raise.
7. **The media table.** One Lance dataset per tenant at
   `<ledger store>/tenant_<id>/tables/media/` (ADR 0013 §2). It is a tenant-wide table,
   because an artefact derives from content and not from a package.
   - **Columns:** `artefact_id`, `source`, `locator` (canonical JSON), `variant`,
     `transform_id`, `transform` (canonical JSON of decoder id, decoder version and the version
     of every library it uses), `media_type`, `size`, `sha256`, `metadata` (canonical JSON
     facts such as width, height, channel, log time and page), and `data`. `data` is a Lance
     blob v2 column with `inline_size_threshold=0`, so bytes are stored out of line and read
     lazily (`take_blobs`, seekable).
   - **Format:** pinned at Lance file format 2.2, with `pylance==12.0.0`.
   - **Identity:** `artefact_id = sha256(canonical JSON {evidence, transform id, variant})`. The
     same citation under the same decoder and library versions is extracted once and served
     from the table after that. A library or decoder upgrade is a new transform and a new
     artefact (a new lineage), and never rewrites a row.
   - **Writes:** a write is a `merge_insert` on `artefact_id` (insert when not matched), retried
     on commit conflicts. Concurrent hydrations of one artefact may race, but readers take the
     row at the lowest address, and both rows hold the same bytes (§8).
8. **Lazy hydration, snapshots and determinism.**
   - `hydrate()` returns a `Hydration` at once. Nothing is resolved, read or decoded until
     `read()`.
   - `read()` returns `Hydrated(value, findings)`: a `SourceSlice` for `bytes`, otherwise an
     `Artefact`.
   - For an artefact, `read()` first looks the id up in the table. Only on a miss does it
     resolve, verify, decode and store.
   - Each write is a Lance version. `Artefact.snapshot` is the version it was read at.
     `hydrate(..., snapshot=v)` reads only from version `v` and never extracts; a `bytes`
     hydration takes no snapshot (`invalid_request`). The catalog must resolve the reference
     at `as_of` before anything is served, a stored artefact included. Versions are
     never cleaned up automatically (no `auto_cleanup`), so an extraction is reproducible for
     as long as an operator keeps the version.
   - Stored values hold no wall clock and no randomness, so two hydrations of one reference,
     in one table or in two, give equal rows and byte-identical artefacts.
   - Lance's own commit metadata (a version timestamp, data-file names) is storage
     bookkeeping. It is not part of any artefact, and it is why determinism is stated over
     rows and bytes, not over the table's files.
   - Every artefact is a derivative. It names its evidence reference and its transform and
     replaces nothing: the source stays where it is, and a `bytes` read is served from it.
9. **Tests** (`tests/test_ledger_media.py`), on real small fixtures (`tests/fixtures/media/`,
   written by `tests/ledger_media_fixtures.py` and frozen):
   - every citation in the four worked examples (drone, manipulator, mobile robot,
     quadruped) resolves to its exact bytes: `byte_range`, `json_pointer`, `row` and six
     adapter steps;
   - every `json_pointer` and `row` citation in them hydrates, and each row holds the text the
     compiler's record states for each known cell;
   - frames from a manipulator's referenced MCAP and a quadruped's materialised MCAP, checked
     pixel by pixel, and the same frame from zstd, lz4, unchunked and tar-nested recordings;
   - every message the compiler's own package cites (zstd and lz4 chunks) hydrates as a frame
     and as its Message record's bytes;
   - archive members inside bzip2 and xz streams, the document limit with its peak memory,
     and forged, lying and encrypted Parquet footers;
   - pages and page regions of a drone report, an image region of the mobile robot's photo,
     Parquet and CSV rows and cells, a span of a drone mission note, and archive members (plain
     and gzip) of a quadruped calibration tar;
   - two hydrations byte-identical across two tables, snapshot pinning, and concurrent
     hydrations;
   - laziness, and one-chunk range reads of a video;
   - a moved source, a moved package, changed and resized sources, links, escaping paths,
     truncated MCAP, MCAP chunk, gzip and pixel bombs, YAML aliases, duplicate keys, and
     malformed requests: all findings.

## Alternatives considered

- **Re-parse with the compiler's adapters.** Rejected. No workspace member may import them
  (`tests/unit/test_merge_freshness.py`), and an adapter's output is canonical records, not
  media. Decoding with pinned libraries makes the extraction a
  separate, provenanced transform.
- **Copy every cited source into Lance (or reference it as a Lance external blob).** Rejected.
  It duplicates immutable evidence, and the copy could drift from the content id. `bytes` is
  served in place and verified per chunk. Only derivatives are stored.
- **Hash the whole source before every read.** Rejected. It reads a multi-gigabyte log to
  serve one frame. The compiler already records chunk ids for exactly this (root ADR 0009),
  and verifying the touched chunks gives the same guarantee for the bytes returned.
- **Trust size alone, as series reads do (ADR 0013 §4).** Rejected for media. A series file is
  a package file `verify` covers, while a source is outside the package and is the evidence
  itself. A same-size edit must never be served.
- **One finding type with only catalog-API codes.** Rejected. "No decoder" and "not that
  format" have no catalog code, and reusing `invalid_request` for a corrupt source would say
  the caller erred. The media codes stay out of the contract until `access/` exposes the
  store.
- **Store the artefact keyed by package.** Rejected. The same bytes in two packages would be
  extracted twice. An artefact is a function of content, locator and transform.
- **Choose the first message of a multi-message range, or frame 0 of an animated image.**
  Rejected. That is a silent assumption. The citation must name one frame.

## Consequences

- Layers above get cited bytes, frames, pages, rows and values without parsing anything. Every
  artefact says what it came from and what made it.
- The Ledger depends on `pylance` (with `numpy`, `pydantic` and others transitively), `mcap`,
  `mcap-ros2-support`, `lz4`, `zstandard`, `pillow` and `pypdfium2`, all pinned. A bump to any
  decoder library is a new extraction lineage. numpy is pinned to 2.3.5: 2.4's stubs use 3.12 syntax that every
  member's 3.11 mypy check would fail to parse.
- Decoders run in the Ledger's process. `guarded` turns a Python exception into a finding, but
  a native crash in PDFium, Pillow or a decompressor on hostile bytes would end the process.
  The sources were already ingested by the compiler in its sandbox. Revisit with a decoding
  subprocess before `access/` (MVL-99) lets untrusted callers trigger hydrations.
- A stored artefact is served without rehashing its blob against its `sha256`: the table is
  Ledger-owned. Check it when `access/` exposes the store.
- The tests' compiled package (`tests/fixtures/media/compiled/`) is written by `neptune
  ingest` as a subprocess (`tests/ledger_media_compiled.py`), so its citations are the
  compiler's own; regenerate it when the compiler's MCAP citations change.
- Referenced sources need a deployment to name its ingest roots per package (`source_roots`).
  Until it does, they are `unavailable`. Recording ingest roots, or a registry of them, is for
  `access/` (MVL-99).
- Looking up an artefact scans the `artefact_id` column. Add a Lance scalar index when the
  table outgrows a scan. Revisit concurrency if many writers hit one tenant's table:
  merge-insert retries serialise them.
- Revisit when video or ROS 1 frames must be decoded (a pinned `av` or `rosbags`), when PDF
  regions on rotated pages are needed, or when artefacts must be removed (it needs its own
  ADR, as package removal does).
