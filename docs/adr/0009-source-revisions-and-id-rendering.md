# 0009 — Source revisions, dedup policy and id rendering

- Status: Accepted
- Date: 2026-10-01
- Issue: MVL-2

## Context

ADR 0003 fixed the three identity tiers and left five things to MVL-2: the `SourceArtifact` / `SourceRevision`
shapes, the chunk size for per-chunk hashes, the dedup policy, how ids render as strings, and the canonical-JSON
implementation. Every one of these ends up inside ids or package bytes, so changing them later re-lineages
every record. They are recorded here rather than only in code.

## Decision

1. **Id rendering.** Every id carries its tier and algorithm; hashes are full-length lowercase hex.

   | Id | Rendering | Hash input |
   |---|---|---|
   | content id (tier 1), chunk hash | `sha256:<64 hex>` | the bytes |
   | config hash | `sha256:<64 hex>` | canonical JSON of the resolved config |
   | record id (tier 2) | `rec:sha256:<64 hex>` | canonical JSON of `{"inputs", "kind", "scheme"}` |
   | logical id (tier 3) | `{"namespace", "value"}` object, not a string | — |

   `scheme` is the constant `neptune.record-id/1`. It separates record-id hashing from any other use of
   sha256 and versions the derivation: changing it is a new ADR. `kind` is a lowercase token.
   Adapter-produced records use the ADR 0003 inputs (`adapter_id`, `adapter_version`, `config_hash`,
   `locator`, `source`). Records not produced by an adapter (a `SourceRevision`, later a `Run`) call the
   same function with their own documented inputs. Types: `ContentId`, `ConfigHash` and `RecordId` are
   distinct `NewType`s over `str`, so mypy rejects a config hash passed as a source id.

2. **Per-chunk hashes.** The chunk size is **8 MiB**, recorded on every `SourceArtifact`. Every non-empty
   source carries its chunk list: one entry for sources up to 8 MiB, which then equals the content id. An empty
   source has no chunks. Chunks are always hashed, not only for "large" files, so there is no size threshold
   to agree on.

3. **Shapes.**
   - `SourceArtifact(content_id, size, chunk_size, chunks)`: one per distinct byte string. Its id is its
     content id.
   - `SourceRevision(id, location, content_id, supersedes)`: one location seen holding one artifact's bytes.
     `supersedes` is `[]` for the first revision at a location and otherwise `[previous revision id]`. The id is
     a record id with kind `source_revision` over `(content_id, location, supersedes)`. Revisions form a hash
     chain, so history cannot be rewritten without changing every later id.
   - `location` is a tagged union: `{"kind": "local", "path"}` (relative to the ingest root, `/`-separated,
     no `.`/`..`/empty parts, as named on disk) or `{"kind": "external", "connector_id", "object_id",
     "revision_token"}`. Revisions of one external object chain on `(connector_id, object_id)`.
   - No wall-clock, mtime, host or absolute path appears in either record.

4. **Dedup and mutation policy.**
   - Same bytes anywhere ⇒ one `SourceArtifact`. Equal bytes are the same evidence, never the same logical thing.
   - Same location, same bytes as its latest revision ⇒ nothing new (idempotent).
   - Same location, different bytes ⇒ new revision superseding the previous one. Nothing is mutated or removed.
     Reverting to earlier bytes is also a new revision.
   - Rename or move ⇒ a first revision at the new location pointing at the existing artifact. No new artifact,
     so every tier-2 record keyed by the content id is unchanged.
   - A new external revision token over identical bytes is not a new revision.
   - A location that disappears is not recorded in v0.

5. **Local walking.** Only regular files are sources. Symlinks are never followed, inside or outside the root,
   and are reported. Directories are opened component by component from the root with `O_NOFOLLOW`, and
   `open()` re-applies the same policy. FIFOs, sockets and devices are reported and never opened. Names that
   are not valid UTF-8 cannot be canonical strings, so they are reported with their raw bytes and skipped. Walk
   order is depth-first, siblings in code-point order.

6. **Canonical JSON** is `neptune.identity.canonical_json`: a hand-written encoder rather than `json.dumps`
   options, because `json.dumps` silently accepts `None`, coerces non-string keys and cannot reject lone
   surrogates. `loads` accepts only bytes that re-encode identically. Integers above Python's 4300-digit
   limit are rejected, not converted.

## Alternatives considered

- **Chunk hashes only above a size threshold.** Saves one list entry per small file, but adds a threshold
  constant that consumers must know. Always emitting the list is simpler.
- **Larger chunks (64 MiB) or content-defined chunking.** Fewer hashes on 100 GB sources (about 1,600 vs 12,800
  per 100 GB). Content-defined chunking resists insertions better. 8 MiB matches common object-store multipart
  part sizes and keeps partial-change diagnosis useful. Content-defined chunking can be added as a second chunk
  list under a new ADR.
- **Revision sequence numbers.** Readable, but they need global coordination and a renumbering could rewrite
  history. A hash chain needs neither.
- **Bare `sha256:` for record ids.** Tier 1 and tier 2 would be indistinguishable in text, and a stored
  canonical-JSON source could coincide with a record id.
- **Following symlinks that stay inside the root.** The same bytes are reached through the real path anyway.
  Following them adds loop detection and a TOCTOU window for no evidence gain.
- **Decoding undecodable names with `surrogateescape`.** The resulting strings cannot enter canonical JSON
  (ADR 0002). A lossy replacement would be a silent rewrite of the name.

## Consequences

- The three MVL-2 acceptance criteria hold structurally and are tested end to end
  (`tests/integration/test_source_identity.py`).
- Persisting the ledger between runs, recording disappeared locations and the incremental-sync semantics are
  the store's (MVL-16) and MVL-9/MVL-45's. The shapes above do not need to change for them.
- Files behind symlinks and files with non-UTF-8 names are not ingested in v0; users see them as skipped
  entries. A policy override can be added through the manifest (MVL-14) if real corpora need it.
- `LocalSource` requires POSIX `dir_fd` / `O_NOFOLLOW`; Windows is unsupported until an ADR says otherwise.
- Revisit if chunk hashing becomes a measured bottleneck, or if consumers need stable ids for undecodable names.
