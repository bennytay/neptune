# 0046 — The ROS 1 bag adapter: connections as streams, planning from the index, the same Run and Stream as MCAP

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-18

## Context

ROS 1 bags (format 2.0) are what legacy and many active stacks record. MVL-18 asks for bag
introspection, message schema preservation, connection and topic mapping, timestamps and clock
metadata, canonical Stream output, and known message helpers (TF, images, IMU, odometry, point
clouds). Its acceptance: a ROS 1 run takes part in the same canonical model as an MCAP one without
losing the original message or schema identity.

The frame is MCAP's (ADR 0034): every call is a fresh sandboxed child (ADR 0030); `ingest` is pure
per chunk and its output must not depend on how the plan cuts the source (ADR 0024); a damaged
source is findings, never a raise; the model is frozen (ADR 0023). The worked `mobile_robot`
example already fixes the shape of a bag's records: adapter id `rosbag1`, a `rosbag1:time_field`
step, `ros1msg` and `ros1` encodings, the md5sum in `metadata`. A bag differs from MCAP in ways
that matter here: it has no checksums; its index (Connection and Chunk Info records after the
chunks) is found through the Bag Header's `index_pos` and is absent from a bag that was never
closed; a connection is one publisher's link to a topic, and its header is what that publisher
stated.

## Decision

1. **We read the container ourselves** (`neptune.adapters.rosbag1`) with the standard library and
   **`lz4`**: `bz2` and `lz4` frames, bounded by the chunk's declared size (fed in pieces, one
   buffer). They are output-affecting `libraries` (`bz2` is the interpreter's, named
   `cpython-<major>.<minor>`; `lz4` its installed version), so they are in the transform id and
   every cache key: a whole chunk decodes to what its format defines, but a cut chunk keeps the
   prefix the library gets to. No new runtime dependency. The official `rosbags` reader is a
   test oracle (`tests/fixtures/rosbag1/oracle.json`), never a dependency; the package imports
   nothing of another adapter.
2. **Probe.** The magic line `#ROSBAG V2.0\n` is `SIGNATURE`; a well-formed Bag Header record after
   it is `VERIFIED`. Anything else, bag version 1.2 included, is 0.
3. **What a bag becomes**, in the shape MCAP gives a recording:
   - One `Run` citing the Bag Header record (the magic when it is damaged). `first`/`last` are the
     least `start_time` and greatest `end_time` of the Chunk Infos that hold messages, `stated`,
     each citing its exact 8 bytes; `Unknown` when no usable Chunk Info states them. `logical_id`
     and `machine` are `Unknown`: a bag has no field for either.
   - One `TimestampDomain` for the message record `time` (scope `()`, role `receive`: ROS record
     time is when the recorder received the message), resolution 1 ns cited to the magic (the
     format defines `sec` and `nsec`), epoch and timescale `Unknown`: ROS time may be wall time or
     simulated time and the bag does not say. Ticks are `sec * 10^9 + nsec` as stored; nothing is
     converted to UTC or normalised.
   - One `Stream` per **connection** (not per topic: two publishers on a topic are two
     declarations, and their `callerid`s must not merge). It cites the Connection record and is
     `stated`: the connection header is what the publisher said, and the bag records it. Topic is
     the record header's, schema name the header's `type`, schema definition the exact bytes of
     `message_definition`, each `stated` and citing its field. `schema_encoding` is `ros1msg` and
     `message_encoding` `ros1`: MCAP's registry names for the bag's own formats, cited to the
     magic, so a bag stream and an MCAP stream compare equal. `metadata` is every other connection
     header field verbatim (`md5sum`, `callerid`, `latching`, and anything a node added), plus
     `topic` when the header's differs from the record's (with a `topic_mismatch` finding).
     Missing, empty or non-UTF-8 values are `Unknown` with a finding; a repeated field is left out
     with a finding. `message_count` is the sum of the Chunk Infos' counts, `stated`, citing the
     span of the records it sums, `Unknown` without an index. `first`/`last` are `Unknown`.
   - **Message identity is preserved, not interpreted.** The type name, md5sum and definition of
     TF, images, IMU, odometry and point clouds (and every other type) are kept as the publisher
     declared them. Payloads are not decoded: each stream gets a `payload_not_decoded` finding.
     Helper decoding, and the `header.stamp` clock inside messages, is MVL-21's and needs new
     record kinds (a decoded-message table, a sample clock), which are not this issue's.
4. **Series.** One row per message: `seq` (its place among its connection's messages in file
   order), `time/0` the record time, and the locator of the whole Message Data record: the Chunk
   record's byte range, then a `byte_range` in its uncompressed data, offsets counted as Index
   Data records count them. There are no value columns and no state columns (a time always fits
   64 bits).
5. **Planning reads as little as the file allows.**
   - *Head*: the magic and the Bag Header (read no further than its 4096 bytes).
   - *Indexed* when `index_pos` points at a section of Connection and Chunk Info records (read
     only up to 64 MiB), whole records only, in numbers the Bag Header states, Chunk Infos in
     strictly increasing `chunk_pos` inside the data section, every connection they count
     declared, and no chunk claiming more messages than its size limit could hold (a plan whose
     claims would need over 10,000 extra stretches is refused likewise). The data section is not
     read to plan it. A check that fails is an `index_invalid` finding with its reason (`info`
     for `index_pos` 0, a bag that was never closed; `warning` otherwise) and the bag is scanned.
   - *Scanned*: every top-level record visited once, every chunk decompressed once, to find the
     connections (the first declaration in file order) and count the messages. That is the price of
     a bag never closed, cut short, or with an index that lies.
   - The data section is cut into ranges of whole *units* (a chunk and what follows up to the next
     chunk) of at most `chunk_bytes` (64 MiB) and `max_rows` (100,000) messages, constructor
     arguments. A chunk with more messages is read by several planned chunks, each emitting one
     stretch of its message order, the last open-ended. Chunk 0 holds the declarations; each data
     chunk carries its byte range, its units (and their Chunk Infos), and per connection the `seq`
     its rows start from. At most 10,000 connections are declared; the rest are a finding.
6. **Nothing depends on where ranges fall.** An indexed chunk's messages are numbered from the
   counts its own Chunk Info gives (read again at ingest), so `seq` is the same however units are
   grouped; a message past its chunk's count has no row and the chunk gets a
   `message_count_mismatch` finding, so `seq` ranges never overlap whatever an index claims. Every
   unit starts at its listed chunk or is skipped with an `index_mismatch` finding; a chunk the index
   does not list gets no rows and the same finding. Findings are made once per unit by the planned
   chunk that starts at it; the plan makes the ones about the whole source.
7. **Integrity and recovery.** A bag has no checksums, so a chunk is checked by its framing: it
   must decompress to exactly its declared `size`, and its records must frame exactly. A chunk
   failing that gives no rows and one finding (`decompression_failed`). A bag cut inside a chunk
   keeps the whole messages the stored prefix decodes to, a strict prefix of its message order
   (no gaps in `seq`) and a `chunk_truncated` finding. Index Data records are checked against the
   chunk they follow (counts), a chunk's message times against its Chunk Info, and Connection
   records inside chunks against the one declared (a different topic or header under an id already
   declared is `conflicting_declaration`; the first declaration is read). Every length is checked
   against its record before anything is read or allocated; a header over `max_header_bytes`
   (1 MiB, at most 64 MiB) is skipped by its two lengths and never read; a header holds at most
   1,024 fields; `max_chunk_bytes` (64 MiB, at most 256 MiB) bounds a chunk's stored and
   uncompressed bytes (a `ConfigError` above, because the adapter's declared memory is sized for
   that); a chunk's records are walked without an object per record, at most `max_chunk_bytes / 46`
   of them.
8. **Inspect** reads the head and the index only: the Bag Header's counts, whether the index is
   usable (`planning: "indexed"`, else `"scan"`), chunk and message totals and the first 1,000
   connections with topic, type, md5sum, callerid, latching and count (`connections_omitted` after
   that), so a hostile index cannot push the reply past the sandbox's cap.

## Alternatives considered

- **The `rosbag` / `rosbags` packages as the reader.** `rosbag` needs a ROS install; `rosbags`
  raises on damage (it refuses `unclosed.bag`, `truncated.bag` and a chunk of unknown
  compression, which are exactly the bags that matter), trusts its index and reads through file
  objects. A dependency for a format this small, with every hostile case still ours to bound.
- **One stream per topic.** It would merge publishers' `callerid`, `latching` and md5sums and
  invent a declaration the bag does not make; a consumer that wants a topic groups its streams.
- **A second clock from `header.stamp`.** It lives in the payload; MVL-21 decodes it.
- **Observed rather than stated streams.** The bag records the connection header; the publisher
  made the claims in it. `observed` would say Neptune saw them true.
- **Confirming every chunk start at planning.** One read per chunk touches every 8 MiB piece, a
  hash pass over the file (M2 review R1). The planned chunk that reads a unit confirms it.
- **Topic and time selection and index-based skipping** (MCAP's `topic_pattern`, `log_time`
  window). Not in the issue; row filtering belongs to consumers of the package. Adding them later
  is a config change and a new lineage.
- **Per-record findings for repeated damage.** A hostile bag can repeat one fault millions of
  times; findings are aggregated per unit or per chunk, with a count and the first record cited.

## Consequences

- ROS 1 bags land as Runs and Streams a consumer cannot tell from an MCAP recording of the same
  messages: `tests/unit/adapters/test_rosbag1_vs_mcap.py` ingests one recording of a mobile
  manipulator (joint states, odometry, tf) as bag and as MCAP and compares topics, types,
  encodings, definition bytes, md5sums, callerids, counts, the run's extent, the clock's ticks and
  every message's seq, time and payload bytes, each read back through its own citations.
- A bag never closed, or whose index is damaged, is planned by decompressing every chunk in one
  call; past what `cpu_seconds` and `wall_seconds` allow it is `limit_exceeded`. M9 can plan such
  bags in passes. Its Run extent and stream counts are `Unknown`: nothing stated them.
- Follow-ups, not built here: decoding TF, image, IMU, odometry and point-cloud payloads and the
  `header.stamp` clock (MVL-21, with the record kinds it needs); topic and time selection; a
  `ros1msg:field` locator step for decoded fields (the worked example names it).
- Fixtures' compressed bytes follow the pinned `lz4` and the interpreter's `bz2`; an upgrade of
  either regenerates them, and is a new transform: every bag record is re-ingested under new
  lineage.
- Revisit if payload decoding moves into adapters, if bag format 1.2 matters, or if lying
  indexes turn out common enough to confirm chunk starts at planning.
