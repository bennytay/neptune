# 0006 — Object stores are read-only Sources over one client interface and the standard library

- Status: Accepted
- Date: 2026-10-02
- Issue: MVL-153
- Builds on: ADR 0001 §1 and §4; root ADRs 0009 (source revisions), 0026 §6 (local-only), 0058 (plugin loader)

## Context

Robot evidence often lives in buckets, not folders: a fleet's MCAP and bag uploads, an arm cell's
calibration exports, a vehicle programme's drive logs. D2 needs one connector that lists a bucket
prefix and hands its objects to the compiler. It must hold the compiler's rules in a place where every
rule is harder: the store is remote, mutable, paginated and hostile.

- **Identity.** Root ADR 0009 gives external objects `ExternalObjectRef(connector_id, object_id,
  revision_token)` and chains their revisions on `(connector_id, object_id)`. A new token over the same
  bytes is not a new revision.
- **No network in adapters.** An adapter is a pure function of its bytes (root ADR 0030). A `Source` is
  where bytes come from, so it is the network boundary. Root ADR 0026 §6 says anything that uses the
  network calls `Workspace.require_network(purpose)` first and is refused while the workspace is
  local-only (the default).
- **Plugin surface.** Root ADR 0058 (MVL-200) admits `neptune.sources` entry points but never calls
  them: "what a connector is given is MVL-153's decision". Members may not import `neptune.discovery`
  (root `tests/unit/test_merge_freshness.py`), so the connector cannot subclass the compiler's
  `Source`; it implements the protocol structurally.
- **No secrets service.** Platform X2 secrets are not on `main`. Credentials must come from somewhere
  an operator controls without a new service.
- **CI has no cloud and no network.** MinIO's community server is archived and its binaries are gone
  (`dl.min.io` answers 410), so a MinIO fixture cannot be the CI oracle either.

## Decision

1. **Plugin surface.** Deploy registers three `neptune.sources` entry points, one per provider, named by
   its connector id: `deploy_s3`, `deploy_gcs`, `deploy_azure_blob`. Each value is a factory with one
   signature:

   ```
   factory(url, *, network, ledger=None, options=None, credentials=None, environ=None) -> ObjectStoreSource
   ```

   `url` is `s3://<bucket>/<prefix>`, `gs://<bucket>/<prefix>` or `az://<account>/<container>/<prefix>`.
   `network` is the compiler's `Workspace` (anything with `require_network(purpose)`). `ledger` is the
   ingest root's `SourceLedger`. `options` are declared (§7), `credentials` declared (§6). The returned
   source has the shape of the compiler's `Source` protocol: `walk()` yields `ObjectEntry` (with
   `location` and `size`, as `SourceEntry` has) and then `SkippedObject` (raw key and finding code,
   where `SkippedEntry` has a raw path and a `SkipReason`), and `open(location)` returns a seekable,
   buffered binary stream. Failed reads raise `ObjectReadError`, an `OSError`, where `LocalSource`
   raises `SourceAccessError`. These are its own types, so the members' import rule (no
   `neptune.discovery`) stays as it is; this settles the question ADR 0001's consequences left to the
   first connector. The compiler's scan must accept them when it ingests plugin Sources (compiler
   gap 2). The source adds `listing()`, `discover(ledger)`, `reader(location, artifact)` (an adapter's
   `SourceReader`) and `findings()`. Importing the package touches nothing.
2. **One interface, three clients.** `StoreClient` has two calls: `list_page(prefix, cursor, size)` and
   `get_range(key, token, start, length)`. Everything else (which keys are kept, order, coverage,
   findings, ledger discovery, reads) is the source's, so the providers cannot differ in policy.
   - S3 and S3-compatible stores (MinIO, Ceph RGW, R2, moto, GCS's XML interoperability API):
     `ListObjectVersions` by default, keeping each key's latest version and dropping a key whose latest
     entry is a delete marker; `ListObjectsV2` with `versions: false` for stores without it. Requests
     are SigV4-signed, or unsigned when anonymous access is declared.
   - GCS: the JSON API's `objects.list`, and `alt=media` downloads pinned by `generation`.
   - Azure Blob: `List Blobs`, and `Get Blob` pinned by `versionid` or `If-Match`.
   S3 is verified against a real S3 server (moto 5.2.3, §10). GCS and Azure are implemented against
   their documented wire formats and tested against in-process fakes of them. They count as verified when
   a live test runs each against its emulator (fake-gcs-server, Azurite), which the D2 gate review does.
   If either fails there, its entry point is withdrawn until it passes.
3. **Identity.** An object is `ExternalObjectRef(<connector id>, <scope><key>, <token>)`. On a
   provider's public endpoint the scope is `<bucket>/`, or `<account>/<container>/` for Azure: those
   names are global there. A declared endpoint (MinIO, Ceph, an emulator) has its own bucket
   namespace, so it requires a declared `store` name, and its scope is `<store>:<bucket>/` (or
   `<store>:<account>/<container>/`). Two sites that each have a bucket `logs` therefore never share an
   identity, whatever ledger they reach. No bucket, account, container or store name holds `/` or `:`,
   so the parts never run together. The key is verbatim. The token is `version:<id>` (an S3 version id
   other than `null`, an Azure version id), `generation:<n>` (GCS), else `etag:<etag>` (quotes
   stripped). A changed object under one key is therefore a new token at the same location. The ledger
   chains it as a new `SourceRevision` superseding the old one, and identical bytes under a new token
   are no new revision (root ADR 0009). The endpoint is not part of identity: like a local root path, it
   says where bytes were read from, not what they are. The declared store name says whose bytes they
   are, so moving one store to a new host keeps its identity.
4. **Listing and determinism.** The source reads every page and sorts the objects by key (code-point
   order, which is UTF-8 byte order). The listing, its findings, their ids and the transform do not
   depend on page size, page order, entry order or the wall clock. A test lists one bucket with page
   sizes from 1 to 1,000, pages shuffled, and gets identical results for each provider.
   - A key listed twice is one object if both entries agree, and is dropped with `key_duplicated` if
     they do not: no arrival order decides.
   - A cursor seen before stops the listing (`pagination_loop`). Only a sha256 digest of each cursor
     is kept for this, never the cursor. A continuation token or marker longer than 4 KiB stops the
     listing (`response_invalid`).
   - Pages are limited to 100,000. Distinct keys, used or not, are limited to `max_objects` (default
     1,000,000). The bytes the listing holds are limited to `max_listing_bytes` (default 256 MiB):
     kept keys and tokens whole, and each unused key as at most its first 256 bytes, with its length
     and sha256. Each limit is a `listing_limit` finding naming the last key covered
     (`covered_through_hex`); every key after it is not covered. A store that lists huge or unusable
     keys therefore cannot grow a listing without bound: 20,000 keys of 30 KB outside the prefix hold
     about 5 MB, where they would otherwise hold 600 MB.
   - A listing stopped by a limit keeps the first `max_objects` keys below the greatest key it saw,
     and drops every finding about later keys. Stores list in key order, so every key below the
     greatest has been seen whole, duplicates included, and a limited listing is the same for every
     page size. A listing stopped by a failure or a loop depends on where it stopped, and its finding
     says where.
   - A listing that stopped early is `complete: false`, and nothing is asserted gone from it.
   - The only wall-clock reading is the SigV4 signing time. It reaches the request, never an output.
   - Findings carry codes, counts, statuses, offsets and keys (as hex), never error text, received
     byte counts, URLs or credentials.
   - The transform record (`deploy_<provider>` 0.1.0) holds provider, store, bucket, account, prefix,
     `versions` and `max_objects`: what decided which objects were seen. It holds no endpoint or
     credentials.
5. **Incremental discovery against the compiler's ledger.** `discover(ledger)` sorts each listed
   object into one of four groups:
   - new: no ledger head, or the head is an absence;
   - changed: the head is a revision whose token differs;
   - unchanged: the head is a revision with the same token. It is never fetched or probed.
   - gone: a ledger revision under this connector, scope and prefix that a complete listing no longer
     holds.
   With a ledger, `walk()` yields new and changed objects only. Keys the listing saw but could not use
   are never called gone.
6. **The network boundary and read-only credentials.**
   - The factory calls `network.require_network` before it builds anything. The transport calls it
     again before every request, so a workspace switched to local-only refuses the next request.
     `LocalOnlyError` propagates as the compiler's policy error, never as a finding.
   - The transport's only method is `GET`. It never follows a redirect: `http.client` does not, and any
     `3xx` is `redirect_refused`. Following one would send the request and its signature wherever the
     server says, which is the object store's symlink.
   - An endpoint is `https` with the default verified TLS context, or `http` to a loopback host only. An
     endpoint holding user information is refused at configuration time with a fixed message:
     credentials are declared, never put in a URL. A query or fragment is refused too. An error names
     a URL only as scheme, host and port, never its user information, path or query. No error,
     finding or transform holds a credential, and a test checks every exception chain.
   - The timeout (default 60 s) bounds each socket operation, and also the whole request as a
     deadline. When the deadline passes, the socket is shut down, so a server that sends a byte every
     59 s cannot hold a read or a page open (`deadline_exceeded`).
   - Credentials are the ones declared to the factory or, if none are declared, the `NEPTUNE_*`
     variables: `NEPTUNE_S3_ACCESS_KEY_ID`, `NEPTUNE_S3_SECRET_ACCESS_KEY`, `NEPTUNE_S3_SESSION_TOKEN`,
     `NEPTUNE_GCS_ACCESS_TOKEN`, `NEPTUNE_AZURE_SAS_TOKEN`. The ambient `AWS_*`, `GOOGLE_*` and
     `AZURE_*` variables, credential files and instance metadata are never read, so an operator's own
     credentials, usually writable, are never used by accident.
   - Anonymous access is declared (`anonymous: true`). It is refused if credentials are also present.
   - An Azure SAS token must grant `sp` within `r` and `l` and carry a signature, or it is refused. S3
     keys and GCS tokens cannot be checked offline, so the operator issues them read-only (an S3 policy
     of `s3:GetObject*` and `s3:ListBucket*`, GCS `roles/storage.objectViewer`).
   - Credentials never appear in a repr, a finding or the transform.
   - When Platform X2 secrets land, a superseding ADR moves the credential source there. The factory
     signature does not change.
7. **Declared, closed options.** `endpoint` with `store` (both or neither), `anonymous`, `max_objects`,
   `max_listing_bytes`, `page_size` (1 to 1,000), `timeout`; S3 adds `region`, `addressing` (`virtual` by default on AWS, `path` for a declared
   endpoint) and `versions`. An unknown option is refused. A dotted bucket over https must be
   path-style, because the wildcard certificate does not cover a dotted host.
8. **Hostile input.**
   - Keys are never normalised: no Unicode normalisation, no collapsing `//`, no resolving `..` or `.`,
     no percent-decoding of the prefix. NFC and NFD spellings are two objects. Keys are sent verbatim
     on the request path (`http.client` does not normalise), percent-encoded once.
   - Every read is pinned to the listed revision (version id, generation or `If-Match`), and a
     `Content-Range` must cover exactly the bytes asked for. An intermediary that normalised `a/../b` to
     `b` therefore fails the read (`object_gone` or `object_changed`). It never serves a neighbour's
     bytes. The test that reads every hostile key back gets each key's own bytes.
   - Every number a store states (a size, a `Content-Range`, a `Content-Length`) must be 1 to 19 ASCII
     digits before it is read as one. `²`, a sign or 5,000 digits make that one read `read_failed`,
     with a finding for that object only, never a bare `ValueError`. A range or length that disagrees
     with the listing is `object_changed`.
   - A key that is not UTF-8 (`key_not_utf8`) is not used. Neither is one longer than 1,024 bytes
     (`key_too_long`), one outside the requested prefix (`key_outside_prefix`), or one with no usable
     token or size (`revision_invalid`, `size_invalid`). Each reason is one finding citing at most ten
     keys as hex and counting all of them.
   - A listing page is decoded as UTF-8 first (another encoding is `response_invalid`; UTF-16 would
     hide `<!ENTITY` from a byte search). A page that declares a document type or entity is refused
     before it is parsed, and the parser is given the decoded text, so it ignores any encoding the page
     declares. No entity is ever expanded. A page body is limited to 32 MiB.
   - A store that ignores `Range` is accepted only from offset 0 and only with a stated length, which
     must equal the listed size. Only the bytes asked for are read, and the connection is dropped. GCS
     reads send `Accept-Encoding: gzip`, so a gzip-encoded object is served as stored, with ranges,
     not decompressed by GCS's transcoding. A transformed body without a length is `read_failed`.
   - An Azure SAS token is split by hand: `parse_qsl` would turn a `+` in an unescaped base64
     signature into a space.
9. **Lazy range reads.** `open()` fetches a 64 KiB window on the first read and doubles the window up to
   8 MiB while reads are sequential. A seek resets it, and no request asks for more than 8 MiB. A
   probe's head therefore costs one small request. The stream is buffered, so `read(n)` returns `n`
   bytes unless the object ends first. A kept-alive connection that the server closed while idle is
   reopened once (`GET` is idempotent); a response whose body is not read drops its connection.
   `reader(location, artifact)` gives an adapter a `SourceReader` that fetches whole artifact chunks with
   one ranged GET each, checks each against `artifact.chunks` before serving a byte, and keeps the last
   four. Adapters never download whole objects. The compiler hashes a new or changed object once, by
   streaming it, to fingerprint it.
   A failed read is a finding (`short_read`, `object_changed`, `object_gone`, `redirect_refused`,
   `read_failed`) and raises `ObjectReadError`, an `OSError`, so the caller quarantines that object and
   nothing else.
10. **No new dependencies.** The connector uses `http.client`, `ssl`, `hmac`, `hashlib`,
    `xml.etree.ElementTree` and `json`, and adds nothing to `uv.lock`.
    - SigV4 is about 60 lines. It is tested against AWS's two published S3 examples, and against four
      botocore 1.43.107 signatures over hostile paths (`a/../b`, `a//b`, NFD, session token, a marker
      with `+`).
    - Tests use an in-process fake of all three wire formats (`tests/deploy_object_store_fake.py`). The
      fake serves real HTTP on a loopback port and has knobs for redirects, truncation, ignored ranges,
      DOCTYPEs, loops, shuffled pages and injected entries.
    - `test_deploy_object_store_live.py` runs against any S3-compatible server named by
      `NEPTUNE_TEST_S3_ENDPOINT` (MinIO, moto server, Ceph) and is skipped otherwise. It passed against
      moto server 5.2.3 for this ADR.
    - The 10⁵-key listing and discovery run in about 7 s locally, server included. It is held to 60 s, 100 requests
      and 256 MiB peak.

## Alternatives considered

- **boto3, google-cloud-storage, azure-storage-blob.** Each brings its own HTTP stack, credential chain
  and retry policy: dozens of packages between them (botocore alone is about 15 MB). Each SDK's default
  credential chain reads ambient credentials and instance metadata, which §6 forbids, and each follows
  redirects or region hints in ways this code would have to switch off one by one. Every workspace member's
  CI job would also re-run on any change to them in `uv.lock`. Lost.
- **obstore / object_store (one Rust client for all three).** Small and fast, but its `Path` type
  refuses keys with empty, `.` or `..` segments. It cannot represent the hostile keys that real buckets
  hold, so they would vanish from the listing with nothing to say why. Lost.
- **fsspec (s3fs, gcsfs, adlfs).** A filesystem layer over the same SDKs. It treats keys as paths
  (stripping and joining slashes) and keeps a listings cache, so neither keys nor listings are taken
  as the store states them. Lost.
- **moto as a CI test dependency.** A truer S3 than the fake, but `moto[server]` installs 61 packages,
  and every member's job re-runs when the workspace lock changes. It runs as the live-test server instead (§10).
- **The version id in `object_id`.** Every version would then be its own location, so the ledger would
  never chain one object's revisions, and a changed object would look like a new object plus one that
  never goes away. Lost.
- **The endpoint URL in identity.** It would separate two sites' `logs` buckets, but moving a store to
  a new host name, or reaching it through another address, would make every object new. A declared
  store name separates them and survives a move. Lost.
- **Following same-host redirects.** S3's `301 PermanentRedirect` points at another regional endpoint,
  not a safe target, and "same host" cannot be checked once DNS is involved. The finding tells the operator to declare the region.
  Lost.
- **Ambient credentials, as the SDKs do.** Convenient, but an operator's default AWS profile is usually
  allowed to write and delete. Lost.
- **Probing every object every run.** It makes incremental discovery a no-op and re-downloads a fleet's
  bucket nightly. Lost.

## Consequences

- Deploy publishes the factory signature (`docs/contracts.md`). The compiler can call it, but nothing in
  the compiler does yet. These compiler gaps are listed, not worked around:
  1. `neptune ingest` and the SDK refuse every non-`file` scheme (`UnsupportedError`). They do not route
     `s3://`, `gs://` or `az://` to a plugin Source.
  2. The scan and runtime accept only `LocalSource` and its entry types. Nothing fingerprints an
     `ObjectEntry`, treats `SkippedObject` as a skipped entry, quarantines one object on
     `ObjectReadError` (the scan catches only `SourceAccessError`), carries `discover().unchanged`
     revisions forward, records `gone` as `SourceAbsence`, or gives adapters `reader()`.
  3. The ledger keeps the first token it saw for identical bytes, so an object re-tokened over the same
     bytes (a re-upload, a copy) counts as changed, and is fetched and hashed again, on every run until
     its bytes change.
- An object that changes between listing and reading fails with `object_changed`. It is never read as a
  mix of two revisions.
- Connection reuse is per source and single-threaded. Parallel reads are a runtime decision for later.
- Revisit if a provider needs a call outside `list_page` and `get_range` (for example, a manifest-based
  inventory instead of listing), if X2 secrets land (§6), or if a store needs a redirect followed.
