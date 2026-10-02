# 0067 — Connector Sources: URI dispatch, revisions carried by token, and a job spool

- Status: Accepted
- Date: 2026-10-03
- Issue: MVL-45
- Amends: ADR 0058 §1 (what a `neptune.sources` factory is given), ADR 0026 §3 and §6 (reads of
  bytes that are not on this host), ADR 0009 §4 (external revision tokens), ADR 0010 §4 (absences
  of external objects)

## Context

Neptune Deploy's object-store connectors (MVL-153, PR #96; Deploy ADR 0006) are `neptune.sources`
plugins. Their `ObjectStoreSource` lists a bucket prefix, classifies the listing against a
`SourceLedger`, and serves ranged reads pinned to the listed revision. The compiler could not use
them:

- `neptune ingest` and the SDK refused every scheme but `file`;
- scan and the runtime took only a `LocalSource` and its walk types;
- the ledger dropped every revision token after the first one seen over the same bytes, so an
  object re-uploaded unchanged was fetched and hashed again on every sync.

The sandbox (ADR 0030) gives adapter calls no network, so an adapter cannot read a connector
directly. Every job also probes every source again (ADR 0033 §1). For a local file that costs a
64 KiB read. For an object it would cost fetching the whole object, on every sync.

## Decision

1. **URI dispatch.** A `neptune.sources` factory may declare the schemes it reads as a `schemes`
   attribute: distinct, lowercase RFC 3986 schemes, never `file`. The loader admits the factory
   with those schemes (`PluginSource.schemes`), or refuses it (`refused`, reason
   `invalid_schemes`). A source that is a URI of another scheme than `file`, or a `RemoteSource`
   (SDK), goes to the connector named (`RemoteSource.connector`, `--connector`). Otherwise it goes
   to the one installed connector that declares the scheme. No connector, or two, is a
   `ConfigurationError` before anything runs, whatever the network setting. A URI with userinfo,
   a query or a fragment is refused (`InvalidSourceError`), because it would put a secret in the
   ledger and the envelope. After the network check (`require_network`), the factory is called
   once per call: `factory(uri, network=workspace, options=...)`. The options are the
   connector's own JSON object, passed as given (`--source-options`). Credentials are never
   options. A factory that raises anything (but `KeyboardInterrupt`) is a `ConfigurationError`
   naming the connector, and a `LocalOnlyError` is a `NetworkRefusedError`. What it returns must
   be an `ExternalSource` whose `connector_id` is the connector's id. `neptune ingest
   --allow-network` records that the workspace may use the network; the workspace remembers it.
2. **The protocol** (`neptune.discovery.external`) is structural. The compiler never imports a
   connector's types. A source has `connector_id`, `transform`, `discover(ledger)` (new, changed,
   unchanged, gone, complete), `open(location)` (a seekable stream over the listed revision; its
   failures are `OSError`s) and `findings()`. Deploy's `ObjectStoreSource` has this shape already.
   A connector that breaks it fails the job (`ExternalSourceError`), because its listing cannot be
   trusted: an object listed twice, another connector's location, a negative size, or a `gone`
   revision that is listed or is not the ledger's head.
3. **Classification is the compiler's.** Only the listing is taken from `discover`. Every listed
   object is classified again against the ledger of the URI. The ledger is keyed by the URI exactly
   as given (`Workspace.load_ledger(uri)`); a resolved path starts with `/`, so a URI key never
   meets a path key. An object whose token `SourceLedger.recognise` knows for the head
   revision's bytes, at the size those bytes were hashed at, is **carried forward**: observed
   under its listed token, never fetched, never hashed (event `source_recognised`). Any other
   object is fetched once through `open`. The bytes are hashed as they are copied into the spool
   (§5), and at most one byte past the listed size is read. A fetch that fails, or whose size is
   not the listed size, leaves the object unobserved. It gets discovery's `unreadable` or
   `size_changed` finding (ADR 0029 codes, the object as subject) and an `entry_skipped` event,
   and nothing is asserted about it. The connector's own findings join the job's under its
   transform. That transform enters every package of a connector's source, so the package names
   the connector version that listed it.
4. **Revision tokens.** `SourceLedger` keeps, for each external revision, every token seen over its
   bytes besides the one its location names. `Observation.new_token` says when one is new. The
   workspace saves them in `ledgers/<key>/tokens.jsonl`: one canonical line per revision, written
   after `ledger.jsonl`. The ledger only grows, so a crash between the two writes only loses
   tokens, which costs one more fetch and never causes a wrong match. A token only matches the
   head revision: once an object has held other bytes, an older token proves nothing. Tokens never
   enter a content id or a record id. The package lists each object under the token it was read
   under this time (ADR 0035 §9).
5. **The spool.** A connector's bytes reach adapters through `ExternalReader`, and every chunk is
   hashed against the artifact before a byte of it is served. A read in this process
   (introspection, the output checks) fetches only the chunks it needs, through ranged reads. A
   sandboxed call needs a descriptor (`fileno`), so it gets the job's spooled copy. That copy is
   the one fetched at fingerprint time, or one fetched now and checked against the artifact. The
   spool is a scratch directory the job holds (`scratch_space(..., ingest_root=None)`) and removes
   when the job ends. A sweep removes it if the job dies. A copy is renamed into place only once
   its digest is known and its size is the listed size. Nothing fetched outlives the job.
6. **Absences.** A connector's `gone` revisions become `SourceAbsence` records, and only when the
   listing was `complete`. History is never deleted, and an incomplete listing asserts nothing.
7. **Kept probes.** A connector's object's probe is kept as a workspace derivative (recipe
   `neptune.runtime.probe/1`). Its key covers the content id, the size, the name hint, the probe
   engine's transform, every registered adapter's descriptor, and the plugin distributions. Only a
   probe whose one sandboxed call returned is kept. It is read back with the sniff it recorded
   (`source_probe_from_json(..., head=None)`), and everything else is checked as for a fresh
   reply. Local sources keep probing every job (ADR 0033).
8. **Local rules stay local.** A connector's source takes no manifest and no ignore patterns, since
   both name local paths: either is a configuration error. Its layout is empty, so grouping
   proposes no sessions for it. Its envelope `root` is the URI.

## Alternatives considered

- **Proxy reads from the sandbox to the parent over a pipe.** This would give lazy reads inside
  calls, but it adds a request loop to security-critical code (ADR 0030). New objects must be
  read whole to be hashed anyway, so the spool costs no extra transfer for them.
- **Trust the connector's classification.** Deploy's compares only the first token, which is the
  bug §4 fixes. One rule in the compiler serves every connector.
- **Make the token part of identity, or record tokens as a canonical record kind.** Tokens are
  observations about a store, not evidence. Putting them in records would change the frozen model
  (ADR 0023) for something only the incremental sync uses.
- **Cache probes for every source.** That changes the M2 gate's settled behaviour (a re-run
  re-probes local files) for a 64 KiB saving per file.
- **Keep the spool across jobs as a cache.** It would copy whole buckets into the workspace and
  break "sources are never copied here" (ADR 0026). Carried-forward objects need no bytes at all.
- **A scheme table in the compiler** (`s3` → `deploy_s3`). The compiler would have to know its
  plugins. Declared schemes keep it ignorant, and a clash is refused rather than resolved.

## Consequences

- `neptune ingest s3://bucket/prefix --connector deploy_s3 --source-options '{...}'` runs Deploy's
  connector end to end today. A plain `s3://…` needs Deploy's factories to declare `schemes`.
- A re-sync of an unchanged bucket fetches nothing, hashes nothing and calls no adapter. A
  re-uploaded object is hashed once under its new token and then recognised.
- A job needs local disk for the bytes it fetches (new or changed objects), until it ends. Very
  large first syncs should go prefix by prefix. Revisit with a bounded spool, or with spooling
  only the objects an adapter call needs, if a first sync outgrows a disk.
- `collect` drops kept probes, since no plan owns them. The next sync fetches each object once to
  probe it again.
- In a dry run, `inspect` of an unchanged object spools it, as a sandboxed call must.
- Introspection still reads the cited ranges of an unchanged object's streams, as chunk-verified
  ranged reads.
- Session grouping does not yet group a connector's keys. Revisit when a layout over object keys is
  wanted (it would be a derived reading of names, ADR 0036).
