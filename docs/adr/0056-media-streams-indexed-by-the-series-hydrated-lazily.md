# 0056 — Media streams: indexed by their series, hydrated lazily

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-22
- Builds on: ADR 0018 (streams and series), ADR 0036 §8 (derived tables), ADR 0049 (stream
  introspection)

## Context

Images, video and point clouds are most of a robot run's bytes. A wrist camera at 30 Hz, a lidar
at 10 Hz or a gimbal's H.264 stream makes an hour-long MCAP gigabytes long. Consumers (retrieval,
memory, training) want the frames around an event, on the clock they care about, without
decoding the run. MVL-22's acceptance says it: request media around a 10-second event without
decoding an entire hour-long run.

Most of the index already exists. Every adapter writes one series row per message (ADR 0018).
The row holds:

- the message's ticks on every clock the stream declares (`time/<i>`, each named by a
  `TimestampDomain`);
- the message's byte range (`locator/…`): in the file, or in its chunk's uncompressed records.

The series is sorted by clock 0 and stored as Parquet with row-group statistics. ADR 0049 then
infers which streams are images, compressed images and point clouds. Two things are missing:

- which streams are media, and how a row's locator becomes the frame's bytes;
- reading those bytes, and the derivatives (thumbnails, keyframes), without decoding everything.

The package schema is held for other work: no new canonical kind may land here.

## Decision

1. **The frame index is the series.** Nothing is copied. A frame is one series row:
   - its `seq`;
   - its ticks on every declared clock, each with its `TimestampDomain`, as the source encodes
     them and never converted (no UTC, no seconds);
   - its handle, the row's `EvidenceRef`.

   Clocks the container declares (MCAP `log_time` and `publish_time`, bag record time) are
   columns. A `header.stamp` is inside the payload: it is read on hydration (§4), never at ingest,
   because reading it would mean reading every payload.
2. **`media_stream`** is a derived kind (derived schema version 1), one line per media stream,
   written by the `neptune.media` transform (0.1.0). Its `upstream` is the adapters' and
   introspection's transforms. The line is `inferred`, because its kind is.
   - `media` is one of:
     - `image`, `compressed_image` or `point_cloud`: the stream's `known` `stream_semantic`. `basis`
       is that line's id and `evidence` is that line's evidence.
     - `video`: a declared video-packet type name (`foxglove.CompressedVideo`,
       `foxglove_msgs/msg/CompressedVideo`). No ADR 0049 semantic is video: a packet has the
       same fields as a compressed image. `basis` is absent and `evidence` is the type name's
       citation.

     An ambiguous or unknown semantic is not media. A tie is never resolved.
   - `frames`: the series rows, counted from each run's Parquet footer. Never declared, and no
     row is read.
   - `message_encoding`, as declared.
   - `hydrator`: how a row's locator becomes bytes, set by the stream's adapter. In 0.1.0 it is
     `mcap_message` for `mcap` and absent otherwise. Unhydrated frames still have times and handles.
   - `thumbnail` and `keyframe`: a `state` and a `reason`.
     - `on_request`: made lazily (§4).
     - `not_covered`: needs a codec. Compressed images and video are `codec_not_covered`, as is
       a raw image with no hydrator (`no_hydrator`) or another message encoding.
     - `not_applicable`: a still's keyframe (`every_frame_is_a_still`), or a point cloud.
   - `state`: `known`, or `not_covered` past the frame budget, with `counts`.
3. **Budget.** Every media stream's rows are charged against `max_frames` (default 2^24 per
   package, a transform config), in stream id order.
   - A stream that would pass the budget is `not_covered` with `counts`: its frames, the frames
     indexed before it, and the limit.
   - It gets one `neptune.media.frame_budget` warning finding, naming every such stream.
   - The query refuses a `not_covered` stream and does not scan it.
   - Indexing costs a stream its row count, not its rows. A stream of millions of tiny messages
     costs no more than ten.

   The other findings are info, with one finding per code and reason, naming every stream:
   - `neptune.media.derivative_not_covered`: a codec is missing;
   - `neptune.media.hydration_not_covered`: no hydrator covers the stream's adapter.

   A package without media gets no transform, no table and no finding.
4. **The query surface** (`neptune.sdk.media`) reads the package's media lines, records and
   series. It reads no source byte.
   - `media_window(package, clock, start, end)` returns, per media stream, every row whose ticks
     on `clock` lie in `[start, end]`.
     - `clock` is a declared clock name (`log_time`) or a `TimestampDomain` id. The ticks are on
       that clock.
     - It reads the clock column of the row groups whose statistics overlap the window, then
       only those row groups.
     - A stream without that clock is `not_applicable` (`no_clock`).
     - Past `max_frames` (default 100 000 per query) a stream is `not_covered` (`max_frames`)
       with its match count, and no frame of it is listed.
   - `Hydrator(source).payload(frame)` returns the message's payload and reads only what holds
     it: one record, or one chunk (decompressed with the adapter's own `open_chunk`, under its
     size limit, and kept for the next frame in that chunk).
     - A handle naming another source, more than `max_bytes`, or anything but an MCAP Message
       record is a `HydrationError`. Nothing is trusted.
     - `FileSource` counts the bytes it serves.
   - On a hydrated payload, with the standard library only:
     - `header_stamp`: the `std_msgs/Header` stamp and frame id, as declared (`sec`, `nanosec`).
     - `point_cloud_ref`: a `PointCloud2`'s shape, declared `PointField`s, and the byte range of
       its point data in the payload. No point is decoded, and fields are capped at 1024.
     - `image_thumbnail`: a nearest-neighbour PGM or PPM, at most 64 px on its long side, of a
       raw `sensor_msgs/Image` (`mono8`, `mono16`, `16UC1`, `rgb8`, `bgr8`, `rgba8`, `bgra8`).
       Its derivative provenance is the frame's handle and the `neptune.media.thumbnail` transform
       (config: side, sampling, formats; upstream: the media transform). It is made on request
       and never stored. The same frame gives the same bytes.
     - Anything else (another pixel encoding, a payload that does not hold its declared pixels)
       is `not_covered` with a reason, never a guess.
   - CDR (either endianness, aligned) and ROS 1 serialisation are read; other encodings raise
     `ValueError`.

## Alternatives considered

- **Copy each frame's times and range into a derived table.** This is what the issue's
  "indexing" suggests. It is a second copy of the series, 30–400 bytes a frame as JSON lines
  that `read_package` parses whole. An hour of 30 Hz video would add tens of MB, and it would
  add nothing: the series already holds every declared clock and byte range, sorted, with
  row-group statistics.
- **A canonical `media_frame` or `video_segment` kind.** This needs a package-schema bump while
  the schema is held. A stream's media kind is inferred anyway (non-negotiable 8).
- **Extract `header.stamp` into the series at ingest.** The adapter would have to decode every
  payload of every stream. That is the eager extraction this issue exists to avoid, and a format
  decoder inside a container adapter. Read on hydration, the stamp costs only the frames asked
  for.
- **Decode keyframes and thumbnails of JPEG, PNG or H.264.** These need codecs the standard
  library lacks, and no new dependency is allowed. The derivative states say `not_covered`, and
  the hook (`image_thumbnail`'s transform and provenance) is where a codec-backed version would
  land as a new transform version.
- **Store thumbnails in the package.** Thumbnails of every frame are eager extraction at another
  size. On request, with the provenance to reproduce them, they cost nothing until asked for.
- **Hydrate inside the adapter contract.** That would mean a fifth adapter method. The MCAP
  hydrator reuses the adapter's chunk reader from the SDK. Other formats get hydrators by
  adapter id as they need them, without changing the ABI.

## Consequences

- A package with media gains `derived/media_stream.jsonl` and the `neptune.media` transform, so
  its id changes once. Packages without media do not change: the MCAP, rosbag and flight-log
  goldens and the worked examples carry no media.
- The acceptance test ingests a generated 60 MB, hour-long MCAP and asks for 10 s at minute 30.
  It reads under 0.5 % of the file (about 230 KB) and lists 51 camera and 10 lidar frames.
- ROS 1 and rosbag2 media are indexed (times and handles) but not hydrated (`hydration_not_covered`)
  until their hydrators land. Video keyframes stay `not_covered` until a codec is allowed.
- Revisit if consumers need frame rows without the package's series (a moved package keeps its
  series, so this is not expected), if a codec dependency is accepted (as a new thumbnail
  transform version), or if in-payload clocks must be queryable before hydration. Then they would
  become a derived column under their own budget.
