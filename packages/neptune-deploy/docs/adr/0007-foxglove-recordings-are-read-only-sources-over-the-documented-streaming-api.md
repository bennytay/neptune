# 0007 — Foxglove recordings are read-only Sources over the documented REST and streaming API

- Status: Accepted
- Date: 2026-10-03
- Issue: MVL-154
- Builds on: ADR 0001 §1 and §4, ADR 0006 (the object-store Source this one shares its client, reads and
  hostile-input rules with); root ADRs 0009 (source revisions), 0026 §6 (local-only), 0058 (plugin loader)

## Context

Fleets that record with Foxglove already hold their MCAP files in the Foxglove Data Platform: indexed by
device, time range and topic, in a primary site Foxglove runs or in the customer's own bucket. D2 needs a
connector that turns that index into compiler Sources, and the declared metadata Foxglove keeps (which device,
which session, which topics) into evidence Memory's identity consolidator can use. The documented surface is
small, and each fact below is from it:

- The REST API is `https://api.foxglove.dev/v1`, authenticated with `Authorization: Bearer <API key>`. A key
  carries capabilities, only an organisation admin creates one, and a call needs the capability its reference
  entry names ([Authentication](https://docs.foxglove.dev/docs/api)).
- `GET /recordings` lists the index (`limit` up to 2000, `offset`, `sortBy`, `sortOrder`, filters such as
  `deviceId`, `deviceName`, `start`, `end`, `projectId`); each recording has `id`, `projectId`, `path`, `size`,
  `createdAt`, `importedAt`, `start`, `end`, `importStatus` (`none`, `pending`, `importing`, `failed`,
  `complete`), `device`, `key`, `metadata` and `sessionId`. `GET /recordings/{keyOrId}` reads one
  ([Recordings](https://docs.foxglove.dev/docs/api), "List recordings", "Get a recording").
- `GET /devices` lists devices (`id`, `name`, `projectId`, `properties`), and `GET /data/topics` lists a
  recording's topics (`topic`, `schemaName`, `schemaEncoding`, `encoding`, `version`; `recordingId` alone is
  allowed for a recording that is not imported yet) ([Devices](https://docs.foxglove.dev/docs/api),
  [Topics](https://docs.foxglove.dev/docs/api)).
- `POST /data/stream` does not stream. It "returns a link URL where you can download your data as an .mcap or
  .bag file"; the link is signed and "expires after 15 seconds"; `recordingId`, a time window, topics,
  `outputFormat` and `compressionFormat` select what it serves ([Stream data](https://docs.foxglove.dev/docs/api)).
  The documentation says nothing about `Range` on the link.
- Paging is `limit` and `offset` only; a `limit` over 2000 is a 400; a `429` carries `Retry-After`
  ([Sorting and pagination, Rate limits](https://docs.foxglove.dev/docs/api)). Times are RFC 3339 UTC with up to
  nine fractional digits.
- The `importId` parameter, `GET /data/imports` and "Delete an import" are marked deprecated: use the recording
  id or key ([Imports](https://docs.foxglove.dev/docs/api)).
- Foxglove warns: "Do not rely on Foxglove-generated IDs (e.g. `dev_abc123`, `rs_abc123`) as an external or
  internal identifier. These IDs may change if a device, session, or other resource is deleted and recreated,
  migrated, or otherwise reprovisioned." A recording's `key` is an idempotency key the user may set, and a
  device's name is unique within a project ([Manage data](https://docs.foxglove.dev/docs/data/primary-sites/manage-data),
  [Devices](https://docs.foxglove.dev/docs/data/devices), [Recordings](https://docs.foxglove.dev/docs/data/recordings)).

The compiler's rules apply in a harder place than a bucket: the index is live, paged by offset, and mutable;
the bytes come from a generated stream behind a link the API names; a device's name changes while its id stays.

## Decision

1. **Plugin surface.** One `neptune.sources` entry point, `deploy_foxglove`, with the factory signature of
   ADR 0006 §1: `factory(url, *, network, ledger=None, options=None, credentials=None, environ=None) ->
   FoxgloveSource`. `url` is `foxglove://<project id>`, or `foxglove://-` for every project the key reads. The
   source has the shape of the compiler's `Source` protocol and reuses the object-store types: `walk()` yields
   `StreamEntry` (an `ObjectEntry`) and then `SkippedObject`; `open()` returns the same windowed, seekable
   stream; `reader()` the same chunk-checked adapter reader; failed reads raise the same `ObjectReadError`.
   What the compiler must do to accept them is ADR 0006's compiler gap 2, not a new one. The shared parts moved
   into helpers the two connectors call (`classify`, `absent_candidates`, `ObjectStream`, `ObjectReader`,
   `Transport`, `read_range`); neither connector imports the other's policy.
2. **The index.** `GET /recordings`, `sortBy=createdAt`, `sortOrder=asc`, `limit` from `page_size` (1 to 2000,
   default 1000), offsets advanced by the entries a page returned, until a page is empty. Filters are the
   operator's: `projectId` from the URL, and the options `device_id`, `device_name`, `start`, `end`. Every
   entry is checked against the documented shape and bounded (§8); a recording whose `importStatus` is not
   `complete` has no data to stream and is skipped with `import_incomplete` (counted by status), never
   asserted gone. The status is read first: a recording that is not imported need not state
   `start`, `end`, `path`, `createdAt` or `projectId`, and is not `record_invalid` for lacking them. `importId`, `GET /data/imports` and `GET /data/coverage` are not used.
3. **Identity.** A recording is `ExternalObjectRef("deploy_foxglove", [<store>:]recording/<recording id>,
   <token>)`. The token is `import:<importedAt or ->;created:<createdAt>;size:<size>`, each part as the API
   states it. The project and the filters are not identity: they say what was asked for, and the same recording
   is the same object however it was asked for. A declared `endpoint` (staging, an emulator, a recorded-API
   fixture) needs a declared `store` name, whose scope is `<store>:`, as in ADR 0006 §3.
   - **Re-import.** A recording imported again keeps its id and gets a new `importedAt`: a new token on the same
     location, which the ledger chains as a new revision. Identical bytes under a new token are no new revision
     (root ADR 0009). The deprecated import id is therefore not part of the revision.
   - **Re-upload.** A recording deleted and uploaded again, or an id Foxglove reprovisioned, is a new object,
     and the old one is `gone` (§5). The two share a `key`, which is declared (§4) and never merges them:
     Foxglove's own documentation says its ids may change, and identity is not Neptune's to infer.
4. **Declared metadata is evidence.** `declared(location)` returns what the API *states* about one recording at
   one revision, as `Knowledge` values with `stated` provenance. Each cites the response object it came from:
   `EvidenceRef(<the recording's ExternalObjectRef>, (AdapterLocator("deploy_foxglove:response", {call,
   object_sha256, recording}), JsonPointer(<pointer into that object>)))`. `object_sha256` is the SHA-256 of
   the object in canonical JSON with nulls dropped, so provenance does not depend on page size or order.
   - `identifiers`: `foxglove.recording_id`, `foxglove.recording_key`, `foxglove.device_id`,
     `foxglove.device_name`, `foxglove.session_id`, `foxglove.project_id`, and one `foxglove.device_property.<key>`
     for each device property the operator names in `identifier_properties` and the device states as text.
     Nothing decides on its own that a property is an identifier. They are declared identifiers for Memory's
     consolidator and never a merge: two devices that share a name or a property stay two, and a device's
     properties are also kept verbatim (`device_properties`) whether or not they are identifiers.
   - A device renamed: the recording and the device list may give one device id two names. The name is then
     `Ambiguous` with both candidates, in that order, each with its own provenance, and a
     `device_name_differs` finding. The id is unchanged and `Known`. Nothing chooses a name, and the rename is
     no new revision of the bytes: it changes what is declared, not what is read.
   - `facts`: `created_at`, `end`, `import_status`, `imported_at`, `path`, `size` (the stored file's, not the
     stream's), `start`, verbatim. An absent one is `Unknown`. Times are not converted.
   - `topics` (each `Known`, sorted) and `topics_coverage`: `Known(count)`, `Unknown` if the call failed or
     exceeded its bounds, `NotCovered` if the `topics` option is off. `metadata`: the recording's MCAP metadata
     records, verbatim.
   - Declared metadata is read from the live index on every run, for every listed recording, independent of the
     ledger: an unchanged recording is not fetched, but what is declared about it is current.
5. **Discovery.** `discover(ledger)` classifies the index with the same helper as the object-store source: new,
   changed (same location, another token), unchanged (never measured or fetched), gone. A ledger recording a
   complete index lacks is gone only after `GET /recordings/{id}` answers 404, at most 1,000 checks per call:
   offset paging over a live index can skip an entry when another is deleted between pages, and a ledger may
   hold recordings another project's or device's source ingested. An unanswered or over-budget check is not
   gone (`gone_unverified`). An incomplete index asserts nothing gone.
6. **Bytes: ranged reads of a stream, no export.** `POST /data/stream` with
   `{"recordingId", "outputFormat": "mcap", "compressionFormat": <option, default lz4; none leaves the key out>, "includeAttachments": true}`
   (no time window, no topic filter: the stream is the recording, so metadata and attachments are kept) returns
   a link, and `GET <link>` with `Range: bytes=a-b` and `Accept-Encoding: identity` returns the bytes. A link is
   good for 15 seconds, so none is kept: each ranged read asks for a fresh one, two requests, and the
   64 KiB-to-8 MiB window of ADR 0006 §9 keeps that count small. The chunk plan drives what is fetched and
   nothing is downloaded whole except by the compiler's one streaming hash of a new or changed recording.
   - **Size.** The stream is Foxglove's re-encoding, not the stored file, so the recording's `size` is not its
     length. `walk()` measures each stream it yields with one one-byte ranged read and takes the total from
     `Content-Range` (or the `Content-Length` of a `200` from offset 0). A stream that states no total, claims
     more than 16 TiB, is empty, or cannot be opened is skipped with a finding (`size_unknown`,
     `stream_too_large`, `stream_empty`, `stream_unavailable`).
   - **Stability.** Foxglove does not document that its stream is byte-identical across requests. A read whose
     stated total differs from the measured size is `object_changed`, and the adapter reader checks every chunk
     against the artifact's chunk hashes before serving a byte: a stream that differs between requests fails
     loudly and is never read as a mix.
   - **The link is untrusted.** It must be `https` (or `http` to loopback) at the API's own host and port, or a
     host the operator declared in `link_hosts` (a host, on any port: the operator trusts the host); it may hold no user information, fragment, backslash, space or
     non-ASCII character, and no more than 8,192 characters; its query is sent verbatim. It never receives the
     API key. A redirect is never followed (`redirect_refused`). A `Content-Encoding` other than identity has
     no byte positions and is refused. The answer to a ranged read must be a `206` for exactly the bytes asked
     for, or a `200` from offset 0 that states its length (only the bytes asked for are read). All of
     ADR 0006 §8's range rules apply unchanged because `read_range` is the same code.
7. **The network boundary and credentials.** The factory calls `network.require_network` before it builds
   anything, and every request asks again (root ADR 0026 §6). The one `POST` is `POST <base>/data/stream`,
   sent by a `Transport` subclass that has no other way to send it; it creates a link and changes nothing.
   Every other request is a `GET`. Credentials are `credentials={"foxglove_api_key": ...}` or
   `NEPTUNE_FOXGLOVE_API_KEY`: no `FOXGLOVE_*` variable, no CLI credential file. The key is one printable ASCII
   token and appears in no repr, finding, transform or error. Capabilities cannot be inspected offline, so the
   operator issues a key with only the list and stream capabilities the reference names for the five calls
   above (for example `devices.list`); the connector sends no other request, so a wider key is not used wider.
   A `429` is a `rate_limited` finding and is not retried: Neptune does not sleep on a server's clock.
8. **Hostile input.** Everything is an API response, and everything is bounded:
   - Bodies are strict JSON (UTF-8, no duplicate keys, no `NaN` or `Infinity`, nesting within Python's limit,
     integers within `int()`'s digit limit) and at most 32 MiB; anything else is `response_invalid` and stops
     that listing, which is then incomplete. `null` is an absent field, never a value.
   - A recording must have the documented required fields (`id`, `projectId`, `path`, `size`, `createdAt`,
     `start`, `end`, `importStatus`), each bounded printable text or an integer from 0 to 2⁶³−1; a malformed
     entry is `record_invalid`, an id that is not `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` is `recording_id_invalid`
     (it never reaches a path, a URL or a pointer), one id with two different descriptions is dropped
     (`recording_duplicated`), one document over 1 MiB is not used. One bad entry costs that entry.
   - A listing holds at most `max_recordings` (1,000,000) recordings and `max_listing_bytes` (256 MiB) of
     canonical JSON; skipped ids are kept as their first 256 bytes with length and SHA-256, never whole. The
     first item past a limit stops the listing, taken in the order the API sends them, so the same recordings
     are kept for every page size; the finding says how many were covered. Pages are limited to 100,000, and a
     page that adds nothing new is a `pagination_loop`.
   - The device list is bounded by the same byte budget, a recording's topics by 10,000 and 16 MiB, and
     `declared()` keeps only the last 256 results. A device list that failed (a failed request, not a limit) is not remembered and a declaration made
     without it is not kept: the next call asks again.
   - Every request has the transport's per-request deadline, so a server that trickles is cut off. Digits in
     headers are ASCII only. No URL, key, signature, link host or error text reaches a finding, an error or the
     transform: configuration errors do not repeat the URL they refuse.
9. **Findings** carry codes, counts, statuses and ids as hex: `listing_failed`, `not_authorised`, `rate_limited`,
   `redirect_refused`, `response_invalid`, `link_refused`, `listing_limit`, `pagination_loop`, `record_invalid`,
   `recording_id_invalid`, `recording_duplicated`, `import_incomplete`, the stream codes of §6,
   `short_read`, `object_changed`, `object_gone`, `read_failed`, `devices_failed`, `topics_failed`,
   `gone_unverified`, `device_name_differs`, `identifier_property_unusable`. The transform record
   (`deploy_foxglove` 0.1.0) holds the project, the filters, `compression`, `topics`, `identifier_properties`,
   `max_recordings`, `max_listing_bytes` and the store name: what decided which recordings were seen and what
   the bytes are.
10. **Tests and dependencies.** Standard library only; `uv.lock` is unchanged. CI has no Foxglove account and no
    network, so tests run against `tests/deploy_foxglove_fake.py`, an in-process server that serves JSON files
    in `tests/fixtures/foxglove/` shaped as the reference documents them (five embodiments: an arm cell, an AMR,
    a legged robot, a marine vehicle and an unassigned upload; one recording not yet imported), with a real
    MCAP (`stream_robot.mcap`, a copy of the compiler's `robot_lz4.mcap`) as the stream. The fixtures were
    written from the reference, not captured from a live account; the connector counts as verified against the
    real service when the D2 gate review runs it with a read-only key, as ADR 0006 does for GCS and Azure.
    Members may not import a format adapter (`tests/unit/test_merge_freshness.py`), so tests read a recording
    through ranged `reader()` calls and check the MCAP magic and every chunk hash. The compiler's MCAP adapter
    was also run once over `reader()` by hand and produced the records and findings it produces over a local
    copy of the same bytes.

## Alternatives considered

- **The `foxglove-client` Python package.** It wraps the same REST API with `requests`; that is a new HTTP
  stack, a redirect-following default and a retry policy to switch off, in every workspace member's lock. The
  five calls are small. Lost.
- **Streaming time windows instead of byte ranges** (`start`/`end`/`topics` in the stream request, one small
  MCAP per window). The compiler's chunk plan is a plan over bytes of one artifact with a content id, and a
  window's MCAP is a different file each time. Lost; a time window remains available as the operator's
  `start`/`end` filter on the index.
- **Downloading the whole recording per recording** and hashing the file once. That is the bulk export the
  scope forbids, and it re-downloads every recording on every run. Lost.
- **`recording.size` as the stream's size.** It is the stored file's; the stream is a re-encoding, so reads
  past the stream's end or a short object would surface only as read errors. Measured instead.
- **The recording `key` (or the device name) as identity.** Foxglove says its generated ids may change, but a
  key is an operator-set idempotency key that can be absent, and a device name is a label that is renamed. Using
  either would merge things Neptune must not merge. They are declared identifiers instead.
- **Cursor pagination.** The reference documents `limit` and `offset` only. The cost is paging skew, which §5
  contains by asking the API before asserting a recording gone.
- **Following the link's redirect to the host it names.** The link's host is the API's input; following it would
  send a request wherever a response says. If the live service redirects, a superseding ADR decides which hosts
  to trust; until then the finding says so.
- **Webhooks** (`recording.created`, `recording.imported`). They push to an HTTPS endpoint Neptune would have to
  run; a Source pulls. Lost.
- **A Deploy record type for declared metadata.** The compiler's model owns record kinds (ADR 0001); the
  connector returns `Knowledge` values built from the model's own types and persists nothing.

## Consequences

- Deploy publishes the connector (`docs/contracts.md`). The compiler cannot run it end to end yet. The gaps are
  ADR 0006's, listed here so the coordinator sees them once more, plus one of this connector's:
  1. `neptune ingest` and the SDK refuse every non-`file` scheme; nothing routes `foxglove://` to a plugin Source.
  2. The scan and runtime accept only `LocalSource` and its entry types, so nothing fingerprints a `StreamEntry`,
     treats a `SkippedObject` as a skipped entry, quarantines one recording on `ObjectReadError`, carries
     unchanged revisions forward, records `gone` as `SourceAbsence`, or gives adapters `reader()`.
  3. The ledger keeps the first token it saw for identical bytes, so a recording re-imported over the same
     bytes counts as changed, and is measured, fetched and hashed again, on every run until its bytes change.
  4. Nothing consumes `declared()`: the compiler has no sink for external-source declarations. Until it has
     one, declared metadata is a return value and does not reach a package.
- An object that changes between measuring and reading fails with `object_changed`; a stream Foxglove does not
  serve byte-identically is detected, not tolerated.
- Every ranged read costs two requests (a link, then the bytes); a 429 fails that recording and the next run
  tries again. A job over a large fleet is slower than one over a bucket by that factor.
- Revisit if the live service shows that the link redirects, ignores `Range` or serves unstable bytes (§6), if
  Foxglove documents cursor paging or a revision field on recordings (§2, §3), if X2 secrets land (ADR 0006 §6),
  or if the compiler adds a place for declarations (consequence 4).
