# D2 gate: connector identity, read-only and local-only guarantees

- Date: 2026-10-03 · Issue: MVL-158 · Reviewed: Deploy ADRs 0006 to 0010, every `neptune.sources`
  entry point (`deploy_s3`, `deploy_gcs`, `deploy_azure_blob`, `deploy_foxglove`, `deploy_roboto`,
  `deploy_rerun`, `deploy_jira`, `deploy_servicenow`, `deploy_linear`, `deploy_gdrive`,
  `deploy_onedrive`, `deploy_confluence`, `deploy_rest`, `deploy_formant`, `deploy_open_rmf`), and
  `neptune_deploy.diagnostics`.
- Method: every cell below is an executed test, not a walkthrough. `tests/test_deploy_d2_gate.py`
  runs each guarantee over one rig per connector (`tests/deploy_d2_rigs.py`). Every networked
  connector is served by its existing fake behind **one hostile reverse proxy**
  (`tests/deploy_d2_proxy.py`). The proxy logs every request and applies the same five attacks to all
  of them. A client-side recorder of `http.client` proves that nothing bypassed the proxy. S3, GCS and
  Azure were also run against real emulators (`tests/test_deploy_d2_emulators.py`). Every connector
  has an env-gated live test (`tests/test_deploy_d2_live.py`). No live vendor tenant or credential
  was available. Linux 7.0, x86_64, Python 3.12.14.
- Outcome: all seven guarantees hold for every connector, once **two defects** were fixed (ADR
  0011 §2 and §3). B1 was in the transport every networked connector shares. B2 was Rerun asserting
  an absence it could not know. GCS and Azure passed their
  emulators and stay registered (ADR 0006 §2's condition is met). **Verdict: pass.** Tag `d2-gate`
  once this is merged. End-to-end compiler ingest through plugin Sources is a tracked dependency, not
  a gate condition (ADR 0011 §6).
- Harness: green at `c6cbb36` (after B1), on the default corpus and on
  `--corpus packages/neptune-deploy/tests/fixtures/archetypes/sources`:
  `contracts ok | compiler: real ok | ledger: stub ok | memory: stub ok | context: stub ok`.

## The guarantees

| # | Guarantee | How the gate shows it | Test |
|---|---|---|---|
| G1 | External identity survives a re-sync | Two syncs, each with its own server and port, and the second at page size 1, list the same `ExternalObjectRef`s. Against the first sync's ledger, the second has nothing new, changed or gone, and every object is unchanged. | `test_g1_…` |
| G2 | A changed object yields a new revision; the old one stays intact | The remote edits one object: a re-upload, a re-import or an edited ticket. `discover` names exactly that object as changed, at the same object id with a new token. After fingerprinting, the ledger holds a second revision that supersedes the first. Every earlier revision is unchanged, its artifact is still held, and every other object still has one revision. | `test_g2_…` |
| G3 | Nothing is written to the remote | A full session: list, fingerprint, ranged reads through `reader()`, `declared()`, `catalog()`, then a second sync after the edit. Every request the proxy saw is checked by method, route and body against the system's read-only surface. Every `http.client` request went to the proxy, so the log is complete. | `test_g3_…` |
| G4 | Local-only mode refuses the source | (a) A local-only workspace: the factory raises `LocalOnlyError`, and neither the proxy nor `http.client` saw a request. (b) Built online, then switched: `walk()` raises before any request. (c) Listed online, then switched: every read that needs the network raises `LocalOnlyError`, and nothing reaches the remote. | `test_g4_…` |
| G5 | Credentials never reach adapters | Every spelling of each secret is searched for: verbatim, percent-encoded, and as the base64 of an HTTP Basic pair. The search covers walk entries (Source refs), findings, the transform (its config and config hash), catalog and declared records (their provenance), relations, the cursor, `repr`, and the ledger's revisions, after a sync and a re-sync. No endpoint `host:port` is in any of them either. As a positive control, the secret did reach the API. It travelled only where allowed: never in a path, and never to a signed link (Foxglove `/blob/`, Roboto `/content/`, Graph `/dl/`). | `test_g5_…` |
| G6 | A hostile remote produces findings, not exceptions or hangs | Five attacks (§ Attacks) on every request: the listing returns, with findings, inside a hard bound, lists nothing, and reports nothing `missing` (what was not read is not known). The same five on byte reads only, after a clean listing: each read raises the source's `OSError` with a code, so the caller quarantines that object alone, and it adds a finding. A listing over its declared budget stops with the limit finding. Exception chains are searched for secrets. | `test_g6_…` |
| G7 | Determinism | Two clean syncs with different servers and ports give byte-identical output and ledgers. Each of the five attacks, run twice, gives identical findings. All fifteen connectors give the same digests under `PYTHONHASHSEED` 0 and 4242. | `test_g7_…` |

## Matrix

Each cell holds unless it says otherwise. "Fake + proxy" means the connector's in-process fake behind
the gate's proxy. "Emulator" means a real server implementation.

| Connector | G1 identity | G2 new revision | G3 read-only surface | G4 local-only | G5 credentials | G6 hostile | G7 determinism | Verified against |
|---|---|---|---|---|---|---|---|---|
| `deploy_s3` | holds | holds (`version:`) | `GET` only | holds | key id only in `Authorization`; secret key never sent | holds | holds | fake + proxy; **moto 5.2.3** |
| `deploy_gcs` | holds | holds (`generation:`) | `GET` only | holds | bearer in `Authorization` only | holds | holds | fake + proxy; **fake-gcs-server 1.56.1** |
| `deploy_azure_blob` | holds | holds (`version:` on the fake, `etag:` + `If-Match` on Azurite) | `GET` only | holds | SAS in the query of each request, by design (ADR 0006 §6); never in an output | holds | holds | fake + proxy; **Azurite 3.37.0** |
| `deploy_foxglove` | holds | holds (re-import: `import:` token) | `GET` of index, devices and topics; `POST /v1/data/stream` only; `GET` of the link | holds; reads refused too | key never sent to a link | holds | holds | fake + proxy |
| `deploy_roboto` | holds | holds (`version:<file>:<n>`) | `GET`; `POST` of the files query only | holds; reads refused too | token never sent to a signed URL | holds | holds | fake + proxy |
| `deploy_rerun` | holds (objects keep `deploy_s3` identity) | holds | `GET` only (storage) | holds | holds | holds, after B2 | holds | fake + proxy |
| `deploy_jira` | holds (numeric id, never the key) | holds (`updated:`) | `GET` only | holds; attachment reads refused | Basic pair in `Authorization` only | holds | holds | fake + proxy |
| `deploy_servicenow` | holds (`sys_id`) | holds (`mod_count:…@…`) | `GET` only | holds | holds | holds | holds | fake + proxy |
| `deploy_linear` | holds (uuid) | holds | `POST /graphql` of query documents only | holds | holds | listing: holds; reads: none of its own | holds | fake + proxy |
| `deploy_gdrive` | holds | holds (`version:`) | `GET` only | holds | holds | holds | holds | fake + proxy |
| `deploy_onedrive` | holds (driveItem id) | holds (`ctag:`) | `GET`; one followed `302` without `Authorization` | holds | bearer never sent to the download host | holds; a redirect loop on download stops after one hop | holds | fake + proxy |
| `deploy_confluence` | holds | holds (`version:`) | `GET` only | holds | holds | listing: holds; reads: none of its own | holds | fake + proxy |
| `deploy_rest` | holds | holds (`updated:`) | `GET` only | holds | `Session-Token` header only | holds | holds | fake + proxy |
| `deploy_formant` | holds | holds (`records:<sha256>` of the part) | `POST` to the five query routes only; no `GET` | holds | holds | listing: holds (each part fails alone); reads: none of its own | holds | fake + proxy |
| `deploy_open_rmf` | holds | holds (`records:<sha256>`) | local: no byte, timestamp, journal or WAL file of the directory changes; no socket | accepted, by design: it is local, never asks the network, and opens no socket under a local-only workspace | takes no credentials; passing any is refused, and the value is not echoed | the file and SQLite attacks are MVL-156's tests, cited below | holds | local fixtures |
| ROS 2 diagnostics (mapper) | records cite only bytes the base package's ledger holds; the ledger is carried unchanged | a new mapping or export is new lineage; the base package is never changed (cited) | no file written, no socket | no network at all | takes no credentials | malformed mappings and statuses are findings (cited) | byte-identical | committed packages |

G7 holds for every networked connector after B1. Before it, a timed-out request's finding
depended on thread scheduling.

Cited tests (`test_every_cited_test_exists` fails if one disappears):

- Open-RMF, hostile input (MVL-156, `test_deploy_fleet_ops_rmf_hostile.py`): path traversal,
  symlinks, FIFO and directory, file size, damaged JSON Lines, not-a-database and truncated, a file
  swapped while it is read, endless views, work limit, views, virtual tables and triggers never read,
  the cell limit (`cell_limit`), the byte budget (`byte_limit`, one per database), and a database that
  cannot be written through its URI.
- ROS 2 diagnostics (`test_deploy_fleet_ops_diagnostics.py`): unmapped codes, bag streams not opened,
  nothing to map, wrong and oversized mapping files, strict JSON, `level_invalid`, new lineage per
  mapping, byte-identical output with the base untouched.

## Attacks

All five come from the same proxy code for every connector (timeout 0.5 s, hard bound 60 s).

| Attack | What the proxy does | Listing (every request attacked) | Byte read (reads only attacked) |
|---|---|---|---|
| Redirect loop | `302` to the request's own URL | `redirect_refused`, never followed (Formant: `part_failed`, cause `redirect_refused`, per part) | `redirect_refused`. OneDrive's one allowed download hop meets the loop and stops at its second `302`. |
| Truncated body | The upstream's headers and full `Content-Length`, half the body, then close | `listing_failed` / `part_failed`, cause `short_read` | `short_read` |
| Slow | No byte for 3 s | `listing_failed` / `part_failed`, cause `deadline_exceeded`, at the 0.5 s timeout | `read_failed` (cause `deadline_exceeded`) |
| Trickle | Headers, then one byte per 0.2 s | `listing_failed` / `part_failed`, cause `deadline_exceeded`: the deadline shuts the socket, however steadily bytes arrive | `read_failed` (cause `deadline_exceeded`), for every connector that reads bytes |
| Oversized | `200` and 40 MiB of JSON-like bytes | object stores, Foxglove, Roboto, Rerun, Formant: cause `response_too_large`; record systems: `response_invalid` (both at the page limit: 8 MiB Roboto, 32 MiB the others) | `object_changed`: a `200` whose length is not the listed size is refused before its body |
| Over budget (declared) | The connector's own `max_objects`, `max_recordings` or `max_records` set below the listing | `listing_limit` (`part_limit` for Formant), with an incomplete listing | n/a |

Under every listing attack, Rerun also reports `object_unresolved` for each object it could not
resolve. Before B2 it reported `object_not_found`. Linear, Confluence and Formant fetch no bytes of
their own: their content is in the listing.

## Findings

### Defects found and fixed here (ADR 0011)

- **B1. One timed-out request gave two finding ids, depending on scheduling.** The transport every
  networked connector shares gives each socket operation the same timeout as the request's deadline.
  The deadline's timer starts first. It decided the cause from whether that timer's thread had run
  yet. Under CPU load the socket's own timeout won the race: 10 of 25 runs against one silent server
  reported `cause: transport_failed` and 15 reported `deadline_exceeded`. Finding ids are derived from
  details, so the same server gave two different finding ids, which breaks G7. Found by running the
  slow attack repeatedly under contention. A `TimeoutError` is now `DeadlineExceeded` (it is: the
  deadline's clock started first). 25 of 25 now agree under the same load. Regression test:
  `test_a_socket_timeout_is_deadline_exceeded_even_when_the_timer_thread_is_late` holds the timer back
  by 30 s, so the late-timer case happens every run, for a server that sends nothing and for one that
  stalls mid-body. It fails without the fix.

- **B2. Rerun asserted an object missing when its storage listing failed** (ADR 0011 §3). Rerun
  resolves each storage URL with one exact-key listing. It reported `object_not_found` (category
  `missing`) whenever the key was not listed, even when the listing was refused, invalid or limited.
  That turned "not known" into "absent", against package non-negotiable 3, and the old
  `test_a_redirect_is_refused_and_never_followed` asserted it. Found by tabulating every
  connector's findings under every attack. A key whose listing did not complete is now
  `object_unresolved` (category `failed`), as is a key the store listed but the source could not
  use. Only a listing that shows the key absent is `object_not_found`: a complete one, or a probe
  stopped at its limit after a key that sorts past it. Regression tests: `test_an_object_whose_listing_failed_is_unresolved_never_not_found`
  (a redirect and a refused page; fails without the fix), and the gate's check that no attacked
  listing, of any connector, reports anything `missing`.

The gate's own changes were reviewed with `/code-review` at high effort. Confirmed and fixed:

- B1 had a second half. A deadline timer starved past its request's end could still shut down the
  *next* request's socket, because `Timer.cancel` cannot stop a running timer thread. A deadline now
  aborts only while its request is running (a lock and a flag). Regression:
  `test_a_deadline_that_fires_after_its_request_ended_touches_nothing`.
- B2 had two edges. A key the store listed but the source could not use (`key_duplicated`) still read
  `object_not_found`. And an absent key with more than 64 siblings read `object_unresolved`, though the
  listing showed it absent. Regressions: `test_a_key_listed_but_unusable_is_unresolved_not_missing`,
  `test_a_key_absent_among_many_siblings_is_not_found_even_past_the_probe_limit`.
- The gate's namespace check allowed `deploy_s3.*` findings from every connector; it now allows them
  for Rerun only. The live test now searches for every spelling of a secret, Basic pairs included,
  as the gate does. The slow and trickle attacks and the hash-seed run are marked `slow`.
- Not taken: a claim that `Response.exact()` reports `ShortRead` after the deadline. It routes through
  the same `deadline.error`, so it reports `DeadlineExceeded`.

### Decisions (ADR 0011)

- The gate is one parametrised module over rigs behind one hostile proxy. A new connector joins by
  adding a rig.
- GCS and Azure passed their emulators and stay registered. Azurite has no blob versioning, so Azure's
  `version:` path is verified on the in-process fake only.
- Vendor connectors that have not met their service stay registered as "fake-verified". Each has an
  env-gated live test and is withdrawn if it fails one.
- Connector versions stay `0.1.0` through `d2-gate`. From the tag on, any change to a connector's
  output bumps it.

### Tracked dependency: end-to-end ingest through plugin Sources

`neptune ingest s3://…` (and `gs://`, `az://`, `foxglove://`, …) on a versioned bucket is the
compiler's work, not this package's. That covers non-file schemes in `neptune ingest` and the SDK,
connector entries in scan and runtime (`ObjectEntry`, `SkippedObject`, `ObjectReadError`
quarantine, `discover().unchanged` carried forward, `gone` as `SourceAbsence`, `reader()` for
adapters), and a sink for `declared()`. These are ADR 0006's gaps 1 and 2 and ADR 0007's gap 4. The
work is **MVL-45, compiler PR #103, open**. The gate proves the guarantees at the Source boundary and
does not depend on that PR. Members may not run ingestion (the merge-freshness rule), so the
end-to-end `neptune ingest s3://…` without `--connector` is verified by the compiler's
`tests/integration/test_connector_deploy_s3_ingest.py` once #103 merges. For that dispatch, the three
object-store factories now declare `schemes` (`("s3",)`, `("gs",)`, `("az",)`) as a plain attribute
(compiler ADR 0067). `test_the_object_store_factories_declare_their_uri_scheme_and_no_other_connector_does`
checks it, and that no other connector declares one: they are chosen by `--connector`. Nothing here
imports or depends on #103 (ADR 0011 §6). Also
open on the compiler: ADR 0006's gap 3 (a re-tokened object with identical bytes is re-fetched on
every run).

### Live verification still needed

Each connector's live run is `NEPTUNE_TEST_LIVE_<CONNECTOR>_URL=… pytest tests/test_deploy_d2_live.py`
with read-only credentials in its `NEPTUNE_*` variables. It checks identity, read-only methods and
routes, local-only refusal, no secret in any output, and determinism, on the real service. It
reads at most three objects whole. What only that run can confirm:

| Connector | Assumptions that need a live run | Status |
|---|---|---|
| `deploy_s3` | AWS itself: `ListObjectVersions` paging and delete markers at scale, the regional `301` (`redirect_refused`; the operator declares `region`), session tokens | emulator-verified (moto) |
| `deploy_gcs` | Real GCS: bearer scope `roles/storage.objectViewer`, `Accept-Encoding: gzip` serving gzip objects as stored, `generation` pinning | emulator-verified (fake-gcs-server) |
| `deploy_azure_blob` | A versioned account (`VersionId` in List Blobs, reads by `versionid`), an account vs service SAS, `If-Match` on real ETags | emulator-verified (Azurite, `etag:` path) |
| `deploy_foxglove` | The stream link honours `Range` (undocumented) and serves identical bytes across requests; the link's host (no redirect expected, `link_hosts` otherwise); `429` with `Retry-After`; `importedAt` moves on re-import | fake-verified |
| `deploy_roboto` | Signed URL hosts (`content_hosts`); `X-Roboto-Api-Version`; the files query's paging. The shapes are validated against `roboto` 0.58.0 models, not the service. | fake-verified |
| `deploy_rerun` | The export envelope against a real `segment_table()` and `schema()`; storage URL forms Rerun Hub writes | fake-verified (storage: as S3, GCS, Azure) |
| `deploy_jira` | `search/jql` `nextPageToken` paging and `isLast`; `attachment/content?redirect=false` answering bytes, not `303`; JQL minute precision in the user's zone | fake-verified |
| `deploy_servicenow` | `sys_audit_delete` readable by a read role; `sys_updated_on` precision; `X-Total-Count` against rows given | fake-verified |
| `deploy_linear` | A rate limit is `400` with `X-RateLimit-*-Remaining: 0` (the connector reads no body); `organization.urlKey` on every answer | fake-verified |
| `deploy_gdrive` | `403` with `rateLimitExceeded` / `userRateLimitExceeded`; `changes.getStartPageToken` before the snapshot | fake-verified |
| `deploy_onedrive` | Graph's `302` from `/content` to a pre-authenticated host within the default `download_hosts`; `cTag` stability over renames; `503` and `509` throttling | fake-verified |
| `deploy_confluence` | v2 `pages` cursor paging; `version.number` on in-place edits | fake-verified |
| `deploy_rest` | Per vendor: a profile is the operator's declaration, and no vendor preset ships until a tenant validates it | fake-verified |
| `deploy_formant` | The five `POST /v1/admin/<route>/query` routes, `continuationToken` paging, and item shapes. Written from the public docs, not validated against a client model. | fake-verified |
| `deploy_open_rmf` | The api-server's SQLite schema and JSON log shapes from a running RMF deployment (local, so its "live" run is a real deployment's directory) | fixture-verified |
| ROS 2 diagnostics | Message definitions checked against ROS 2 Humble with `rosbags`. Bag payloads wait for the compiler to decode `DiagnosticArray`. | definition-verified |

### Accepted limitations

- **L1.** The fakes verify the documented wire formats, not the services. A vendor connector's guarantees
  hold at the boundary the gate tests. Where a service differs from its documentation, only its live
  run shows it.
- **L2.** The gate module takes about three minutes, most of it the deliberate timeouts of the slow and
  trickle attacks.
- **L3.** The emulator and live tests are skipped in CI, by design: CI has no network and no tenants.

### Open risks

- **R1.** Until PR #103 merges, nothing in the compiler calls these Sources, so a contract drift
  between a connector's entry types and the compiler's scan would surface only then.
- **R2.** A vendor that changes its API shape breaks its connector into findings (`response_invalid`,
  `record_invalid`), never into wrong records. Nothing tells the operator ahead of time, so the
  live tests are worth running on a schedule once credentials exist.
