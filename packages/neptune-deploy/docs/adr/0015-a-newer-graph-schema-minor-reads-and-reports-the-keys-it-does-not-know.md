# 0015 — A newer graph-schema minor reads, and reports the keys Deploy does not know

- Status: Accepted
- Date: 2026-10-06
- Issue: MVL-161
- Amends: ADR 0013 §2 (the snapshot reader is strict about the contract's shapes)
- Uses: contracts/graph-schema (1.6.0 pinned; 1.9.0, Memory PR #133, adds an optional `builds` key to `Graph`)

## Context

ADR 0013 §2 refuses any key the pinned contract does not name (`snapshot_malformed`, with a JSON pointer).
graph-schema minors are additive: 1.9.0 adds `builds` to the graph document, and a Memory that has moved to it
writes the key into every snapshot. A strict reader would refuse every such snapshot until Deploy re-pins, and
a lenient one that dropped unknown keys would lose data without saying so. The document names only the major
(`graph_schema_version: 1`), never the minor.

## Decision

1. **The minor is declared by the caller.** `read_snapshot(document, schema_version="1.9.0")`,
   `load_snapshot(..., schema_version=...)` and `pack --snapshot-schema-version 1.9.0` carry the graph-schema
   version Memory built the snapshot under. Omitted, it is the pin (`GRAPH_SCHEMA_PIN`, held equal to
   `contracts/lock.toml` by a test). A declaration that is not `MAJOR.MINOR.PATCH` is `snapshot_unsupported`.
2. **Tolerance is exactly: same major as the pin, newer minor.** Then an unknown key, at any object level the
   reader checks, is not refused. Under the pin, an older version (including a newer patch of the pinned minor)
   or another major, an unknown key is refused as `snapshot_malformed`, as before.
3. **Known keys keep strict validation.** Types, patterns, required keys and the cross-field rules are checked
   in the same pass; a wrong or missing known key is refused under any declaration.
4. **One finding per distinct key path.** `snapshot_key_unread` carries `key_path` (the JSON pointer with array
   indices as `*`, e.g. `/claims/*/provenance/build_ref`), `pointer` (the first occurrence, concrete) and
   `occurrences`. Findings are ordered by `key_path`.
5. **The content is never read or rendered.** The document is read a second time with the unknown keys removed,
   so a claim's `raw` JSON (hence `claims.json` and the pack's `claims`) and an object's canonical bytes are the
   pinned shape. The snapshot id still hashes the whole document as given, so the spec names exactly what Memory
   wrote.
6. **Where it shows.** The pack gains a top-level `findings` list and `snapshot.declared_schema_version`, and the
   PDF header a `Not read:` line per finding. All three appear only when a finding exists.

## Consequences

- `COMPILER_VERSION` is not raised for this change: a pack of any snapshot compiler 3 read before is byte
  for byte what it was, and the new keys exist only for snapshots it refused. (It is 3 because the catalog-api
  1.7.0 pin changed the appendix's `api_version`; that is the pin PR's reason, not this one's.)
- A new object *kind* or enum value in a newer minor is still refused: this ADR is about unknown keys, and a
  new shape Deploy cannot read must not be guessed at.
- A key whose value matters to a reader (1.9.0's `builds`) is not carried into the pack. Deploy re-pins when
  it needs one, and the finding says what it did not read.
- Gap for Memory: the document has no minor, so a producer cannot state it where a consumer would find it. A
  `graph_schema_minor` key would let the caller drop the declaration; until then the flag is the contract.
