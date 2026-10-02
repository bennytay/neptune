# 0009 — Roboto and Rerun Hub connectors: files as Sources, catalog metadata as stated records

- Status: Accepted
- Date: 2026-10-03
- Issue: MVL-155
- Builds on: ADR 0006 (object-store Sources), ADR 0001 §1 and §4; root ADRs 0009 (source revisions), 0020 §5 (structured records), 0026 §6 (local-only), 0051 (stated evidence), 0058 (plugin loader)

## Context

Fleets keep recordings in two hosted catalogs besides plain buckets. Roboto holds a dataset's MCAP, bag and
config files with a REST API, tags, comments and annotations ("events": a name, a time range, a file or
topic). A Rerun Hub holds `.rrd` recordings as segments of a dataset, with a catalog of segments, entity
paths and timelines. Both sit in front of ordinary object stores. D2 needs each as a read-only `Source`,
and needs what the catalog says about the objects without turning it into interpretation.

- **A catalog is evidence somebody authored.** A Roboto event saying `protective_stop` from `t0` to `t1`,
  or a Rerun schema row naming `/arm_cell3/joint_states/shoulder`, is `stated`, never `observed` and never
  inferred (root ADR 0051). The time on an event is on a clock the API does not name.
- **Both are hostile inputs and need the network.** A response is untrusted JSON; Roboto answers a file
  request with a time-limited URL on another host; local-only workspaces must refuse both.
- **Rerun documents no HTTP access to its catalog.** The Hub is gRPC (the `redap` protocol over HTTP/2 and
  protobuf). Neither is in the standard library, and ADR 0006 §10 adds no dependency.
- **The compiler has no RRD adapter.** Objects can be listed and read, not decoded.

## Decision

1. **Identity and URLs.**
   - Roboto: `roboto://<org id>/<dataset id>/<path prefix>`. A file is
     `ExternalObjectRef("deploy_roboto", "<org>/<dataset>/<relative path>", "version:<file id>:<version>")`.
     Roboto numbers a file's versions, so a re-upload is a new token at the same location and the ledger
     chains it as a new revision. The file id is in the token: a file deleted and uploaded again at one path
     has a new id and version 1, so it is never read as unchanged. A directory, a deleted file and a
     reserved (not yet uploaded) file are not objects. Ids hold no `/` or `:`; the prefix is verbatim.
   - Rerun: the factory is given the path of a catalog export file (§5). An object is the same
     `ExternalObjectRef` as through `deploy_s3`, `deploy_gcs` or `deploy_azure_blob` (ADR 0006 §3), because
     the connector resolves each storage URL with that connector's own client. A `.rrd` replaced under the
     same key is a new revision of the same location, and the catalog never changes identity.
2. **Roboto's API, read only.** `GET` of the dataset record, events, comments and a file's record and
   signed URL; one `POST`, the dataset files query (a query, not a write). The transport refuses any other
   `POST` path. Bytes are one ranged `GET` of the signed URL. Reading a file first fetches its record and
   compares `file_id` and `version` with the listed token, so a file that moved on is `object_changed`
   before any URL is asked for. An expired signed URL is asked for again once.
3. **Declared, closed options and a declared token.** Unknown options are refused. Roboto: `endpoint`,
   `content_hosts`, `api_version` (sent as `X-Roboto-Api-Version`, so the shape does not move under stored
   documents), `events`, `comments`, `max_records`, `event_clock`, plus the object-store limits. The token
   is the declared `roboto_api_token`, else `NEPTUNE_ROBOTO_API_TOKEN`. Nothing ambient (`ROBOTO_*`, the
   SDK's config file) is read. It is printable ASCII with no space, goes only to the API host, and is in no
   repr, finding or transform.
4. **Catalog metadata is `stated` structured records** (`neptune_deploy.sources.stated_records`).
   - The objects a catalog returned become one **catalog document**, `{"items": [...]}` in a fixed byte
     form (sorted keys, ASCII, items sorted and de-duplicated). It is a function of the objects, not of the
     order or paging they arrived in. Its `ExternalObjectRef` is `(connector, "<scope>:<part>",
     "records:<sha256>")`; the `:` keeps it from ever sharing an id with a file.
   - Each table cites `/items`, each row `/items/<i>`, each cell `/items/<i>/<key>`, all `stated`, with
     the document's content id as evidence source. A cell is the value as given: text stays text, numbers
     and booleans keep their type, an object or array is its own sorted JSON as text. `null`, an absent
     key and `""` are `Unknown`. Nothing is parsed, converted or normalised: a time stays the integer the
     catalog wrote, `"kind": "timestamp"` stays text.
   - A time a catalog names (`start_time` and `end_time` of an event; an index of a Rerun dataset) becomes
     a `TimestampDomain` with `field` the catalog's name and `scope` the dataset. Its epoch, timescale,
     resolution and role are `Unknown` unless the operator declared them (`event_clock`,
     `timeline_clocks`). An event row carries a `@clock:<field>` cell citing that domain, so an annotation
     over a time range says which named clock it is on, and which one is not known. Declared clocks are
     part of the transform config.
   - Roboto's catalog is the dataset record, the files the source listed (its prefix and revision), events
     and comments. A part that failed or stopped at a limit is a finding and is absent or partial; nothing
     is invented to fill it.
5. **Rerun catalog export.** The connector reads what Rerun's own SDK returns (`segment_table()` columns
   `rerun_segment_id`, `rerun_layer_names`, `rerun_storage_urls`, `rerun_size_bytes`, `schema()`, indexes)
   from one JSON file with a Neptune envelope (`neptune.rerun_catalog_export` version 1; `catalog`, a name
   for the Hub that is part of every id; `dataset`, `segments`, `schema`, `indexes`). It does not speak
   gRPC. Only the three documented segment columns are read; every other key is carried through as stated
   metadata. The file is strict JSON, a regular file the operator named (no symlink, no FIFO wait),
   bounded (`max_export_bytes`, 64 MiB).
   - Each distinct storage URL (`s3://`, `gs://`, `az://`) is resolved with one exact-key listing on its
     own store, in byte order of `(provider, bucket, key)`, up to `max_objects`. Per-provider object-store
     options (`storage.s3.endpoint` with `store`, `region`, `anonymous`) and credentials are ADR 0006's.
     The key is never decoded.
   - Entity paths, archetypes, components and timelines are the catalog's word. Nothing reads the `.rrd`.
   - A single-layer segment whose stated size differs from the store's is `catalog_size_differs`; the
     store's is used. A catalog that stops naming an object never makes it `gone`: only the object-store
     connector asserts that, of its own prefix.
6. **Hostile input.**
   - Responses are strict UTF-8 JSON: no duplicate keys, no `NaN`, nesting bounded, a page limited to
     8 MiB, a next token to 4 KiB. A number a response states is checked as 1 to 19 digits before `int`
     sees it. A malformed files page is `response_invalid` and stops the listing; a malformed events or
     comments page is `catalog_invalid` for that part only.
   - A signed URL must be https or loopback http, no longer than 8 KiB, with no user information,
     fragment, space, dot segment, backslash, `+` or empty name in its query, and on the API's own host or
     one in `content_hosts` (at most eight). The token is never sent to it. A refused URL is `read_failed`
     for that object and no request is made.
   - No redirect is followed (`redirect_refused`). Every request has a deadline. A short or shifted
     `Content-Range`, a failing content host, or a range ignored after offset 0 is a read finding. Findings
     and errors carry codes, counts and statuses, never a URL, header, token or response text.
   - A skipped file record (no usable id, version, size or path, a key that is not UTF-8, the same path
     listed with two versions) is one finding per reason and is used by nothing.
7. **Read only and local-only.** Both factories call `network.require_network` before building anything,
   and every request calls it again. `LocalOnlyError` propagates as policy, never a finding. Building a
   source reads the Rerun export file but sends nothing.
8. **Findings** are `deploy_roboto.*` and `deploy_rerun.*`, with the object-store codes for listings and
   reads, plus `catalog_failed`, `catalog_limit`, `catalog_invalid`, `value_unrepresentable` (Roboto) and
   `segment_invalid`, `storage_url_unsupported`, `object_not_found`, `catalog_size_differs`,
   `value_unrepresentable` (Rerun). Reads of a Rerun object are reported by the connector of its store
   (`deploy_s3.object_changed`), merged into the Rerun source's findings, sorted by id.
9. **Compiler gap.** An RRD adapter does not exist in the compiler. MVL-203 (M4) files it. Until it lands,
   `.rrd` objects are listed, fingerprinted and readable by range, and their entity paths and timelines
   exist only as the catalog's stated records. MCAP files from Roboto need nothing new: they are the MCAP
   adapter's.
10. **Fixtures and oracles.** No test reaches a network. Roboto documents are recorded-shape fixtures
    written by `tests/fixtures/connectors/make_connector_fixtures.py` and validated against the vendor's
    own pydantic models (`roboto` 0.58.0 `DatasetRecord`, `FileRecord`, `EventRecord`, `CommentRecord`) with
    `uv run --no-project --with roboto==0.58.0`; no dependency is added. They are not captures of a live
    service, and the ADR does not claim the service behaves exactly as its models do. An in-process server
    (`tests/deploy_roboto_fake.py`) serves them over real HTTP with knobs for redirects, foreign signed
    URLs, ignored and shifted ranges, malformed pages and expiry; the Rerun tests use ADR 0006's S3 fake.
    The fixtures are an AMR fleet shift, an arm cell and a legged patrol: no morphology is assumed.
    Neither connector has run against the live service. A live test is the D2 gate's, as for ADR 0006.

## Alternatives considered

- **Speak Rerun's gRPC.** Needs HTTP/2 and protobuf; the standard library has neither, and ADR 0006 §10
  forbids a dependency for this. An export written by the SDK the operator already runs costs one file.
- **Read the `.rrd` here for entity paths and timelines.** That is an adapter's job over a byte-level
  format, and the connector would be parsing raw sources twice. The catalog's word is `stated`; the
  adapter's reading will be `observed`.
- **Make Roboto events `observed` or give them an epoch.** The API does not state a clock. Assuming Unix
  nanoseconds because the SDK docs say so is an assumption about units and clocks (non-negotiable 4).
- **Follow Roboto's signed URL anywhere.** A response could send the connector, and the machine's network
  position, to a host of its choosing. The operator names the extra hosts.
- **One identity scheme for every catalog.** A Rerun object that is an S3 object would then not match the
  same object seen through `deploy_s3`, and a bucket read two ways would be two histories.

## Consequences

- A Roboto or Rerun dataset is a Source the compiler can ingest by range, with its annotations and
  catalog as stated records beside it, each citing the document the API returned.
- Rerun support needs an operator step (the export) until a gRPC client can be admitted by a superseding
  ADR. The envelope and the objects stay the same.
- `entry`, `fetch` and `_gone` of `RerunSource` and `ObjectStoreSource.__init__` skipping are relied on
  by name; a test runs every inherited method so a new attribute in `ObjectStoreSource` fails here first.
- Revisit when Roboto publishes a clock for event times, when the compiler gains an RRD adapter (the
  catalog's entity paths can then be checked against the file's), or when a live run disagrees with the
  recorded shapes.
