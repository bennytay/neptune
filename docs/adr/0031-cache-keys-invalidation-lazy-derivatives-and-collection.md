# 0031 — The cache: chunk ids as keys, named invalidation rules, lazy derivatives, a report and collection

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-9
- Extends: ADR 0008 §5 (the runtime owns the cache), ADR 0024 §4 (chunk ids), ADR 0026 (the
  workspace), ADR 0028 (the job)
- Amends: ADR 0022 §1 (one more file under `volatile/`), ADR 0026 §1 (workspace format 2)

## Context

MVL-9 asks that large robotics datasets not be reprocessed on every run: a chunk abstraction,
cache keys of source identity + adapter version + transform config + chunk identity, incremental
recomputation, lazy materialisation of heavy derivatives, and explicit invalidation rules. Its
acceptance: re-ingesting unchanged 100 GB-scale sources does near-zero duplicate transform work,
and parser or config changes invalidate only the derivatives they affect.

Most of the mechanism already existed. A chunk id (ADR 0024 §4) hashes the source's content id,
the transform (adapter id, version, resolved config, libraries) and the chunk's context, so equal
ids mean equal output. The workspace (ADR 0026) keeps each source's plan by (source, transform)
and each chunk's output by chunk id, and the job (ADR 0028) skips committed chunks. What was
missing:

- **Assembly was not incremental.** Every job re-read every committed run to check the
  cross-chunk laws and merged every stream's runs into a new series file, even when nothing had
  changed: for a large recording, the decode, sort and encode of every row, twice.
- **A miss said nothing.** A job could not say why it recomputed a chunk, so "only affected
  derivatives" could not be checked, and a surprising rerun could not be explained.
- **The workspace only grew.** ADRs 0026 and 0028 left collection to MVL-9.

ADR 0003 bounds the design: tier-2 ids are lineage-scoped, so records made under one transform can
never stand for another's. A new adapter version or config therefore must recompute every chunk
it applies to; "only affected" means nothing outside that transform's chunks and their derivatives.

## Decision

1. **The cache key of a chunk is its id; nothing else is consulted.** The chunk id covers the
   source's content id (tier 1, so a rename or a second root holding the same bytes hits), the
   transform's adapter id, version, config hash, libraries and upstream, and the context `plan`
   gave the chunk (its identity within the source). `cost` is not in it. No clock, file time,
   path, flag or host enters a cache decision. Libraries are in the key although they are not
   tier-2 inputs (ADR 0003): a library change recomputes, conservatively, and yields the same
   records whenever the adapter's version did not need to change.
2. **What is cached, and by what.**

   | Kept | Key | Built by |
   |---|---|---|
   | a plan | (source content id, transform id) | the adapter's `plan` |
   | a chunk's output | chunk id | the adapter's `ingest`, checked and committed |
   | a source's verdict on the cross-chunk laws | `neptune.runtime.admission/1` over the plan's chunk ids and the runtime's version | the job (ADR 0028 §5) |
   | a stream's series file | `neptune.store.series/1` over the chunk ids whose runs it merges and `SERIES_SETTINGS` | the store's merge (ADR 0025) |

   A derivative's key (`DerivativeKey`: recipe and version, canonical-JSON inputs, and the
   (source, transform) pairs it reads from) hashes to `drv:sha256:<hex>`. A recipe's version
   changes whenever the same inputs would give other bytes; the runtime's version, which ADR
   0028 §4 already bumps when its findings change, is an input of the verdict, so new laws
   recompute every verdict.
3. **Invalidation is a rule, and every miss names the first one that holds.** For a source's
   plan, in order:
   - `planned` (hit): the plan under this transform is kept;
   - `transform_changed`: this adapter planned the source under another transform; `changed`
     names the parts that differ (`adapter_version`, `config`, `libraries`, `upstream`) from the
     kept transform with the fewest differences, then the least id, which `previous` names;
   - `adapter_changed`: only other adapters planned it (a new adapter, or selection changed);
   - `source_changed`: a location now holds these bytes in place of others (`previous` names
     them), from the ledger's revision chain;
   - `source_new`: nothing is known of these bytes.

   Only the explanation reads the source's other plans, so one that cannot be read is passed
   over (the rule is made from what remains) and never fails the job, which plans the source
   either way.

   A chunk is `committed` (hit) if its output is kept; otherwise it misses with `not_committed`
   if its plan was kept (a job was interrupted, cancelled or failed on it) or with its plan's
   rule. A derivative is `held` (hit), `absent` (built now) or `corrupt` (kept but damaged:
   discarded and built again). Planning granularity is not a rule: the kept plan is reused
   whatever chunk size the adapter is constructed with, since chunking never changes output.
4. **Heavy derivatives are lazy.** A derivative is computed only when something reads it: a
   series file when a package that holds its stream is staged, a verdict when its source is
   assembled. A cancelled job, a quarantined source or a stream no package asks for costs no
   merge. Once built, it is kept in the workspace by key (`derivatives/<2 hex>/<62 hex>/`, its
   files and a `derivative.json` recording the key and each file's size and sha256), committed
   by one rename like a chunk, and every later reader copies it. A series file is copied into
   a package with its hash checked as it streams; a verdict is read with its hash checked. A
   derivative that fails either check, or whose record or files are damaged, is discarded and
   built again (`corrupt`), so a damaged cache costs time, never a wrong package. Source bytes
   stay references until read: sources are referenced, not copied (ADR 0022 §5), and adapters
   cite payloads by byte range rather than decoding them (ADR 0018 §4).
5. **Every job reports its cache use.** `CacheReport` lists each selected source's plan
   (cache, rule, `changed`, `previous`) and its chunks in plan order (cache, rule), every
   derivative read (cache, rule, recipe, owners), totals, and the job's calls to each adapter
   method (`probe`, `plan`, `ingest`; a retry is another call). It is deterministic: sorted, no
   clock, so two jobs over the same sources, adapters and workspace state report the same
   bytes. It is `JobOutcome.cache`, and a committed job writes it to
   `volatile/cache-report.json` beside the envelope, naming the receipt it accompanies. It is
   volatile (outside the manifest) for the same reason as the envelope: a rerun hits where the
   first run missed, and the package must not differ (ADR 0028 §2). Events add
   `derivative_reused` and `derivative_built`, and `source_planned` carries the plan's rule.
6. **Collection keeps what the current configuration can reuse.** `runtime.collect(workspace,
   registry, options)` keeps each plan whose transform is one the registry and options define
   and whose source some saved ledger holds at the head of a location, the chunks those plans
   list, and the derivatives all of whose owners are kept plans. Everything else goes:
   superseded versions and configs, sources gone from every root, orphans, damaged plans and
   derivatives (no job could reuse them), staging debris. Ledgers are history and are never
   collected; one that cannot be read stops collection, since without it nothing can be judged
   unreachable (`JobError` from `runtime.collect`). Each removal is one rename into
   `staging/` before deletion, so a reader never sees half an entry. Jobs hold a shared `flock`
   on the workspace's `lock` for their whole run; collection takes it exclusively without
   waiting and is refused while any job runs, so it can never remove what a job is using.
7. **Workspace format 2.** Plans are filed by source, then transform
   (`plans/<2 hex>/<62 hex>/<transform hex>.json`), so a miss can list the transforms a source
   was planned under; `derivatives/` and `lock` are added. Opening a format-1 workspace moves
   each plan under its source (one rename each) and then records format 2, so an upgrade killed
   midway finishes on the next open and ledger history is kept; a plan another opener moved
   first is passed over. A format-1 plan that cannot be read is left where it is: no job can
   reuse it, so it never stops a workspace from opening. A job of the previous version still
   running during the upgrade may save a plan at the old path afterwards; `plans()` passes over
   anything at an old plan path, and collection settles each one before judging plans: moved
   under its source if readable and not already kept, removed otherwise. Jobs of the previous
   version hold no `lock`, so collect only once they have finished.

## Alternatives considered

- **A separate cache index beside the workspace** (a key → output table). It would duplicate
  what the workspace already keys by chunk id, and two stores of one truth can disagree after a
  kill. The workspace is the cache.
- **Reusing chunks across adapter versions** when a new version declares its output unchanged.
  Tier-2 ids embed the version (ADR 0003), so v2 records are new records whatever their content;
  reuse would mean rewriting ids, which is new lineage built from old, not a cache hit. Cross-
  version comparison is MVL-43's.
- **Skipping the fingerprint hash when size, mtime and inode are unchanged.** The only remaining
  O(bytes) cost of a re-ingest (3.6 s for 4 GiB in the acceptance test). Rejected: a hit must be
  proven by content identity; metadata can lie (a restored backup, a coarse clock, a hostile
  writer), and a wrong hit is a package that cites bytes it never read. An explicit, recorded
  opt-in may come with M9's storage tiers (MVL-48).
- **Putting the cache report in the receipt core.** The core is a function of the records (ADR
  0022 §3), and a rerun's hits would change the package id, breaking resume's byte-identity.
- **Adding the report to `ReceiptEnvelope`.** The envelope is a model document; a field is a
  schema version bump (ADR 0023 §1) for a document only the runtime writes and reads. A sibling
  volatile file costs nothing and leaves the model frozen.
- **Hard-linking series files from the workspace into packages.** No copy, but the two names
  share an inode, so editing either changes both. A checked copy keeps packages independent.
- **Caching the merged series file inside the package only** (merge into the package, keep
  nothing). The next package would merge again; the merge is the dominant cost after parsing.
- **Collecting by age or size (LRU).** Needs wall-clock or access times, and evicts what the
  current configuration will ask for next. Reachability from the current transforms and ledgers
  is deterministic and explains itself.
- **Collection waiting for running jobs.** A long job would block it indefinitely; refusing at
  once and saying why is simpler and safe.

## Consequences

- An unchanged source costs its fingerprint hash, a 64 KiB probe, and copying its kept series
  files into the new package; no `plan`, no `ingest`, no merge, no cross-chunk check. Validating
  the new package still reads it whole (ADR 0028 §1).
- A config or version change of one adapter recomputes that adapter's chunks, verdicts and
  series, and nothing of any other adapter's; the old lineage stays kept until collected, so
  switching back is free.
- The workspace holds a stream's rows twice once its series file is built (the runs and the
  merged file). Collection removes both with their source's lineage; dropping runs whose series
  file is kept is a later optimisation.
- Every miss is explainable from the report alone, which MVL-11's CLI prints and MVL-43's
  replay checks can build on.
- Golden files are unchanged: the report is outside the manifest.
- Revisit if fingerprinting dominates re-ingest at real scale (the stat shortcut above, opt-in),
  if several collections or many jobs contend on one workspace (M9 queues), or if a derivative
  needs inputs beyond committed chunks.
