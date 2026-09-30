# 0003 — Identity tiers and parser-upgrade survival

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-55

## Context

The design contract asks for several properties at once: re-ingesting identical bytes is idempotent, a changed
source creates a revision, renames do not create false new evidence, and "downstream references survive parser
upgrades". Taken together these contradict each other if there is one kind of id. A record produced by parser
v2 is *not* the same record as the one produced by v1: its content may differ. Giving both the same id would
either mutate history (violating non-negotiable 6) or make ids lie about what they identify.

A second trap is identity by content equality. Two robots can ship byte-identical URDFs, and two sites can have
identical CSV registers. Equal bytes are the same *evidence*, not the same *thing*.

The audit ranks identity as risk #2: if it is wrong, nothing joins across runs, revisions or parser versions.

## Decision

Neptune has **three identity tiers**. Their ids are never interchangeable and are never derived from one another
implicitly.

**Tier 1 — content id (identifies bytes).**
- `sha256:<64 lowercase hex>` over the complete source bytes, computed by streaming.
- Large files also carry per-chunk hashes for verification and partial-change diagnosis. The chunk size is
  recorded next to the hashes. Chunk hashes are metadata, not identity: changing the chunk size never changes
  the content id.
- Identity does not depend on path, filename, mtime or permissions. A path is an observation of *where* bytes
  were seen, recorded separately from the bytes. Renaming or moving identical bytes yields the same content id.
- An external object whose bytes have not been fetched is identified by
  `(connector id, external object id, revision token)`, where the revision token is an etag, version id or
  generation. When the bytes are fetched, the content id is added. The revision token never substitutes for it
  once bytes exist.
- A location (a local path relative to the ingest root, or an external object id) that later holds different
  bytes gets a new `SourceRevision` pointing at the new content id. Earlier revisions are never mutated or
  removed. MVL-2 defines `SourceArtifact` / `SourceRevision` and the dedup policy within these rules.

**Tier 2 — derived-record id (identifies one canonical record in one lineage).**
- The id is derived deterministically from the canonical JSON (ADR 0002) of:
  `(record kind, source content id, locator, adapter id, adapter version, config hash)`.
  The record kind is included so that one locator can yield records of different kinds without a collision.
  If an adapter emits several records of the same kind from one locator, the locator must be made finer, not
  disambiguated by a counter.
- Some records are not produced from a single source, for example a `Run` assembled by grouping. They fall
  outside this formula. MVL-1 defines their identity inputs, which must be equally deterministic and
  lineage-scoped. The input is the sorted set of constituent evidence plus the producing transform.
- The **config hash** is the sha256 of the canonical JSON of the *resolved* adapter config, with defaults filled
  in. Passing a default explicitly and omitting it produce the same hash.
- Tool and library versions are recorded on the `TransformRecord` (ADR 0006) but are **not** tier-2 inputs.
  Instead, **an adapter must bump its version whenever its output bytes may change.** That covers any change to
  its code, its pinned parser dependency, or its writer settings. The adapter version is the single lineage
  lever. A golden-file diff without an adapter version bump is a defect.
- Tier-2 ids are **lineage-scoped by design**. A new adapter version produces new ids *beside* the old records.
  Nothing is overwritten. Comparison across lineages is MVL-43.
- Records inside one package reference each other by tier-2 id, for example a `Stream` referencing its `Run`.
  **References that must outlive a lineage** (downstream systems, user annotations, alignment across ingest
  runs) must use tier-1 ids, tier-3 ids or `EvidenceRef`s (ADR 0006), never tier-2 ids.

**Tier 3 — logical id (identifies a real-world thing across sources).**
- A logical id is always a pair `(namespace, value)`. Examples: `("serial", "…")`, `("manifest", "spot-07")`,
  `("mac", "…")`.
- It is **declared, never inferred.** Valid sources are a serial number or identifier *stated* in a source (with
  provenance), a manifest entry, or an explicit alias.
- Content equality, filename similarity and co-location never produce a logical id or merge two of them.
- When sources disagree, or an id is missing, that stays explicit as `Knowledge` (ADR 0004) and an
  `IngestFinding`. Conservative cross-source resolution with an explicit unresolved state is MVL-35.

**Common rules.**
- Every id string carries its algorithm or namespace prefix, so a future hash algorithm is added alongside the
  current one rather than replacing it. Hashes are never truncated.
- Idempotence is structural. Identical bytes + adapter version + resolved config ⇒ identical tier-1 and tier-2
  ids ⇒ identical records.

## Alternatives considered

- **One id per record that is stable across parser versions**, for example keyed by source + locator only. The
  design contract's wording suggests this. It lost because v1 and v2 records would share an id while differing
  in content: either history is mutated or the id stops identifying content. Stability is provided by tier 1,
  tier 3 and `EvidenceRef`, which really are stable.
- **Random UUIDs for records.** They break determinism and idempotence, which are non-negotiable.
- **Path-based source identity.** Renames would create false new evidence, and one path over time would conflate
  different bytes.
- **Content-equality merging of logical entities.** Identical URDFs are not the same robot, and a silent merge
  corrupts every downstream join.
- **Tool versions as tier-2 inputs.** This is maximally conservative, but every lockfile refresh would re-lineage
  every record, including records whose bytes did not change. Making the adapter version the explicit lever
  keeps lineage meaningful and puts the obligation where the knowledge is.
- **BLAKE3 instead of sha256.** Faster, but sha256 is universal in object stores, container registries and
  tooling, and hashing is not the measured bottleneck. The algorithm prefix leaves room to add BLAKE3 later.

## Consequences

- Idempotence and parser lineage need no extra machinery.
- Downstream systems must be taught not to store tier-2 ids as durable references. `explain` (MVL-39) and the
  receipt surface `EvidenceRef`s for this reason.
- Adapter authors carry a versioning obligation, enforced by golden tests.
- Two packages built from the same bytes with different adapter versions share tier-1 ids but no tier-2 ids.
  Cross-version joins go through `EvidenceRef` and locator equality.
- Revisit if a consumer genuinely needs record-level identity that is stable across parser versions. That would
  be a new, explicitly *derived* mapping, not a change to tier 2.
