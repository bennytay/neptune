# 0008 — Record systems are read-only Sources with revision identity and change feeds

- Status: Accepted
- Date: 2026-10-03
- Issue: MVL-157
- Builds on: ADR 0001 §1 and §4, ADR 0002, ADR 0006; root ADRs 0009 (source revisions), 0026 §6 (local-only), 0058 (plugin loader)

## Context

Deployment evidence lives in systems of record as much as in folders: an AMR fleet's incidents in Jira or
Linear, an arm cell's changes in ServiceNow, work orders in a CMMS, SOPs and risk assessments in Drive,
SharePoint or Confluence. D2 needs these as compiler Sources. A system of record is mutable (a ticket is
edited in place, a document revised, a record deleted), paginated, rate limited and hostile. Its objects
are the inputs the D1 adapters and mapper already read, so the connector adds identity and bytes, not
parsing. ADR 0006 settled the plugin surface and the network boundary for object stores. This ADR carries
them to record systems and records where those rules had to bend.

## Decision

1. **Plugin surface.** Deploy registers seven more `neptune.sources` entry points, one per system, named by
   its connector id: `deploy_jira`, `deploy_servicenow`, `deploy_linear`, `deploy_gdrive`, `deploy_onedrive`
   (OneDrive and SharePoint document libraries), `deploy_confluence`, `deploy_rest` (a declared CMMS or EAM
   API). Each value is a factory with ADR 0006 §1's signature, `factory(url, *, network, ledger=None,
   options=None, credentials=None, environ=None)`, returning a `RecordSource`. A `RecordSource` has the
   shape of the compiler's `Source` (`walk`, `open`) with its own entry types (`RecordEntry`,
   `SkippedRecord`, `ObjectReadError`), because members may not import `neptune.discovery`. It adds
   `listing()`, `discover(ledger)`, `reader(location, artifact)`, `relations()`, `findings()`, `cursor` and
   `ingest_options()`. Importing the package touches nothing.
2. **One mapping path.** A connector hands the compiler bytes in a shape an existing adapter reads, and the
   mapper of ADR 0002 makes the lifecycle records. Jira issues are the JSON `ticketing.jira-json` reads.
   ServiceNow and Linear issues are one-row CSVs in the columns of those systems' own exports, which
   `ticketing.servicenow-csv` and `ticketing.linear-csv` read. A `rest` profile maps a CMMS's JSON to the
   columns of `cmms.generic` (or any mapping file). Documents and ticket attachments (PDFs) are separate
   sources, read by the document adapters and templates of ADR 0003. A CSV snapshot's first row is its
   header, which the compiler reads only if told (root ADR 0042), so `ingest_options()` returns
   `{"tabular": {"csv_header": "first_row"}}`: the compiler cannot ask a plugin Source for it yet.
   Nothing here maps, ranks or interprets a value: a blank is a blank cell, not a fact.
3. **Identity.** An item is `ExternalObjectRef(<connector id>, <instance>/<scope>/<id>, <token>)`.
   - The instance is the host the URL names (a non-default port included), or `@<name>` when the operator
     declares `instance`, which a loopback host or a declared endpoint requires (as ADR 0006 §3 requires
     `store`). Linear shares one API host across every workspace, so its URL names the workspace and the
     instance is `@<workspace url key>`.
   - The id is the system's own stable id, never a name or key that can change: `issue/<numeric id>` (Jira,
     whose key changes when an issue moves), `table/<table>/<sys_id>`, `issue/<uuid>`, `file/<id>`,
     `item/<driveItem id>`, `page/<id>`, `record/<id>`. An attachment is `<parent id>/attachment/<id>`.
   - The token is the system's revision signal, `<kind>:<value>` as written: `updated:` (Jira, Linear),
     `mod_count:<n>@<updated>` (ServiceNow), `version:` (Drive, Confluence), `ctag:` (OneDrive), and for a
     profile `updated:`, `version:` or `etag:`. OneDrive uses the content tag, not the eTag: a rename moves
     the eTag, and identical bytes under a new token count as changed on every run (ADR 0006's consequence
     3). A file with no content tag uses `etag:`.
   - A document revised in place is therefore a new token at the same location. The ledger chains it as a
     new `SourceRevision` and keeps the old one. Identical bytes under a new token are no new revision.
     An attachment's id begins with its parent's, so its parent is also a declared relation
     (`relations()`, `attachment_of`, stated by the system), the compiler having nowhere for one yet.
4. **The systems.** Each system knows its wire format and nothing else; limits, order, findings, discovery
   and reads are the source's, so no two differ in policy.
   - **Jira Cloud**: enhanced JQL `search/jql` for a project, `attachment/content/{id}?redirect=false`.
     Change feed: `updated >=` the cursor, widened by one day, because JQL states times in the user's zone
     to the minute; the ledger's tokens discard what was seen. Deletions are found by absence.
   - **ServiceNow**: the Table API, Attachment API and `sys_audit_delete`. Change feed: `sys_updated_on`
     (UTC), inclusive, plus the deletions table (if denied the run is incomplete and the cursor stays).
   - **Linear**: GraphQL `issues` for one team, with `includeArchived`. Linear lists newest first, so a run
     that stopped part-way has seen the newest and not the oldest: only a run that read every page states a
     cursor. Every answer states its workspace (`organization.urlKey`) and a different one stops the listing.
     A trashed issue is a deletion; an archived one is not. Linear's `attachments` are references to URLs
     elsewhere, not files, and are not exported.
   - **Google Drive**: `files.list` (files with bytes and an `md5Checksum`; Google-native files have
     neither until exported and are `type_unsupported`) and `changes.list`, from a start token taken
     before the snapshot so nothing that changed during it is missed.
   - **OneDrive and SharePoint**: Microsoft Graph `root/delta`, one feed for snapshot and changes. Folders,
     packages and shortcuts are not files. Graph states an item more than once if it changed while paging,
     so Graph items are `later_wins`: the last statement is the item, where two different statements of an
     id are otherwise `record_duplicated` and neither is used. A download is checked against `sha256Hash`
     or `sha1Hash` if the file states one.
   - **Confluence Cloud**: v2 `pages` for a space, storage format, `version.number`. No change feed, so every
     run lists the space. Attachments are not exported: their download is a redirect to another host that
     the v2 API has no inline form of, and Confluence's is not on §6's allow-list.
   - **REST (CMMS, EAM)**: a declared, closed, checked JSON profile (`neptune-deploy.record-profile/1`):
     where the records are, the id and revision pointers, paging style (`cursor`, `offset`, `page`, `none`),
     the since parameter, the snapshot's columns, the attachments and the auth header. There is no vendor
     preset: a vendor's wire format becomes a preset only once a live tenant has validated it. A profile is
     data, never code, and part of the transform.
   SharePoint lists and pages, Jira and Linear comments, and Confluence attachments are not exported. Each
   needs its own decision.
5. **Listing, discovery and cursors.** The source reads every page once. Items are sorted by id. The
   listing, its findings, their ids and the transform do not depend on page size, page order or the wall
   clock. A listing is a `snapshot` (every record the scope holds) or `incremental` (what changed since a
   `since` cursor an earlier run's `cursor` returned). A cursor is stated only where the feed can resume
   from it: at the end of a feed that was read whole. It is a string `<connector id>/1:<payload>` of a
   per-system shape, and another connector's cursor is refused. The caller stores it after the job's
   receipt is durable. `discover(ledger)` sorts items into new, changed (a different token), unchanged
   (never fetched) and gone. Gone is: a complete snapshot's absences; ids the system said were deleted
   (and what hangs under them); and an attachment that its parent's own full list no longer holds. An item
   that was seen and could not be used is never called gone. A listing that stopped early is incomplete and
   asserts nothing gone.
6. **The network boundary and read-only credentials.** ADR 0006 §6 holds: the workspace is asked before the
   source is built and before every request; `GET` is the transport's method; a redirect is refused; an
   endpoint is https, or http to loopback only, with no user information; a timeout is also a deadline.
   A URL a system states (a Jira attachment's `content`, a Confluence `_links.next`) is never requested:
   requests are built from validated ids, so a hostile record cannot point the client, or its credentials,
   anywhere. Nothing sleeps or retries: a `429` is a finding with the system's integer `Retry-After`, and the
   listing stops and says where. Two narrow exceptions, each for a system whose API has no other form:
   - **GraphQL queries by `POST` (Linear).** `Api.graphql` sends a constant query document of the module
     with variables in the JSON body, and refuses before sending any document that is not a `query` or
     that contains `mutation` or `subscription`. No value enters a document. A query is a read, so a stale
     kept-alive connection is retried once, as `GET` is. Linear reports a rate limit as `400` with its state
     in `X-RateLimit-*` headers, never read from the body, so `400` with a remaining count of `0` is
     `rate_limited` and any other `400` is `listing_failed`.
   - **One download redirect (Microsoft Graph).** `/items/{id}/content` answers `302` to a short-lived,
     pre-authenticated URL on another host. It is followed once, only for a system that declares it, only for
     `302`, `303` or `307`, to a host the operator allows (`download_hosts`; default Microsoft's domains
     `sharepoint.com`, `.us`, `.de`, `.cn`, `1drv.com`, `microsoftpersonalcontent.com`, each matched as the
     domain or a subdomain, never a prefix), over https on port 443 (http to loopback), without user
     information or a fragment, with a plain path. The request carries no `Authorization` header, uses the
     same workspace gate, timeout and size check, and its own redirect is refused. Any other target is
     `redirect_refused` and no request is sent to it. A bare top-level domain is refused as an allowed
     host. The credential never goes anywhere but the declared endpoint.
   - **Credentials** are the ones declared to the factory or, if none are, the system's `NEPTUNE_*`
     variables: `NEPTUNE_JIRA_EMAIL`/`_API_TOKEN`/`_ACCESS_TOKEN`, `NEPTUNE_SERVICENOW_USERNAME`/`_PASSWORD`/`_ACCESS_TOKEN`,
     `NEPTUNE_LINEAR_API_KEY`/`_ACCESS_TOKEN`, `NEPTUNE_GDRIVE_ACCESS_TOKEN`, `NEPTUNE_ONEDRIVE_ACCESS_TOKEN`,
     `NEPTUNE_CONFLUENCE_EMAIL`/`_API_TOKEN`/`_ACCESS_TOKEN`, and `NEPTUNE_REST_API_KEY`/`_ACCESS_TOKEN`.
     Ambient variables, files and metadata are never read. A header value is printable text without edge
     space or a line break. Credentials are never in a repr, a finding, the transform or an exception
     chain. Whether a token is read-only cannot be checked offline, so the operator issues one (Jira
     `read:jira-work`, a ServiceNow user with read roles, Linear's read scope, Drive `drive.readonly`,
     Graph `Files.Read.All`).
7. **Declared, closed options.** `instance`, `scheme`, `since`, `page_size`, `timeout`, `max_records`
   (default 100,000), `max_attachment_bytes` (64 MiB), `max_snapshot_bytes` (256 MiB), `max_listing_bytes`
   (64 MiB), and per system: Jira `fields`, `api_version`, `attachments`; ServiceNow `fields`, `filter`,
   `attachments`; Drive `endpoint`, `mime_types`; OneDrive `endpoint`, `download_hosts`; Linear `endpoint`;
   REST `profile`. An unknown option is refused, never ignored. The transform holds what decided which
   records were seen (scope, limits, fields, profile), not the endpoint, credentials or cursor.
8. **Hostile input.** Every response is read as strict UTF-8 JSON, bounded (32 MiB), with duplicate keys,
   `NaN`, deep nesting and absurd numbers refused, and a number keeps the text the system wrote. A
   compressed body is refused: a decompression bomb needs a decompressor. A page that says more is coming
   and names none, a cursor or page seen before, a cursor over 4 KiB, a page body over the limit, a
   truncated body and a trickling server each stop the listing with a finding and a deterministic code. An
   id outside the system's documented form is not used (`id_invalid`), as is one with no usable revision or
   field (`record_invalid`), no deterministic text (`record_unrepresentable`), no size (`size_invalid`), over
   the limit (`record_too_large`) or listed twice differently (`record_duplicated`). Each reason is one
   finding citing at most ten ids as hex and counting all of them; a huge unusable id is held as a prefix,
   its length and its sha256. Attachment names are hints, never paths. Findings carry codes, counts and
   statuses, never error text, URLs or credentials. Partial pages (`incompleteSearch`, a table that counts
   more rows than it gives) are `listing_partial`, and a rate limit is `rate_limited`.
   A read is exactly the listed size, checked against the system's own checksum if it states one
   (`md5Checksum`, `sha1Hash`, `sha256Hash`); another length is `object_changed` (or `short_read`) and
   raises `ObjectReadError`, an `OSError`, so the caller quarantines that item and nothing else.
9. **Tests.** CI has no network and no tenants. Each system has an in-process fake that serves real HTTP on
   a loopback port from recorded response shapes (`tests/fixtures/records/*.json`) and can be edited
   between syncs, plus injected 429s, 5xx, redirects, truncation, compression, trickling and any bytes
   (`tests/deploy_records_fake.py`, `deploy_records_fake_graph.py`). Per connector: revision identity, an
   edit in place, a ticket with three attachments (Jira), a document revised in place (Drive, OneDrive,
   Confluence), the change feed and deletions, any page size giving one listing, and determinism. For every
   system: rate limits, partial pages, redirects, hostile ids and bodies, and a check that only `GET` is
   sent (and for Linear only `POST` of queries) and no secret appears anywhere. An integration test runs
   Jira, ServiceNow, Linear and REST snapshots through `neptune ingest` and the existing presets. These
   fakes verify what the documented wire formats say, not a live tenant. A connector counts as verified
   when the D2 gate runs it against a sandbox tenant, and is withdrawn until it passes if it fails there.
10. **No new dependencies.** `http.client`, `ssl`, `json`, `csv`, `hashlib` and `urllib.parse`; nothing is
    added to `uv.lock`. The record transport extends ADR 0006's `Transport` and shares its deadline,
    gate and error types.

## Alternatives considered

- **Vendor SDKs (`jira`, `servicenow`, `google-api-python-client`, `msgraph-sdk`, `linear-api`).** Each
  brings its own HTTP stack, credential chain, retries and redirect behaviour, and dozens of packages.
  Each would have to be switched off, one setting at a time, from reading ambient credentials, following
  redirects and retrying. Lost, for the reasons ADR 0006 gave.
- **A vendor-neutral CMMS preset for Maximo, UpKeep, Fiix.** None has been validated against a live tenant.
  A profile is declared by the operator and checked; a preset is added when one has been run. Lost for now.
- **Following redirects generally, or any host Microsoft names.** The redirect is the system's statement.
  Following any host sends a request, and with it the credential, wherever the server says. The allow-list
  keeps the exception to one download hop without credentials. Lost.
- **Skipping OneDrive and SharePoint because their bytes sit behind a redirect.** The issue asks for
  them, and Microsoft documents `/content` as the download form. Lost.
- **Linear through its CSV export, or an operator's own REST relay.** An export is a manual step and has no
  change feed. A relay is the `rest` profile, which a Linear user can still use. Lost as the only way.
- **Allowing `POST` generally in the record transport.** It would remove the one structural guarantee that
  a connector cannot write. The query-only check on the document, and no value ever entering a document,
  keeps the guarantee for the one system that needs it. Lost.
- **A retry on `429`.** A retry is a decision about time, and no adapter or connector reads the wall clock.
  The listing stops and states where. Lost.
- **Linear and Jira cursors from page position.** Positions move when records change. The cursor is the
  highest revision value seen. Lost.
- **A document's revision history as separate items.** The ledger already chains revisions from the token
  over time. A system's own history API (Drive revisions, Confluence versions) would give revisions that
  were never ingested, with no way to say why. Lost: the old revision is kept because it was ingested.

## Consequences

- Deploy publishes the factories (`docs/contracts.md`). The compiler can call them (root ADR 0058) but
  nothing in it does yet. Compiler gaps, listed and not worked around: (1) `neptune ingest` and the SDK
  refuse every non-`file` scheme and do not route `jira://`, `linear://` and the rest to a plugin Source;
  (2) the scan and runtime accept only `LocalSource` entries, so nothing fingerprints a `RecordEntry`,
  treats `SkippedRecord` as skipped, quarantines one item on `ObjectReadError`, carries `discover().unchanged`
  forward, records `gone` as `SourceAbsence`, or gives adapters `reader()`; (3) nothing stores an
  attachment's parent link as a relation between sources; (4) a plugin Source cannot declare adapter options
  (`csv_header: first_row`), so the caller writes them to a manifest; (5) the ledger keeps the first token
  it saw for identical bytes, so a re-tokened object counts as changed until its bytes do.
- A cursor and a ledger are the caller's: the connector returns where to resume and never stores it.
- An item that changes between listing and read fails with `object_changed`, never as a mix of two
  revisions. A rate limit stops a run. It states a cursor where its feed can resume from the last page read (Jira,
  ServiceNow, Drive) and none where it cannot (Linear, newest first).
- A document deleted and restored under another id is a new source. Neptune does not merge identities.
- Revisit if a vendor's wire format is validated against a live tenant (a preset), if X2 secrets land
  (ADR 0006 §6 moves credentials there; the factory signature does not change), if the compiler gives
  plugin Sources a place for relations and options, or if a system needs a method outside §6's two
  exceptions.
